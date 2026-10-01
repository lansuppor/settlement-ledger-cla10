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


# ---------- 收款登记 payment_id ----------

def _register_payment(order_id: str, amount: int, *, tenant: str = "t1") -> dict:
    resp = client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_payment_id_is_assigned_and_readable() -> None:
    client.post("/orders", json=order_body("p0", "pk0", "pfp0", amount=300))
    paid = _register_payment("p0", 100)
    payment_id = paid["payment_id"]
    assert isinstance(payment_id, str) and payment_id
    got = client.get(f"/payments/{payment_id}", headers={"X-Tenant": "t1"})
    assert got.status_code == 200
    assert got.json() == {
        "tenant": "t1",
        "payment_id": payment_id,
        "order_id": "p0",
        "amount_cents": 100,
        "status": "active",
    }
    # 不同收款得到不同且稳定的标识。
    second = _register_payment("p0", 50)
    assert second["payment_id"] != payment_id
    assert client.get(f"/payments/{payment_id}", headers={"X-Tenant": "t1"}).json()["status"] == "active"


def test_payment_cross_tenant_read_is_not_found() -> None:
    client.post("/orders", json=order_body("p0x", "pk0x", "pfp0x", tenant="t1"))
    payment_id = _register_payment("p0x", 100)["payment_id"]
    assert client.get(f"/payments/{payment_id}", headers={"X-Tenant": "t2"}).status_code == 404


# ---------- 收款冲正 ----------

def _reverse(payment_id: str, reversal_id: str, fp: str, *, tenant: str = "t1"):
    return client.post(
        f"/payments/{payment_id}/reversal",
        json={"reversal_id": reversal_id, "request_fingerprint": fp},
        headers={"X-Tenant": tenant},
    )


def test_reverse_restores_outstanding_and_recomputes_status() -> None:
    client.post("/orders", json=order_body("r1", "rk1", "rfp1", amount=500))
    payment_id = _register_payment("r1", 500)["payment_id"]
    # 全额收款 → 已结清。
    assert client.get("/orders/r1", headers={"X-Tenant": "t1"}).json()["status"] == "settled"

    resp = _reverse(payment_id, "rev1", "sha256:r1")
    assert resp.status_code == 200 and resp.headers.get("X-Idempotency-Replay") is None
    body = resp.json()
    assert body["reversal_id"] == "rev1" and body["amount_cents"] == 500 and body["status"] == "reversed"
    order = body["order"]
    # 未收清零为已结清，否则回到受理态：全额冲正后回到受理态，已收 0、未收 500。
    assert order["status"] == "accepted" and order["paid_cents"] == 0 and order["outstanding_cents"] == 500
    # 应收恒等于已收 + 未收。
    assert order["amount_cents"] == order["paid_cents"] + order["outstanding_cents"]
    # 收款记录标记为已冲正，订单当前状态与快照一致。
    assert client.get(f"/payments/{payment_id}", headers={"X-Tenant": "t1"}).json()["status"] == "reversed"
    current = client.get("/orders/r1", headers={"X-Tenant": "t1"}).json()
    assert current["paid_cents"] == 0 and current["outstanding_cents"] == 500 and current["status"] == "accepted"


def test_partial_reversal_keeps_settled_when_outstanding_zero() -> None:
    client.post("/orders", json=order_body("r1p", "rk1p", "rfp1p", amount=300))
    first = _register_payment("r1p", 200)["payment_id"]
    _register_payment("r1p", 100)
    # 冲正其中一笔 200：仍有已收 100，未收 200，回到受理态。
    resp = _reverse(first, "rev1p", "sha256:r1p")
    assert resp.status_code == 200
    order = resp.json()["order"]
    assert order["paid_cents"] == 100 and order["outstanding_cents"] == 200 and order["status"] == "accepted"
    assert order["amount_cents"] == order["paid_cents"] + order["outstanding_cents"]


def test_reverse_replay_same_fingerprint_returns_snapshot_without_double_effect() -> None:
    client.post("/orders", json=order_body("r2", "rk2", "rfp2", amount=400))
    payment_id = _register_payment("r2", 300)["payment_id"]
    first = _reverse(payment_id, "rev2", "sha256:r2")
    assert first.status_code == 200
    snapshot = first.json()

    # 再登记收款并结清，改变订单当前状态。
    _register_payment("r2", 400)
    assert client.get("/orders/r2", headers={"X-Tenant": "t1"}).json()["status"] == "settled"

    replay = _reverse(payment_id, "rev2", "sha256:r2")
    assert replay.status_code == 200 and replay.headers["X-Idempotency-Replay"] == "true"
    # 返回首次冲正的结果快照，而非当前订单。
    assert replay.json() == snapshot
    assert replay.json()["order"]["paid_cents"] == 0
    # 金额只被抵回一次：当前订单仍结清、已收 400。
    assert client.get("/orders/r2", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 400


def test_same_reversal_id_different_fingerprint_is_conflict() -> None:
    client.post("/orders", json=order_body("r3", "rk3", "rfp3", amount=400))
    payment_id = _register_payment("r3", 100)["payment_id"]
    assert _reverse(payment_id, "rev3", "sha256:aaa").status_code == 200
    resp = _reverse(payment_id, "rev3", "sha256:bbb")
    # 冲正标识复用给不同业务内容：422，与已冲正 409 明确可区分。
    assert resp.status_code == 422 and "fingerprint" in resp.json()["detail"]
    # 首次冲正记录与收款抵回均不得被覆盖。
    assert client.get(f"/payments/{payment_id}", headers={"X-Tenant": "t1"}).json()["status"] == "reversed"
    order = client.get("/orders/r3", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 400


def test_reversing_already_reversed_payment_with_new_id_is_409() -> None:
    client.post("/orders", json=order_body("r4", "rk4", "rfp4", amount=400))
    payment_id = _register_payment("r4", 100)["payment_id"]
    assert _reverse(payment_id, "rev4", "sha256:first").status_code == 200
    # 冲正本身不可再被冲正：新冲正标识作用于已冲正收款，按已处理拒绝。
    resp = _reverse(payment_id, "rev4-other", "sha256:second")
    assert resp.status_code == 409 and "already reversed" in resp.json()["detail"]
    # 抵回只发生一次。
    assert client.get("/orders/r4", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM payment_reversals WHERE tenant='t1' AND payment_id=?",
            (payment_id,),
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_reverse_missing_fields_is_400_and_persists_nothing() -> None:
    client.post("/orders", json=order_body("r5", "rk5", "rfp5", amount=400))
    payment_id = _register_payment("r5", 100)["payment_id"]
    for stripped in ("reversal_id", "request_fingerprint"):
        body = {"reversal_id": "rev5", "request_fingerprint": "sha256:r5"}
        del body[stripped]
        resp = client.post(f"/payments/{payment_id}/reversal", json=body, headers={"X-Tenant": "t1"})
        assert resp.status_code == 400, (stripped, resp.status_code)
    assert client.post(
        f"/payments/{payment_id}/reversal",
        json={"reversal_id": "", "request_fingerprint": "sha256:r5"},
        headers={"X-Tenant": "t1"},
    ).status_code == 400
    # 不落任何冲正数据，收款仍有效。
    assert client.get(f"/payments/{payment_id}", headers={"X-Tenant": "t1"}).json()["status"] == "active"


def test_reverse_nonexistent_and_cross_tenant_is_not_found() -> None:
    assert _reverse("P-NOPE", "rev-x", "sha256:x").status_code == 404

    client.post("/orders", json=order_body("r6", "rk6", "rfp6", tenant="t1"))
    payment_id = _register_payment("r6", 100, tenant="t1")["payment_id"]
    # 跨租户冲正按不存在处理，不改变订单与收款。
    assert _reverse(payment_id, "rev6", "sha256:r6", tenant="t2").status_code == 404
    assert client.get(f"/payments/{payment_id}", headers={"X-Tenant": "t1"}).json()["status"] == "active"
    # 跨租户冲正不得消耗该冲正标识：本租户用同一标识仍可首次生效。
    assert _reverse(payment_id, "rev6", "sha256:r6", tenant="t1").status_code == 200


def test_reversal_id_is_scoped_per_tenant() -> None:
    client.post("/orders", json=order_body("r7a", "rk7a", "rfp7a", tenant="ta"))
    client.post("/orders", json=order_body("r7b", "rk7b", "rfp7b", tenant="tb"))
    pa = _register_payment("r7a", 100, tenant="ta")["payment_id"]
    pb = _register_payment("r7b", 100, tenant="tb")["payment_id"]
    # 不同租户的相同冲正标识互不影响。
    assert _reverse(pa, "rev7", "sha256:same", tenant="ta").status_code == 200
    assert _reverse(pb, "rev7", "sha256:same", tenant="tb").status_code == 200


def test_concurrent_same_reversal_id_only_one_takes_effect() -> None:
    url = _start_server()
    client.post("/orders", json=order_body("rc1", "rkc1", "rfpc1", amount=800))
    payment_id = _register_payment("rc1", 300)["payment_id"]
    body = {"reversal_id": "revc1", "request_fingerprint": "sha256:rc1"}
    with httpx.Client(base_url=url, timeout=30) as http, ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(
                lambda _: http.post(
                    f"/payments/{payment_id}/reversal", json=body, headers={"X-Tenant": "t1"}
                ),
                range(8),
            )
        )
    assert all(r.status_code == 200 for r in responses)
    assert sum(1 for r in responses if r.headers.get("X-Idempotency-Replay") == "true") == 7
    assert len({r.text for r in responses}) == 1
    _assert_single_row_where("payment_reversals", "reversal_id", "revc1")
    order = client.get("/orders/rc1", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 800


def test_concurrent_distinct_reversal_ids_on_same_payment_exactly_one_wins() -> None:
    url = _start_server()
    client.post("/orders", json=order_body("rc2", "rkc2", "rfpc2", amount=600))
    payment_id = _register_payment("rc2", 250)["payment_id"]

    def one(i: int) -> httpx.Response:
        with httpx.Client(base_url=url, timeout=30) as http:
            return http.post(
                f"/payments/{payment_id}/reversal",
                json={"reversal_id": f"revc2-{i}", "request_fingerprint": f"sha256:{i}"},
                headers={"X-Tenant": "t1"},
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = [f.result() for f in [pool.submit(one, i) for i in range(8)]]
    assert sum(1 for r in responses if r.status_code == 200) == 1
    assert all(r.status_code in (200, 409) for r in responses)
    assert sum(1 for r in responses if r.status_code == 409) == 7
    _assert_single_row_where("payment_reversals", "payment_id", payment_id)
    order = client.get("/orders/rc2", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 600


def test_crash_before_reversal_commit_then_retry_behaves_like_first() -> None:
    client.post("/orders", json=order_body("rcr", "rkcr", "rfpcr", amount=200, tenant="tc"))
    payment_id = _register_payment("rcr", 200, tenant="tc")["payment_id"]
    env = {**os.environ, "APP_CRASH_REVERSAL_BEFORE_COMMIT": "1"}
    code = (
        "from app.store.db import migrate; "
        "from app.store import orders; "
        "migrate(); "
        f"orders.reverse_payment('tc', '{payment_id}', 'revcr', 'sha256:crash')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    # 提交前崩溃：冲正记录不存在，收款仍有效，订单仍结清。
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM payment_reversals WHERE tenant='tc' AND reversal_id='revcr'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT status FROM payments WHERE tenant='tc' AND payment_id=?", (payment_id,)
        ).fetchone()["status"] == "active"
    finally:
        conn.close()
    assert client.get("/orders/rcr", headers={"X-Tenant": "tc"}).json()["status"] == "settled"

    # 重启后用同一冲正标识重试：按首次冲正生效，结论与未崩溃时一致。
    resp = _reverse(payment_id, "revcr", "sha256:crash", tenant="tc")
    assert resp.status_code == 200 and resp.headers.get("X-Idempotency-Replay") is None
    assert resp.json()["order"]["status"] == "accepted" and resp.json()["order"]["outstanding_cents"] == 200
    # 再发一次即为确定性重放。
    replay = _reverse(payment_id, "revcr", "sha256:crash", tenant="tc")
    assert replay.status_code == 200 and replay.headers["X-Idempotency-Replay"] == "true"


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


def _assert_single_row_where(table: str, column: str, value: str, *, tenant: str = "t1") -> None:
    conn = connect()
    try:
        count = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE tenant=? AND {column}=?", (tenant, value)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1
