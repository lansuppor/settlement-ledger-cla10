import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test-installments.sqlite"))

import httpx
from fastapi.testclient import TestClient
from uvicorn import Config, Server

from app.entry import app
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


def accept(order_id: str, *, tenant: str = "t1", amount: int = 1000) -> None:
    resp = client.post("/orders", json=order_body(order_id, f"k-{order_id}", f"fp-{order_id}", tenant=tenant, amount=amount))
    assert resp.status_code == 201, resp.text


def register(order_id: str, key: str, fp: str, amount: int, *, tenant: str = "t1"):
    return client.post(
        f"/orders/{order_id}/installments",
        json={"installment_key": key, "request_fingerprint": fp, "amount_cents": amount},
        headers={"X-Tenant": tenant},
    )


def reverse(installment_id: str, reversal_id: str, fp: str, *, tenant: str = "t1"):
    return client.post(
        f"/installments/{installment_id}/reversal",
        json={"reversal_id": reversal_id, "request_fingerprint": fp},
        headers={"X-Tenant": tenant},
    )


# ---------- 分期登记 ----------

def test_multiple_installments_accumulate_and_settle() -> None:
    accept("i1", amount=500)
    r1 = register("i1", "ik1", "ih1", 200)
    assert r1.status_code == 200, r1.text
    assert r1.headers.get("X-Idempotency-Replay") is None
    body1 = r1.json()
    assert body1["amount_cents"] == 200 and body1["status"] == "active"
    assert isinstance(body1["installment_id"], str) and body1["installment_id"].startswith("I")
    order = body1["order"]
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 300 and order["status"] == "accepted"
    assert order["amount_cents"] == order["paid_cents"] + order["outstanding_cents"]

    r2 = register("i1", "ik2", "ih2", 300)
    assert r2.status_code == 200
    order2 = r2.json()["order"]
    # 未收清零为已结清；应收恒等于已收加未收。
    assert order2["paid_cents"] == 500 and order2["outstanding_cents"] == 0 and order2["status"] == "settled"
    assert r2.json()["installment_id"] != body1["installment_id"]


def test_installment_cannot_exceed_outstanding() -> None:
    accept("i2", amount=300)
    assert register("i2", "ik2a", "ih2a", 100).status_code == 200
    over = register("i2", "ik2b", "ih2b", 201)
    assert over.status_code == 409 and "outstanding" in over.json()["detail"]
    # 拒绝不改变订单与任何分期记录。
    order = client.get("/orders/i2", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 100 and order["outstanding_cents"] == 200
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM installments WHERE tenant='t1' AND installment_key='ik2b'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()
    # 被拒绝的业务标识未被消耗：改为不超额金额以同一（标识, 指纹）重试等价于首次登记。
    retry = register("i2", "ik2b", "ih2b", 200)
    assert retry.status_code == 200 and retry.headers.get("X-Idempotency-Replay") is None
    assert retry.json()["order"]["status"] == "settled"


def test_register_replay_same_fingerprint_returns_first_snapshot() -> None:
    accept("i3", amount=700)
    first = register("i3", "ik3", "ih3", 200)
    assert first.status_code == 200
    # 再登记一笔改变订单当前状态。
    register("i3", "ik3-other", "ih3-other", 500)
    assert client.get("/orders/i3", headers={"X-Tenant": "t1"}).json()["status"] == "settled"

    replay = register("i3", "ik3", "ih3", 200)
    assert replay.status_code == 200 and replay.headers["X-Idempotency-Replay"] == "true"
    # 不新增分期、不改变订单金额与既有分期，返回首次登记的结果快照。
    assert replay.json() == first.json()
    assert replay.json()["order"]["paid_cents"] == 200 and replay.json()["order"]["outstanding_cents"] == 500
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM installments WHERE tenant='t1' AND order_id='i3'"
        ).fetchone()["c"] == 2
    finally:
        conn.close()


def test_same_installment_key_different_fingerprint_is_conflict() -> None:
    accept("i4", amount=400)
    first = register("i4", "ik4", "ih4-a", 100)
    assert first.status_code == 200
    conflict = register("i4", "ik4", "ih4-b", 100)
    # 业务标识被复用于不同业务内容：422，与超未收 409 明确可区分。
    assert conflict.status_code == 422 and "fingerprint" in conflict.json()["detail"]
    # 不覆盖首次记录，不改变订单与既有分期。
    order = client.get("/orders/i4", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 100 and order["outstanding_cents"] == 300
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT request_fingerprint, amount_cents FROM installments "
            "WHERE tenant='t1' AND installment_key='ik4'"
        ).fetchall()
        assert len(rows) == 1 and rows[0]["request_fingerprint"] == "ih4-a" and rows[0]["amount_cents"] == 100
    finally:
        conn.close()


def test_register_missing_fields_is_400_and_persists_nothing() -> None:
    accept("i5", amount=400)
    base = {"installment_key": "ik5", "request_fingerprint": "ih5", "amount_cents": 100}
    for stripped in ("installment_key", "request_fingerprint"):
        body = {k: v for k, v in base.items() if k != stripped}
        resp = client.post("/orders/i5/installments", json=body, headers={"X-Tenant": "t1"})
        assert resp.status_code == 400, (stripped, resp.status_code)
    for bad in (
        {"installment_key": "", "request_fingerprint": "ih5", "amount_cents": 100},
        {"installment_key": "ik5", "request_fingerprint": "", "amount_cents": 100},
        {"installment_key": "ik5", "request_fingerprint": "ih5", "amount_cents": 0},
        {"installment_key": "ik5", "request_fingerprint": "ih5", "amount_cents": -1},
    ):
        assert client.post("/orders/i5/installments", json=bad, headers={"X-Tenant": "t1"}).status_code == 400, bad
    # 不落任何数据。
    order = client.get("/orders/i5", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 400


def test_register_nonexistent_and_cross_tenant_order_is_not_found() -> None:
    assert register("i-missing", "ikx", "ihx", 1).status_code == 404
    accept("i6", tenant="t1", amount=100)
    # 跨租户登记按订单不存在处理，不消耗业务标识。
    assert register("i6", "ik6", "ih6", 100, tenant="t2").status_code == 404
    assert register("i6", "ik6", "ih6", 100, tenant="t1").status_code == 200


def test_installment_key_is_scoped_per_tenant() -> None:
    accept("i7a", tenant="ta", amount=100)
    accept("i7b", tenant="tb", amount=100)
    # 不同租户可使用相同业务标识，互不影响。
    ra = register("i7a", "ik7", "ih7", 100, tenant="ta")
    rb = register("i7b", "ik7", "ih7", 100, tenant="tb")
    assert ra.status_code == 200 and rb.status_code == 200
    assert ra.json()["installment_id"] != rb.json()["installment_id"]


# ---------- 分期读取 ----------

def test_installment_id_is_readable_stable_and_cross_tenant_404() -> None:
    accept("i8", amount=300)
    paid = register("i8", "ik8", "ih8", 100)
    installment_id = paid.json()["installment_id"]
    got = client.get(f"/installments/{installment_id}", headers={"X-Tenant": "t1"})
    assert got.status_code == 200
    assert got.json() == {
        "tenant": "t1",
        "installment_id": installment_id,
        "installment_key": "ik8",
        "order_id": "i8",
        "amount_cents": 100,
        "status": "active",
    }
    assert client.get(f"/installments/{installment_id}", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/installments/INOPE0000000000", headers={"X-Tenant": "t1"}).status_code == 404
    # 分期记录空间与整单收款独立：收款标识在分期入口读不到。
    payment = client.post("/orders/i8/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"}).json()
    assert client.get(f"/installments/{payment['payment_id']}", headers={"X-Tenant": "t1"}).status_code == 404
    # 服务分配标识稳定不变。
    assert client.get(f"/installments/{installment_id}", headers={"X-Tenant": "t1"}).json()["installment_id"] == installment_id


# ---------- 分期冲正 ----------

def test_reverse_restores_outstanding_and_recomputes_status() -> None:
    accept("ir1", amount=500)
    installment_id = register("ir1", "irk1", "irh1", 500).json()["installment_id"]
    # 全额分期 → 已结清。
    assert client.get("/orders/ir1", headers={"X-Tenant": "t1"}).json()["status"] == "settled"

    resp = reverse(installment_id, "irev1", "sha256:ir1")
    assert resp.status_code == 200 and resp.headers.get("X-Idempotency-Replay") is None
    body = resp.json()
    assert body["reversal_id"] == "irev1" and body["amount_cents"] == 500 and body["status"] == "reversed"
    order = body["order"]
    # 已收减少、未收回升；未收不为零回到受理态。
    assert order["status"] == "accepted" and order["paid_cents"] == 0 and order["outstanding_cents"] == 500
    assert order["amount_cents"] == order["paid_cents"] + order["outstanding_cents"]
    # 分期记录标记为已冲正，订单当前状态与快照一致。
    assert client.get(f"/installments/{installment_id}", headers={"X-Tenant": "t1"}).json()["status"] == "reversed"
    current = client.get("/orders/ir1", headers={"X-Tenant": "t1"}).json()
    assert current["paid_cents"] == 0 and current["outstanding_cents"] == 500 and current["status"] == "accepted"


def test_partial_reversal_goes_back_to_accepted() -> None:
    accept("ir1p", amount=300)
    first = register("ir1p", "irk1p", "irh1p", 200).json()["installment_id"]
    register("ir1p", "irk1p2", "irh1p2", 100)
    resp = reverse(first, "irev1p", "sha256:ir1p")
    assert resp.status_code == 200
    order = resp.json()["order"]
    assert order["paid_cents"] == 100 and order["outstanding_cents"] == 200 and order["status"] == "accepted"
    assert order["amount_cents"] == order["paid_cents"] + order["outstanding_cents"]


def test_reverse_replay_returns_snapshot_without_double_effect() -> None:
    accept("ir2", amount=400)
    installment_id = register("ir2", "irk2", "irh2", 300).json()["installment_id"]
    first = reverse(installment_id, "irev2", "sha256:ir2")
    assert first.status_code == 200
    snapshot = first.json()

    # 再登记分期并结清，改变订单当前状态。
    register("ir2", "irk2-more", "irh2-more", 400)
    assert client.get("/orders/ir2", headers={"X-Tenant": "t1"}).json()["status"] == "settled"

    replay = reverse(installment_id, "irev2", "sha256:ir2")
    assert replay.status_code == 200 and replay.headers["X-Idempotency-Replay"] == "true"
    # 返回首次冲正的结果快照，而非当前订单。
    assert replay.json() == snapshot
    assert replay.json()["order"]["paid_cents"] == 0
    # 金额只被抵回一次：当前订单仍结清、已收 400。
    assert client.get("/orders/ir2", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 400


def test_same_reversal_id_different_fingerprint_is_conflict() -> None:
    accept("ir3", amount=400)
    installment_id = register("ir3", "irk3", "irh3", 100).json()["installment_id"]
    assert reverse(installment_id, "irev3", "sha256:aaa").status_code == 200
    resp = reverse(installment_id, "irev3", "sha256:bbb")
    # 冲正标识复用给不同业务内容：422，与已冲正 409 明确可区分。
    assert resp.status_code == 422 and "fingerprint" in resp.json()["detail"]
    # 首次冲正记录与分期抵回均不得被覆盖。
    assert client.get(f"/installments/{installment_id}", headers={"X-Tenant": "t1"}).json()["status"] == "reversed"
    order = client.get("/orders/ir3", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 400


def test_reversing_already_reversed_installment_with_new_id_is_409() -> None:
    accept("ir4", amount=400)
    installment_id = register("ir4", "irk4", "irh4", 100).json()["installment_id"]
    assert reverse(installment_id, "irev4", "sha256:first").status_code == 200
    # 对已冲正分期再次冲正（含换新标识）按已处理拒绝。
    resp = reverse(installment_id, "irev4-other", "sha256:second")
    assert resp.status_code == 409 and "already reversed" in resp.json()["detail"]
    # 抵回只发生一次。
    assert client.get("/orders/ir4", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM installment_reversals WHERE tenant='t1' AND installment_id=?",
            (installment_id,),
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_reverse_missing_fields_is_400_and_persists_nothing() -> None:
    accept("ir5", amount=400)
    installment_id = register("ir5", "irk5", "irh5", 100).json()["installment_id"]
    for stripped in ("reversal_id", "request_fingerprint"):
        body = {"reversal_id": "irev5", "request_fingerprint": "sha256:ir5"}
        del body[stripped]
        resp = client.post(f"/installments/{installment_id}/reversal", json=body, headers={"X-Tenant": "t1"})
        assert resp.status_code == 400, (stripped, resp.status_code)
    assert reverse(installment_id, "", "sha256:ir5").status_code == 400
    # 不落任何冲正数据，分期仍有效。
    assert client.get(f"/installments/{installment_id}", headers={"X-Tenant": "t1"}).json()["status"] == "active"


def test_reverse_nonexistent_and_cross_tenant_is_not_found() -> None:
    assert reverse("INOPE0000000000", "irev-x", "sha256:x").status_code == 404

    accept("ir6", tenant="t1", amount=100)
    installment_id = register("ir6", "irk6", "irh6", 100, tenant="t1").json()["installment_id"]
    # 跨租户冲正按不存在处理，不改变订单与分期。
    assert reverse(installment_id, "irev6", "sha256:ir6", tenant="t2").status_code == 404
    assert client.get(f"/installments/{installment_id}", headers={"X-Tenant": "t1"}).json()["status"] == "active"
    # 跨租户冲正不得消耗该冲正标识：本租户用同一标识仍可首次生效。
    assert reverse(installment_id, "irev6", "sha256:ir6", tenant="t1").status_code == 200


def test_reversal_id_is_scoped_per_tenant_and_independent_of_payments() -> None:
    accept("ir7a", tenant="ta", amount=100)
    accept("ir7b", tenant="tb", amount=100)
    ia = register("ir7a", "irk7a", "irh7a", 100, tenant="ta").json()["installment_id"]
    ib = register("ir7b", "irk7b", "irh7b", 100, tenant="tb").json()["installment_id"]
    # 不同租户的相同冲正标识互不影响。
    assert reverse(ia, "irev7", "sha256:same", tenant="ta").status_code == 200
    assert reverse(ib, "irev7", "sha256:same", tenant="tb").status_code == 200


# ---------- 与整单收款共用未收口径 ----------

def test_installments_share_outstanding_with_whole_payments() -> None:
    accept("mix", amount=1000)
    whole = client.post("/orders/mix/payments", json={"amount_cents": 400}, headers={"X-Tenant": "t1"})
    assert whole.status_code == 200
    # 分期只能在整单收款后的剩余未收内登记。
    inst = register("mix", "mix-ik1", "mix-ih1", 600)
    assert inst.status_code == 200 and inst.json()["order"]["status"] == "settled"
    assert register("mix", "mix-ik2", "mix-ih2", 1).status_code == 409
    # 冲正整单收款：已收抵回 400，分期记录不受影响。
    rv = client.post(
        f"/payments/{whole.json()['payment_id']}/reversal",
        json={"reversal_id": "mix-prv", "request_fingerprint": "sha256:mixp"},
        headers={"X-Tenant": "t1"},
    )
    assert rv.status_code == 200
    order = rv.json()["order"]
    assert order["paid_cents"] == 600 and order["outstanding_cents"] == 400 and order["status"] == "accepted"
    assert client.get(f"/installments/{inst.json()['installment_id']}", headers={"X-Tenant": "t1"}).json()["status"] == "active"


# ---------- 并发 ----------

def test_concurrent_same_installment_key_only_one_takes_effect() -> None:
    url = _start_server()
    accept("ic1", amount=800)
    body = {"installment_key": "ikc1", "request_fingerprint": "ihc1", "amount_cents": 300}
    with httpx.Client(base_url=url, timeout=30) as http, ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(
                lambda _: http.post("/orders/ic1/installments", json=body, headers={"X-Tenant": "t1"}),
                range(8),
            )
        )
    assert all(r.status_code == 200 for r in responses)
    assert sum(1 for r in responses if r.headers.get("X-Idempotency-Replay") == "true") == 7
    assert len({r.text for r in responses}) == 1
    _assert_single_row_where("installments", "installment_key", "ikc1")
    order = client.get("/orders/ic1", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 300 and order["outstanding_cents"] == 500


def test_concurrent_same_key_different_fingerprints_exactly_one_wins() -> None:
    url = _start_server()
    accept("ic2", amount=1000)
    bodies = [
        {"installment_key": "ikc2", "request_fingerprint": f"ihc2-{i}", "amount_cents": 100 + i}
        for i in range(8)
    ]
    with httpx.Client(base_url=url, timeout=30) as http, ThreadPoolExecutor(max_workers=8) as pool:
        responses = [f.result() for f in [pool.submit(http.post, "/orders/ic2/installments", json=b, headers={"X-Tenant": "t1"}) for b in bodies]]
    assert sum(1 for r in responses if r.status_code == 200) == 1
    assert all(r.status_code in (200, 422) for r in responses)
    _assert_single_row_where("installments", "installment_key", "ikc2")


def test_concurrent_distinct_reversal_ids_on_same_installment_exactly_one_wins() -> None:
    url = _start_server()
    accept("ic3", amount=600)
    installment_id = register("ic3", "ikc3", "ihc3", 250).json()["installment_id"]

    def one(i: int) -> httpx.Response:
        with httpx.Client(base_url=url, timeout=30) as http:
            return http.post(
                f"/installments/{installment_id}/reversal",
                json={"reversal_id": f"irevc3-{i}", "request_fingerprint": f"sha256:{i}"},
                headers={"X-Tenant": "t1"},
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = [f.result() for f in [pool.submit(one, i) for i in range(8)]]
    assert sum(1 for r in responses if r.status_code == 200) == 1
    assert all(r.status_code in (200, 409) for r in responses)
    assert sum(1 for r in responses if r.status_code == 409) == 7
    _assert_single_row_where("installment_reversals", "installment_id", installment_id)
    order = client.get("/orders/ic3", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 600


def test_concurrent_same_reversal_id_only_one_takes_effect() -> None:
    url = _start_server()
    accept("ic4", amount=800)
    installment_id = register("ic4", "ikc4", "ihc4", 300).json()["installment_id"]
    body = {"reversal_id": "irevc4", "request_fingerprint": "sha256:ic4"}
    with httpx.Client(base_url=url, timeout=30) as http, ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(
                lambda _: http.post(
                    f"/installments/{installment_id}/reversal", json=body, headers={"X-Tenant": "t1"}
                ),
                range(8),
            )
        )
    assert all(r.status_code == 200 for r in responses)
    assert sum(1 for r in responses if r.headers.get("X-Idempotency-Replay") == "true") == 7
    assert len({r.text for r in responses}) == 1
    _assert_single_row_where("installment_reversals", "reversal_id", "irevc4")
    order = client.get("/orders/ic4", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 800


# ---------- 崩溃恢复 ----------

def test_crash_before_register_commit_then_retry_behaves_like_first() -> None:
    accept("icr", tenant="tc", amount=200)
    env = {**os.environ, "APP_CRASH_INSTALLMENT_BEFORE_COMMIT": "1"}
    code = (
        "from app.store.db import migrate; "
        "from app.store import installments; "
        "migrate(); "
        "installments.register_installment('tc', 'icr', 'ikcr', 'ihcrash', 200)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    # 提交前崩溃：分期记录不存在，订单未收不变。
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM installments WHERE tenant='tc' AND installment_key='ikcr'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()
    assert client.get("/orders/icr", headers={"X-Tenant": "tc"}).json()["status"] == "accepted"

    # 重启后用同一业务标识重试：按首次登记生效，而不是误判为重放。
    resp = register("icr", "ikcr", "ihcrash", 200, tenant="tc")
    assert resp.status_code == 200 and resp.headers.get("X-Idempotency-Replay") is None
    assert resp.json()["order"]["status"] == "settled"
    # 再发一次即为确定性重放。
    replay = register("icr", "ikcr", "ihcrash", 200, tenant="tc")
    assert replay.status_code == 200 and replay.headers["X-Idempotency-Replay"] == "true"


def test_crash_before_reversal_commit_then_retry_behaves_like_first() -> None:
    accept("icrr", tenant="tc", amount=200)
    installment_id = register("icrr", "ikcrr", "ihcrr", 200, tenant="tc").json()["installment_id"]
    env = {**os.environ, "APP_CRASH_INSTALLMENT_REVERSAL_BEFORE_COMMIT": "1"}
    code = (
        "from app.store.db import migrate; "
        "from app.store import installments; "
        "migrate(); "
        f"installments.reverse_installment('tc', '{installment_id}', 'irevcr', 'sha256:crash')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    # 提交前崩溃：冲正记录不存在，分期仍有效，订单仍结清。
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM installment_reversals WHERE tenant='tc' AND reversal_id='irevcr'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT status FROM installments WHERE tenant='tc' AND installment_id=?", (installment_id,)
        ).fetchone()["status"] == "active"
    finally:
        conn.close()
    assert client.get("/orders/icrr", headers={"X-Tenant": "tc"}).json()["status"] == "settled"

    # 重启后用同一冲正标识重试：按首次冲正生效，结论与未崩溃时一致。
    resp = reverse(installment_id, "irevcr", "sha256:crash", tenant="tc")
    assert resp.status_code == 200 and resp.headers.get("X-Idempotency-Replay") is None
    assert resp.json()["order"]["status"] == "accepted" and resp.json()["order"]["outstanding_cents"] == 200
    replay = reverse(installment_id, "irevcr", "sha256:crash", tenant="tc")
    assert replay.status_code == 200 and replay.headers["X-Idempotency-Replay"] == "true"


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


def _assert_single_row_where(table: str, column: str, value: str, *, tenant: str = "t1") -> None:
    conn = connect()
    try:
        count = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE tenant=? AND {column}=?", (tenant, value)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1
