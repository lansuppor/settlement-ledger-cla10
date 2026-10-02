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
from app.store import installments
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

TENANT = "ti"
HEADERS = {"X-Tenant": TENANT}


def accept_order(order_id: str, *, tenant: str = TENANT, amount: int = 1000) -> dict:
    body = {
        "tenant": tenant,
        "order_id": order_id,
        "amount_cents": amount,
        "currency": "CNY",
        "idempotency_key": f"key-{order_id}",
        "request_fingerprint": f"sha256:order-{order_id}",
    }
    resp = client.post("/orders", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def register(order_id: str, key: str, fp: str, amount: int, *, tenant: str = TENANT):
    return client.post(
        f"/orders/{order_id}/installments",
        json={"installment_key": key, "request_fingerprint": fp, "amount_cents": amount},
        headers={"X-Tenant": tenant},
    )


def reverse(installment_id: str, reversal_id: str, fp: str, *, tenant: str = TENANT):
    return client.post(
        f"/installments/{installment_id}/reversal",
        json={"reversal_id": reversal_id, "request_fingerprint": fp},
        headers={"X-Tenant": tenant},
    )


def read_order(order_id: str, *, tenant: str = TENANT) -> dict:
    resp = client.get(f"/orders/{order_id}", headers={"X-Tenant": tenant})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- 分期登记与读取 ----------

def test_register_installment_assigns_id_and_lowers_outstanding() -> None:
    accept_order("i1", amount=500)
    resp = register("i1", "INST-1", "sha256:inst1", 200)
    assert resp.status_code == 200 and resp.headers.get("X-Idempotency-Replay") is None
    body = resp.json()
    installment_id = body["installment_id"]
    assert isinstance(installment_id, str) and installment_id.startswith("I")
    assert body["installment_key"] == "INST-1" and body["amount_cents"] == 200 and body["status"] == "active"
    order = body["order"]
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 300
    # 应收恒等于已收加未收。
    assert order["amount_cents"] == order["paid_cents"] + order["outstanding_cents"]

    got = client.get(f"/installments/{installment_id}", headers=HEADERS)
    assert got.status_code == 200
    assert got.json() == {
        "tenant": TENANT,
        "installment_id": installment_id,
        "installment_key": "INST-1",
        "order_id": "i1",
        "amount_cents": 200,
        "status": "active",
    }
    assert read_order("i1")["status"] == "accepted"

    # 第二笔分期把未收打平：订单结清。
    assert register("i1", "INST-1B", "sha256:inst1b", 300).status_code == 200
    settled = read_order("i1")
    assert settled["paid_cents"] == 500 and settled["outstanding_cents"] == 0 and settled["status"] == "settled"


def test_multiple_installments_get_distinct_stable_ids() -> None:
    accept_order("i1m", amount=900)
    first = register("i1m", "INST-M1", "fp-m1", 100).json()
    second = register("i1m", "INST-M2", "fp-m2", 100).json()
    assert first["installment_id"] != second["installment_id"]
    # 标识稳定不变。
    got = client.get(f"/installments/{first['installment_id']}", headers=HEADERS).json()
    assert got["installment_id"] == first["installment_id"] and got["status"] == "active"


def test_installment_and_whole_order_payment_share_outstanding() -> None:
    # 分期与整单收款共用订单已收/未收，但各自使用独立记录空间。
    accept_order("ix", amount=500)
    paid = client.post("/orders/ix/payments", json={"amount_cents": 200}, headers=HEADERS)
    assert paid.status_code == 200
    assert register("ix", "INST-X", "sha256:instx", 200).status_code == 200
    order = read_order("ix")
    assert order["paid_cents"] == 400 and order["outstanding_cents"] == 100
    # 整单收款 200 已登记，剩余未收只有 100：分期 200 必被拒绝。
    assert register("ix", "INST-X2", "sha256:instx2", 200).status_code == 409
    assert read_order("ix")["paid_cents"] == 400


# ---------- 重放与标识冲突 ----------

def test_replay_same_key_same_fingerprint_returns_snapshot_without_double_effect() -> None:
    accept_order("i2", amount=400)
    first = register("i2", "INST-2", "sha256:inst2", 300)
    assert first.status_code == 200
    snapshot = first.json()

    # 用整单收款改变订单当前状态。
    assert client.post("/orders/i2/payments", json={"amount_cents": 100}, headers=HEADERS).status_code == 200
    assert read_order("i2")["status"] == "settled"

    replay = register("i2", "INST-2", "sha256:inst2", 300)
    assert replay.status_code == 200 and replay.headers["X-Idempotency-Replay"] == "true"
    # 返回首次登记的结果快照，而非当前订单。
    assert replay.json() == snapshot
    assert replay.json()["order"]["paid_cents"] == 300
    # 分期未被重复登记：分期表只有一行，订单已收仍是 400。
    assert read_order("i2")["paid_cents"] == 400
    assert _count_installments_by_key("INST-2") == 1


def test_same_key_different_fingerprint_is_conflict() -> None:
    accept_order("i3", amount=400)
    assert register("i3", "INST-3", "sha256:aaa", 100).status_code == 200
    resp = register("i3", "INST-3", "sha256:bbb", 100)
    # 分期业务标识复用给不同业务内容：422，与超未收 409 明确区分。
    assert resp.status_code == 422 and "fingerprint" in resp.json()["detail"]
    # 首次分期记录与订单已收均不得被覆盖。
    assert read_order("i3")["paid_cents"] == 100
    assert _count_installments_by_key("INST-3") == 1


def test_installment_cannot_exceed_outstanding() -> None:
    accept_order("i4", amount=300)
    assert register("i4", "INST-4A", "fp-4a", 200).status_code == 200
    resp = register("i4", "INST-4B", "fp-4b", 200)
    assert resp.status_code == 409 and "outstanding" in resp.json()["detail"]
    # 订单与任何分期记录均不改变。
    order = read_order("i4")
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 100
    assert installments.get_installment(TENANT, _id_by_key("INST-4A"))["status"] == "active"
    assert _count_installments_by_key("INST-4B") == 0


def test_missing_installment_fields_is_400_and_persists_nothing() -> None:
    accept_order("i5", amount=300)
    base = {"installment_key": "INST-5", "request_fingerprint": "fp5", "amount_cents": 100}
    for stripped in ("installment_key", "request_fingerprint"):
        bad = {k: v for k, v in base.items() if k != stripped}
        resp = client.post("/orders/i5/installments", json=bad, headers=HEADERS)
        assert resp.status_code == 400, (stripped, resp.status_code)
    # 空串与非正金额同样拒绝。
    for bad in (
        {"installment_key": "", "request_fingerprint": "fp5", "amount_cents": 100},
        {"installment_key": "INST-5E", "request_fingerprint": "", "amount_cents": 100},
        {"installment_key": "INST-5E", "request_fingerprint": "fp5", "amount_cents": 0},
    ):
        assert client.post("/orders/i5/installments", json=bad, headers=HEADERS).status_code == 400
    # 不落任何分期数据，订单已收仍为 0。
    assert read_order("i5")["paid_cents"] == 0
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM installments WHERE tenant=? AND order_id='i5'", (TENANT,)
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_register_nonexistent_and_cross_tenant_order_is_not_found() -> None:
    resp = register("i-NOPE", "INST-X0", "fp-x0", 100)
    assert resp.status_code == 404

    accept_order("i6", tenant="tA", amount=300)
    # 跨租户登记按订单不存在处理，不改变订单，也不消耗该分期业务标识。
    cross = register("i6", "INST-6", "fp-6", 100, tenant="tB")
    assert cross.status_code == 404
    assert read_order("i6", tenant="tA")["paid_cents"] == 0
    own = register("i6", "INST-6", "fp-6", 100, tenant="tA")
    assert own.status_code == 200 and own.headers.get("X-Idempotency-Replay") is None


def test_installment_cross_tenant_read_is_not_found() -> None:
    accept_order("i7", tenant="tA", amount=300)
    installment_id = register("i7", "INST-7", "fp-7", 100, tenant="tA").json()["installment_id"]
    assert client.get(f"/installments/{installment_id}", headers={"X-Tenant": "tB"}).status_code == 404


def test_installment_key_is_scoped_per_tenant() -> None:
    accept_order("i8a", tenant="tA", amount=300)
    accept_order("i8b", tenant="tB", amount=300)
    # 不同租户可使用相同分期业务标识，互不影响。
    ra = register("i8a", "INST-8", "fp-8", 100, tenant="tA")
    rb = register("i8b", "INST-8", "fp-8", 100, tenant="tB")
    assert ra.status_code == 200 and rb.status_code == 200
    assert ra.json()["installment_id"] != rb.json()["installment_id"]


# ---------- 分期冲正 ----------

def _register_one(order_id: str, key: str, amount: int, *, tenant: str = TENANT, fp: str | None = None) -> str:
    resp = register(order_id, key, fp or f"sha256:{key}", amount, tenant=tenant)
    assert resp.status_code == 200, resp.text
    return resp.json()["installment_id"]


def test_reverse_installment_restores_outstanding_and_recomputes_status() -> None:
    accept_order("ir1", amount=500)
    installment_id = _register_one("ir1", "INST-R1", 500)
    assert read_order("ir1")["status"] == "settled"

    resp = reverse(installment_id, "IREV-1", "sha256:irev1")
    assert resp.status_code == 200 and resp.headers.get("X-Idempotency-Replay") is None
    body = resp.json()
    assert body["reversal_id"] == "IREV-1" and body["amount_cents"] == 500 and body["status"] == "reversed"
    order = body["order"]
    # 全额冲正后回到受理态：已收 0、未收 500。
    assert order["status"] == "accepted" and order["paid_cents"] == 0 and order["outstanding_cents"] == 500
    assert order["amount_cents"] == order["paid_cents"] + order["outstanding_cents"]
    # 分期记录标记为已冲正，订单当前状态与快照一致。
    assert client.get(f"/installments/{installment_id}", headers=HEADERS).json()["status"] == "reversed"
    current = read_order("ir1")
    assert current["paid_cents"] == 0 and current["outstanding_cents"] == 500 and current["status"] == "accepted"


def test_partial_installment_reversal_keeps_accepted() -> None:
    accept_order("ir1p", amount=300)
    first = _register_one("ir1p", "INST-R1P", 200)
    _register_one("ir1p", "INST-R1P2", 100)
    # 冲正其中一笔 200：仍有已收 100、未收 200，回到受理态。
    resp = reverse(first, "IREV-1P", "sha256:irev1p")
    assert resp.status_code == 200
    order = resp.json()["order"]
    assert order["paid_cents"] == 100 and order["outstanding_cents"] == 200 and order["status"] == "accepted"
    # 冲回的未收额度可以再登记新的分期。
    assert register("ir1p", "INST-R1P3", "fp-r1p3", 200).status_code == 200
    assert read_order("ir1p")["status"] == "settled"


def test_reverse_replay_returns_snapshot_without_double_effect() -> None:
    accept_order("ir2", amount=400)
    installment_id = _register_one("ir2", "INST-R2", 300)
    first = reverse(installment_id, "IREV-2", "sha256:irev2")
    assert first.status_code == 200
    snapshot = first.json()

    # 再登记整单收款并结清，改变订单当前状态。
    assert client.post("/orders/ir2/payments", json={"amount_cents": 400}, headers=HEADERS).status_code == 200
    assert read_order("ir2")["status"] == "settled"

    replay = reverse(installment_id, "IREV-2", "sha256:irev2")
    assert replay.status_code == 200 and replay.headers["X-Idempotency-Replay"] == "true"
    assert replay.json() == snapshot
    assert replay.json()["order"]["paid_cents"] == 0
    # 金额只被抵回一次：当前订单已收 400。
    assert read_order("ir2")["paid_cents"] == 400


def test_same_reversal_id_different_fingerprint_is_conflict() -> None:
    accept_order("ir3", amount=400)
    installment_id = _register_one("ir3", "INST-R3", 100)
    assert reverse(installment_id, "IREV-3", "sha256:aaa").status_code == 200
    resp = reverse(installment_id, "IREV-3", "sha256:bbb")
    assert resp.status_code == 422 and "fingerprint" in resp.json()["detail"]
    assert client.get(f"/installments/{installment_id}", headers=HEADERS).json()["status"] == "reversed"
    order = read_order("ir3")
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 400


def test_reversing_already_reversed_installment_with_new_id_is_409() -> None:
    accept_order("ir4", amount=400)
    installment_id = _register_one("ir4", "INST-R4", 100)
    assert reverse(installment_id, "IREV-4", "sha256:first").status_code == 200
    # 分期冲正不可再被冲正：新冲正标识作用于已冲正分期，按已处理拒绝。
    resp = reverse(installment_id, "IREV-4-OTHER", "sha256:second")
    assert resp.status_code == 409 and "already reversed" in resp.json()["detail"]
    # 抵回只发生一次。
    assert read_order("ir4")["paid_cents"] == 0
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM installment_reversals WHERE tenant=? AND installment_id=?",
            (TENANT, installment_id),
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_reverse_missing_fields_is_400_and_persists_nothing() -> None:
    accept_order("ir5", amount=400)
    installment_id = _register_one("ir5", "INST-R5", 100)
    for stripped in ("reversal_id", "request_fingerprint"):
        body = {"reversal_id": "IREV-5", "request_fingerprint": "sha256:irev5"}
        del body[stripped]
        resp = client.post(f"/installments/{installment_id}/reversal", json=body, headers=HEADERS)
        assert resp.status_code == 400, (stripped, resp.status_code)
    assert client.post(
        f"/installments/{installment_id}/reversal",
        json={"reversal_id": "", "request_fingerprint": "sha256:irev5"},
        headers=HEADERS,
    ).status_code == 400
    # 不落任何冲正数据，分期仍有效。
    assert client.get(f"/installments/{installment_id}", headers=HEADERS).json()["status"] == "active"


def test_reverse_nonexistent_and_cross_tenant_is_not_found() -> None:
    assert reverse("I-NOPE", "IREV-X", "sha256:x").status_code == 404

    accept_order("ir6", tenant="tA", amount=400)
    installment_id = _register_one("ir6", "INST-R6", 100, tenant="tA")
    assert reverse(installment_id, "IREV-6", "sha256:irev6", tenant="tB").status_code == 404
    assert client.get(f"/installments/{installment_id}", headers={"X-Tenant": "tA"}).json()["status"] == "active"
    # 跨租户冲正不得消耗该冲正标识：本租户用同一标识仍可首次生效。
    assert reverse(installment_id, "IREV-6", "sha256:irev6", tenant="tA").status_code == 200


def test_reversal_id_is_scoped_per_tenant() -> None:
    accept_order("ir7a", tenant="tA", amount=400)
    accept_order("ir7b", tenant="tB", amount=400)
    ia = _register_one("ir7a", "INST-R7A", 100, tenant="tA")
    ib = _register_one("ir7b", "INST-R7B", 100, tenant="tB")
    assert reverse(ia, "IREV-7", "sha256:same", tenant="tA").status_code == 200
    assert reverse(ib, "IREV-7", "sha256:same", tenant="tB").status_code == 200


# ---------- 并发 ----------

def test_concurrent_same_installment_key_only_one_takes_effect() -> None:
    url = _start_server()
    accept_order("ic1", amount=800)
    body = {"installment_key": "INST-C1", "request_fingerprint": "sha256:ic1", "amount_cents": 300}
    with httpx.Client(base_url=url, timeout=30) as http, ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(lambda _: http.post("/orders/ic1/installments", json=body, headers=HEADERS), range(8))
        )
    assert all(r.status_code == 200 for r in responses)
    assert sum(1 for r in responses if r.headers.get("X-Idempotency-Replay") == "true") == 7
    assert len({r.text for r in responses}) == 1
    _assert_single_row("installments", "installment_key", "INST-C1")
    order = read_order("ic1")
    assert order["paid_cents"] == 300 and order["outstanding_cents"] == 500


def test_concurrent_distinct_keys_on_same_order_amount_closes() -> None:
    # 8 笔各 200 作用于金额 600 的订单：恰好 3 笔生效，其余因超过未收 409；
    # 任一时刻已收不超过应收。
    url = _start_server()
    accept_order("ic2", amount=600)

    def one(i: int) -> httpx.Response:
        with httpx.Client(base_url=url, timeout=30) as http:
            return http.post(
                "/orders/ic2/installments",
                json={"installment_key": f"INST-C2-{i}", "request_fingerprint": f"sha256:{i}", "amount_cents": 200},
                headers=HEADERS,
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = [f.result() for f in [pool.submit(one, i) for i in range(8)]]
    assert sum(1 for r in responses if r.status_code == 200) == 3
    assert sum(1 for r in responses if r.status_code == 409) == 5
    order = read_order("ic2")
    assert order["paid_cents"] == 600 and order["outstanding_cents"] == 0 and order["status"] == "settled"


def test_concurrent_same_reversal_id_only_one_takes_effect() -> None:
    url = _start_server()
    accept_order("irc1", amount=800)
    installment_id = _register_one("irc1", "INST-RC1", 300)
    body = {"reversal_id": "IREV-C1", "request_fingerprint": "sha256:ircv1"}
    with httpx.Client(base_url=url, timeout=30) as http, ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(
                lambda _: http.post(f"/installments/{installment_id}/reversal", json=body, headers=HEADERS),
                range(8),
            )
        )
    assert all(r.status_code == 200 for r in responses)
    assert sum(1 for r in responses if r.headers.get("X-Idempotency-Replay") == "true") == 7
    assert len({r.text for r in responses}) == 1
    _assert_single_row("installment_reversals", "reversal_id", "IREV-C1")
    order = read_order("irc1")
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 800


def test_concurrent_distinct_reversal_ids_on_same_installment_exactly_one_wins() -> None:
    url = _start_server()
    accept_order("irc2", amount=600)
    installment_id = _register_one("irc2", "INST-RC2", 250)

    def one(i: int) -> httpx.Response:
        with httpx.Client(base_url=url, timeout=30) as http:
            return http.post(
                f"/installments/{installment_id}/reversal",
                json={"reversal_id": f"IREV-C2-{i}", "request_fingerprint": f"sha256:{i}"},
                headers=HEADERS,
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = [f.result() for f in [pool.submit(one, i) for i in range(8)]]
    assert sum(1 for r in responses if r.status_code == 200) == 1
    assert all(r.status_code in (200, 409) for r in responses)
    assert sum(1 for r in responses if r.status_code == 409) == 7
    _assert_single_row("installment_reversals", "installment_id", installment_id)
    order = read_order("irc2")
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 600


# ---------- 崩溃恢复 ----------

def test_crash_before_installment_commit_then_retry_behaves_like_first() -> None:
    accept_order("icr", amount=200, tenant="tc")
    env = {**os.environ, "APP_CRASH_INSTALLMENT_BEFORE_COMMIT": "1"}
    code = (
        "from app.store.db import migrate; "
        "from app.store import installments; "
        "migrate(); "
        "installments.register_installment('tc', 'icr', 'INST-CR', 'sha256:crash', 200)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    # 提交前崩溃：分期记录不存在，订单已收仍为 0。
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM installments WHERE tenant='tc' AND installment_key='INST-CR'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()
    assert read_order("icr", tenant="tc")["paid_cents"] == 0

    # 重启后用同一业务标识重试：按首次登记生效，而非误判为重放。
    resp = register("icr", "INST-CR", "sha256:crash", 200, tenant="tc")
    assert resp.status_code == 200 and resp.headers.get("X-Idempotency-Replay") is None
    assert resp.json()["order"]["status"] == "settled" and resp.json()["order"]["outstanding_cents"] == 0
    replay = register("icr", "INST-CR", "sha256:crash", 200, tenant="tc")
    assert replay.status_code == 200 and replay.headers["X-Idempotency-Replay"] == "true"


def test_crash_before_installment_reversal_commit_then_retry_behaves_like_first() -> None:
    accept_order("ircr", amount=200, tenant="tc")
    installment_id = _register_one("ircr", "INST-RCR", 200, tenant="tc")
    env = {**os.environ, "APP_CRASH_INSTALLMENT_REVERSAL_BEFORE_COMMIT": "1"}
    code = (
        "from app.store.db import migrate; "
        "from app.store import installments; "
        "migrate(); "
        f"installments.reverse_installment('tc', '{installment_id}', 'IREV-CR', 'sha256:crash')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM installment_reversals WHERE tenant='tc' AND reversal_id='IREV-CR'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT status FROM installments WHERE tenant='tc' AND installment_id=?", (installment_id,)
        ).fetchone()["status"] == "active"
    finally:
        conn.close()
    assert read_order("ircr", tenant="tc")["status"] == "settled"

    resp = reverse(installment_id, "IREV-CR", "sha256:crash", tenant="tc")
    assert resp.status_code == 200 and resp.headers.get("X-Idempotency-Replay") is None
    assert resp.json()["order"]["status"] == "accepted" and resp.json()["order"]["outstanding_cents"] == 200
    replay = reverse(installment_id, "IREV-CR", "sha256:crash", tenant="tc")
    assert replay.status_code == 200 and replay.headers["X-Idempotency-Replay"] == "true"


# ---------- 辅助 ----------

def _count_installments_by_key(key: str, *, tenant: str = TENANT) -> int:
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM installments WHERE tenant=? AND installment_key=?", (tenant, key)
        ).fetchone()["c"]
    finally:
        conn.close()


def _id_by_key(key: str, *, tenant: str = TENANT) -> str:
    conn = connect()
    try:
        return conn.execute(
            "SELECT installment_id FROM installments WHERE tenant=? AND installment_key=?", (tenant, key)
        ).fetchone()["installment_id"]
    finally:
        conn.close()


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
        count = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE tenant=? AND {column}=?", (TENANT, value)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1
