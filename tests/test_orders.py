import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))

import httpx
from fastapi.testclient import TestClient
from uvicorn import Config, Server

from app.entry import app
from app.store import orders
from app.store.db import connect, migrate

migrate()
client = TestClient(app)


def order_body(order_id: str, key: str, fp: str, *, tenant: str = "t1", amount: int = 100) -> dict:
    return {
        "tenant": tenant,
        "order_id": order_id,
        "amount_cents": amount,
        "currency": "CNY",
        "idempotency_key": key,
        "request_fingerprint": fp,
    }


def test_accept_and_read_order() -> None:
    body = order_body("o1", "k1", "fp1", amount=500)
    assert client.post("/orders", json=body).status_code == 201
    got = client.get("/orders/o1", headers={"X-Tenant": "t1"})
    assert got.status_code == 200 and got.json()["outstanding_cents"] == 500


def test_duplicate_is_refused() -> None:
    body = order_body("o2", "k2", "fp2")
    assert client.post("/orders", json=body).status_code == 201
    # 同一订单标识、换一个幂等键：仍按订单重复受理返回 409。
    dup = order_body("o2", "k2-other", "fp2")
    resp = client.post("/orders", json=dup)
    assert resp.status_code == 409 and "order already accepted" in resp.json()["detail"]


def test_cross_tenant_read_is_not_found() -> None:
    body = order_body("o3", "k3", "fp3")
    client.post("/orders", json=body)
    assert client.get("/orders/o3", headers={"X-Tenant": "t2"}).status_code == 404


def test_payment_cannot_exceed_outstanding() -> None:
    client.post("/orders", json=order_body("o4", "k4", "fp4", amount=300))
    assert client.post("/orders/o4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/o4/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"}).status_code == 409


def test_replay_same_fingerprint_returns_first_order() -> None:
    body = order_body("o5", "k5", "fp5", amount=700)
    first = client.post("/orders", json=body)
    assert first.status_code == 201 and first.headers.get("X-Idempotency-Replay") is None

    # 登记一笔收款，改变当前订单状态。
    assert client.post("/orders/o5/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"}).status_code == 200

    second = client.post("/orders", json=body)
    assert second.status_code == 201 and second.headers["X-Idempotency-Replay"] == "true"
    # 重放返回首次受理时的订单对象：paid_cents/outstanding 与首次一致。
    assert second.json() == first.json()
    assert second.json()["paid_cents"] == 0 and second.json()["outstanding_cents"] == 700
    # 收款未被重复登记，当前订单仍是已收 200。
    assert client.get("/orders/o5", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 200


def test_same_key_different_fingerprint_is_conflict() -> None:
    first = client.post("/orders", json=order_body("o6", "k6", "fp6-a", amount=100))
    assert first.status_code == 201
    conflict_body = order_body("o6-other", "k6", "fp6-b", amount=9999)
    resp = client.post("/orders", json=conflict_body)
    # 与订单重复（409）明确区分：幂等键复用给不同业务内容为 422。
    assert resp.status_code == 422 and "fingerprint" in resp.json()["detail"]
    # 首次受理的订单、金额、币种均不得被覆盖。
    original = client.get("/orders/o6", headers={"X-Tenant": "t1"}).json()
    assert original["amount_cents"] == 100 and original["currency"] == "CNY"
    assert client.get("/orders/o6-other", headers={"X-Tenant": "t1"}).status_code == 404


def test_missing_idempotency_fields_is_400_and_persists_nothing() -> None:
    base = order_body("o7", "k7", "fp7")
    for stripped in ("idempotency_key", "request_fingerprint"):
        bad = {k: v for k, v in base.items() if k != stripped}
        bad["order_id"] = f"o7-missing-{stripped}"
        resp = client.post("/orders", json=bad)
        assert resp.status_code == 400, (stripped, resp.status_code)
        assert orders.get("t1", bad["order_id"]) is None
    # 空字符串同样拒绝。
    empty = order_body("o7-empty", "", "fp7")
    assert client.post("/orders", json=empty).status_code == 400


def test_idempotency_key_is_scoped_per_tenant() -> None:
    client.post("/orders", json=order_body("o8a", "k8", "fp8", tenant="ta"))
    # 不同租户可使用相同幂等键，互不影响。
    other = client.post("/orders", json=order_body("o8b", "k8", "fp8", tenant="tb"))
    assert other.status_code == 201 and other.headers.get("X-Idempotency-Replay") is None


def test_concurrent_same_key_only_one_accepts() -> None:
    url = _start_server()
    body = order_body("oc1", "kc1", "fpc1", amount=150)
    with httpx.Client(base_url=url, timeout=30) as http, ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: http.post("/orders", json=body), range(8)))
    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 8
    assert sum(1 for r in responses if r.headers.get("X-Idempotency-Replay") == "true") == 7
    orders_json = {r.text for r in responses}
    assert len(orders_json) == 1
    _assert_single_row("orders", "order_id", "oc1")
    _assert_single_row("accepted_requests", "idempotency_key", "kc1")


def test_concurrent_same_key_different_fingerprints_exactly_one_wins() -> None:
    url = _start_server()
    bodies = [order_body("oc2", "kc2", f"fpc2-{i}", amount=100 + i) for i in range(8)]
    with httpx.Client(base_url=url, timeout=30) as http, ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(http.post, "/orders", json=b) for b in bodies]
        responses = [f.result() for f in futures]
    assert sum(1 for r in responses if r.status_code == 201) == 1
    assert all(r.status_code in (201, 422) for r in responses)
    _assert_single_row("orders", "order_id", "oc2")
    _assert_single_row("accepted_requests", "idempotency_key", "kc2")


def test_crash_before_commit_then_retry_behaves_like_first_accept() -> None:
    env = {**os.environ, "APP_CRASH_BEFORE_COMMIT": "1"}
    code = (
        "from app.store.db import migrate; "
        "from app.store import orders; "
        "migrate(); "
        "orders.accept_order('tc', 'kc-crash', 'fpcrash', 'o-crash', 100, 'CNY')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    # 提交前崩溃：订单与幂等记录都不存在。
    assert orders.get("tc", "o-crash") is None
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM accepted_requests WHERE tenant='tc' AND idempotency_key='kc-crash'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()

    # 重启后用同一幂等键重试：按“首次受理”成功，而不是误判为重放。
    order, replayed = orders.accept_order("tc", "kc-crash", "fpcrash", "o-crash", 100, "CNY")
    assert replayed is False and order["order_id"] == "o-crash"
    # 再发一次即为确定性重放。
    _, replayed_again = orders.accept_order("tc", "kc-crash", "fpcrash", "o-crash", 100, "CNY")
    assert replayed_again is True


_server: tuple | None = None


def _start_server() -> str:
    global _server
    if _server is not None:
        return _server[0]
    config = Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    _server = (url, server, thread)
    return url


def _assert_single_row(table: str, column: str, value: str) -> None:
    conn = connect()
    try:
        count = conn.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE {column}=?", (value,)).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1
