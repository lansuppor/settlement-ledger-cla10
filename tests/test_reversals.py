import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test-reversals.sqlite"))

import httpx
from fastapi.testclient import TestClient
from uvicorn import Config, Server

from app.entry import app
from app.store import orders
from app.store.db import connect, migrate

migrate()
client = TestClient(app)


def accept_order(order_id: str, *, tenant: str = "t1", amount: int = 500) -> None:
    body = {
        "tenant": tenant,
        "order_id": order_id,
        "amount_cents": amount,
        "currency": "CNY",
        "idempotency_key": f"key-{order_id}",
        "request_fingerprint": f"fp-{order_id}",
    }
    assert client.post("/orders", json=body).status_code == 201


def register_payment(order_id: str, amount: int, *, tenant: str = "t1") -> str:
    resp = client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})
    assert resp.status_code == 200, resp.text
    return resp.json()["payment_id"]


def reverse(payment_id: str, reversal_id: str, fp: str, *, tenant: str = "t1"):
    return client.post(
        f"/payments/{payment_id}/reversals",
        json={"reversal_id": reversal_id, "request_fingerprint": fp},
        headers={"X-Tenant": tenant},
    )


def order_view(order_id: str, *, tenant: str = "t1") -> dict:
    return client.get(f"/orders/{order_id}", headers={"X-Tenant": tenant}).json()


def test_payment_id_is_assigned_stable_and_unique_per_tenant() -> None:
    accept_order("rp-id", tenant="tid-a", amount=500)
    p1 = register_payment("rp-id", 100, tenant="tid-a")
    p2 = register_payment("rp-id", 100, tenant="tid-a")
    assert isinstance(p1, str) and p1 and p1 != p2
    # 服务分配的标识稳定可回读。
    assert orders.get_payment("tid-a", p1)["payment_id"] == p1
    # 租户间序号空间独立：另一租户首笔同样取得“1”，与 tid-a 的“1”共存且各属各单。
    accept_order("rp-id-other", tenant="tid-b", amount=500)
    p_other = register_payment("rp-id-other", 100, tenant="tid-b")
    assert p_other == p1
    assert orders.get_payment("tid-a", p1)["order_id"] == "rp-id"
    assert orders.get_payment("tid-b", p_other)["order_id"] == "rp-id-other"


def test_reverse_full_payment_settles_back_to_accepted() -> None:
    accept_order("rv-full", amount=500)
    pid = register_payment("rv-full", 500)
    assert order_view("rv-full")["status"] == "settled"

    resp = reverse(pid, "rev-full", "fp-rev-full")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert resp.headers.get("X-Idempotency-Replay") is None
    assert body["reversed_amount_cents"] == 500
    order = body["order"]
    # 应收恒等于已收加未收；未收未清零（回升到全额），回到受理态。
    assert order["amount_cents"] == order["paid_cents"] + order["outstanding_cents"]
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 500 and order["status"] == "accepted"
    assert orders.get_payment("t1", pid)["status"] == "reversed"


def test_reverse_one_of_several_payments() -> None:
    accept_order("rv-part", amount=500)
    p_a = register_payment("rv-part", 300)
    register_payment("rv-part", 200)
    assert order_view("rv-part")["status"] == "settled"

    resp = reverse(p_a, "rev-part", "fp-rev-part")
    order = resp.json()["order"]
    # 仅抵回被冲正那笔：已收回到 200，未收回升 300，仍为受理态。
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 300 and order["status"] == "accepted"


def test_reverse_when_outstanding_already_open_keeps_accepted() -> None:
    accept_order("rv-open", amount=500)
    pid = register_payment("rv-open", 200)
    assert order_view("rv-open")["status"] == "accepted"
    order = reverse(pid, "rev-open", "fp-rev-open").json()["order"]
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 500 and order["status"] == "accepted"


def test_replay_same_fingerprint_returns_first_snapshot_and_does_not_reverse_twice() -> None:
    accept_order("rv-replay", amount=500)
    pid = register_payment("rv-replay", 300)
    first = reverse(pid, "rev-replay", "fp-rev-replay")
    assert first.status_code == 200

    # 再次登记收款改变当前订单，重放仍应返回首次冲正快照，且不再抵回。
    register_payment("rv-replay", 100)
    second = reverse(pid, "rev-replay", "fp-rev-replay")
    assert second.status_code == 200 and second.headers["X-Idempotency-Replay"] == "true"
    assert second.json() == first.json()
    assert second.json()["order"]["paid_cents"] == 0  # 首次快照时已收为 0
    # 当前订单只含后来登记的 100，冲正金额没有被第二次抵回。
    assert order_view("rv-replay")["paid_cents"] == 100


def test_same_reversal_id_different_fingerprint_is_conflict() -> None:
    accept_order("rv-conflict", amount=500)
    pid = register_payment("rv-conflict", 200)
    assert reverse(pid, "rev-conflict", "fp-a").status_code == 200

    resp = reverse(pid, "rev-conflict", "fp-b")
    # 指纹冲突 422，与收款已处理 409 明确可区分。
    assert resp.status_code == 422 and "fingerprint" in resp.json()["detail"]
    # 首次冲正记录与已登记收款均不得被覆盖。
    assert order_view("rv-conflict")["paid_cents"] == 0
    assert orders.get_payment("t1", pid)["status"] == "reversed"


def test_reversing_already_reversed_payment_with_new_id_is_409() -> None:
    accept_order("rv-done", amount=500)
    pid = register_payment("rv-done", 200)
    assert reverse(pid, "rev-done-1", "fp-1").status_code == 200
    resp = reverse(pid, "rev-done-2", "fp-2")
    assert resp.status_code == 409 and "already reversed" in resp.json()["detail"]
    # 只抵回过一次。
    assert order_view("rv-done")["paid_cents"] == 0


def test_missing_reversal_fields_is_400_and_persists_nothing() -> None:
    accept_order("rv-400", amount=500)
    pid = register_payment("rv-400", 100)
    base = {"reversal_id": "rev-400", "request_fingerprint": "fp-400"}
    for stripped in ("reversal_id", "request_fingerprint"):
        bad = {k: v for k, v in base.items() if k != stripped}
        resp = client.post(f"/payments/{pid}/reversals", json=bad, headers={"X-Tenant": "t1"})
        assert resp.status_code == 400, (stripped, resp.status_code)
    # 空串同样拒绝。
    assert reverse(pid, "", "fp").status_code == 400
    assert reverse(pid, "rev-empty", "").status_code == 400
    # 未落任何冲正数据，收款仍为 active、订单已收不变。
    assert orders.get_payment("t1", pid)["status"] == "active"
    assert order_view("rv-400")["paid_cents"] == 100


def test_unknown_payment_is_404_and_changes_nothing() -> None:
    accept_order("rv-404", amount=500)
    resp = reverse("does-not-exist", "rev-missing", "fp-missing")
    assert resp.status_code == 404 and "payment not found" in resp.json()["detail"]
    assert order_view("rv-404")["paid_cents"] == 0


def test_cross_tenant_reversal_is_not_found() -> None:
    accept_order("rv-tenant", tenant="ta", amount=500)
    pid = register_payment("rv-tenant", 200, tenant="ta")
    # 跨租户按不存在处理，不改变订单与收款。
    assert reverse(pid, "rev-cross", "fp-cross", tenant="tb").status_code == 404
    assert orders.get_payment("ta", pid)["status"] == "active"
    assert order_view("rv-tenant", tenant="ta")["paid_cents"] == 200


def test_concurrent_same_reversal_id_only_one_takes_effect() -> None:
    accept_order("rv-c1", amount=500)
    pid = register_payment("rv-c1", 300)
    url = _start_server()
    body = {"reversal_id": "rev-c1", "request_fingerprint": "fp-c1"}
    with httpx.Client(base_url=url, timeout=30) as http, ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(lambda _: http.post(f"/payments/{pid}/reversals", json=body, headers={"X-Tenant": "t1"}), range(8))
        )
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 8
    assert sum(1 for r in responses if r.headers.get("X-Idempotency-Replay") == "true") == 7
    assert len({r.text for r in responses}) == 1
    # 只抵回一次：已收 300 全部抵回为 0，而不是被扣成负数或多次。
    assert order_view("rv-c1")["paid_cents"] == 0
    _assert_single_reversal("rev-c1")


def test_concurrent_distinct_reversal_ids_on_same_payment_exactly_one_wins() -> None:
    accept_order("rv-c2", amount=500)
    pid = register_payment("rv-c2", 300)
    url = _start_server()
    with httpx.Client(base_url=url, timeout=30) as http, ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                http.post,
                f"/payments/{pid}/reversals",
                json={"reversal_id": f"rev-c2-{i}", "request_fingerprint": f"fp-c2-{i}"},
                headers={"X-Tenant": "t1"},
            )
            for i in range(8)
        ]
        statuses = [f.result().status_code for f in futures]
    assert statuses.count(200) == 1
    # 其余均为确定性的“收款已处理”409，而非指纹冲突。
    assert statuses.count(409) == 7
    assert order_view("rv-c2")["paid_cents"] == 0
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS c FROM payment_reversals WHERE payment_id=?", (pid,)).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1


def test_crash_before_commit_then_retry_behaves_like_first_reversal() -> None:
    tenant, oid, rid = "tc-rv", "o-crash-rv", "rev-crash"
    orders.accept_order(tenant, "key-crash-rv", "fp-order", oid, 300, "CNY")
    _pid = orders.add_payment(tenant, oid, 300)[1]

    env = {**os.environ, "APP_CRASH_BEFORE_COMMIT": "1"}
    code = (
        "from app.store.db import migrate; "
        "from app.store import orders; "
        "migrate(); "
        f"orders.reverse_payment('{tenant}', {_pid!r}, '{rid}', 'fp-rev-crash')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    # 提交前崩溃：冲正记录不存在、收款仍 active、订单已收不变。
    assert orders.get_payment(tenant, _pid)["status"] == "active"
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM payment_reversals WHERE tenant=? AND reversal_id=?", (tenant, rid)
        ).fetchone()["c"] == 0
    finally:
        conn.close()
    assert orders.get(tenant, oid)["paid_cents"] == 300

    # 重启后用同一冲正标识重试：按“首次冲正”生效，而非误判为重放。
    snapshot, replayed = orders.reverse_payment(tenant, _pid, rid, "fp-rev-crash")
    assert replayed is False and snapshot["order"]["paid_cents"] == 0
    # 再发一次即为确定性重放。
    _, replayed_again = orders.reverse_payment(tenant, _pid, rid, "fp-rev-crash")
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


def _assert_single_reversal(reversal_id: str) -> None:
    conn = connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM payment_reversals WHERE reversal_id=?", (reversal_id,)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1
