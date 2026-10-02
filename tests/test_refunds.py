import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

H1 = {"X-Tenant": "t1"}
H2 = {"X-Tenant": "t2"}


def _make_order(tenant: str, order_id: str, amount: int = 1000, currency: str = "CNY") -> None:
    resp = client.post("/orders", json={"tenant": tenant, "order_id": order_id,
                                        "amount_cents": amount, "currency": currency})
    assert resp.status_code == 201, resp.text


def _refund(order_id: str, request_id: str, amount: int, headers=H1):
    return client.post(f"/orders/{order_id}/refunds",
                       json={"refund_request_id": request_id, "amount_cents": amount},
                       headers=headers)


def _pay(order_id: str, amount: int, headers=H1):
    return client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers=headers)


# ---------- 受理 / 查询 / 币种沿用 ----------

def test_refund_accepted_against_paid_order() -> None:
    _make_order("t1", "r1", 1000)
    assert _pay("r1", 700).status_code == 200
    resp = _refund("r1", "req-1", 300)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["currency"] == "CNY"  # 币种沿用原订单
    assert body["order_id"] == "r1"
    assert body["refund_request_id"] == "req-1"
    # 受理即占用未决退款，但尚未冲减已收
    order = client.get("/orders/r1", headers=H1).json()
    assert order["paid_cents"] == 700
    assert order["refunded_cents"] == 0
    assert order["pending_refund_cents"] == 300
    assert order["net_cents"] == 700
    assert order["outstanding_cents"] == 300


def test_refund_on_nonexistent_order_is_404() -> None:
    resp = _refund("missing", "req-x", 10)
    assert resp.status_code == 404
    assert client.get("/refunds/req-x", headers=H1).status_code == 404


def test_refund_requires_positive_amount() -> None:
    _make_order("t1", "r2", 1000)
    _pay("r2", 500)
    resp = client.post("/orders/r2/refunds",
                       json={"refund_request_id": "req-bad", "amount_cents": 0}, headers=H1)
    assert resp.status_code == 422


def test_currency_inherited_from_order() -> None:
    _make_order("t1", "r2b", 500, currency="USD")
    _pay("r2b", 500)
    body = _refund("r2b", "req-usd", 200).json()
    assert body["currency"] == "USD"


# ---------- 金额守恒与未决占额 ----------

def test_pending_refund_blocks_excess_refund() -> None:
    _make_order("t1", "r3", 1000)
    _pay("r3", 500)
    assert _refund("r3", "req-a", 400).status_code == 201
    # 未决占额 400 后只剩 100 可退
    resp = _refund("r3", "req-b", 200)
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "amount_exceeds_paid"
    # 被整体拒绝：不留下未决占额，也不产生 accepted 单据
    order = client.get("/orders/r3", headers=H1).json()
    assert order["pending_refund_cents"] == 400
    rejected = client.get("/refunds/req-b", headers=H1).json()
    assert rejected["status"] == "rejected"
    assert rejected["rejection_reason"] == "amount_exceeds_paid"


def test_refund_cannot_exceed_paid_before_any_payment() -> None:
    _make_order("t1", "r4", 1000)
    resp = _refund("r4", "req-c", 1)
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "amount_exceeds_paid"


# ---------- 完成：冲减净额并驱动订单状态 ----------

def test_complete_refund_updates_net_and_status() -> None:
    _make_order("t1", "r5", 1000)
    _pay("r5", 1000)
    order = client.get("/orders/r5", headers=H1).json()
    assert order["status"] == "settled"

    _refund("r5", "req-full", 1000)
    done = client.post("/refunds/req-full/complete", headers=H1)
    assert done.status_code == 200 and done.json()["status"] == "completed"

    order = client.get("/orders/r5", headers=H1).json()
    assert order["paid_cents"] == 1000          # 累计已收不回改
    assert order["refunded_cents"] == 1000
    assert order["pending_refund_cents"] == 0
    assert order["net_cents"] == 0
    assert order["outstanding_cents"] == 0
    assert order["status"] == "unsettled"       # 净额为零且曾收款 → 未结清
    # 未收 = 金额 − 已收 = 0，全退后不能再补收（守恒式口径）
    assert _pay("r5", 1).status_code == 409


def test_partial_refund_keeps_order_unsettled() -> None:
    _make_order("t1", "r6", 1000)
    _pay("r6", 1000)
    _refund("r6", "req-p", 400)
    client.post("/refunds/req-p/complete", headers=H1)
    order = client.get("/orders/r6", headers=H1).json()
    assert order["net_cents"] == 600 and order["status"] == "unsettled"


# ---------- 拒绝：释放占额、原因可区分 ----------

def test_reject_refund_releases_hold_with_distinct_reason() -> None:
    _make_order("t1", "r7", 1000)
    _pay("r7", 500)
    _refund("r7", "req-r", 300)
    resp = client.post("/refunds/req-r/reject", json={"reason": "fraud_check_failed"}, headers=H1)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert resp.json()["rejection_reason"] == "fraud_check_failed"

    order = client.get("/orders/r7", headers=H1).json()
    assert order["pending_refund_cents"] == 0
    assert order["net_cents"] == 500
    # 释放后额度恢复，可再退
    assert _refund("r7", "req-r2", 500).status_code == 201


def test_reject_without_body_uses_default_reason() -> None:
    _make_order("t1", "r7b", 1000)
    _pay("r7b", 100)
    _refund("r7b", "req-rb", 100)
    resp = client.post("/refunds/req-rb/reject", headers=H1)
    assert resp.json()["rejection_reason"] == "operator_rejected"


def test_complete_or_reject_terminal_refund_is_conflict() -> None:
    _make_order("t1", "r8", 1000)
    _pay("r8", 100)
    _refund("r8", "req-t", 100)
    client.post("/refunds/req-t/complete", headers=H1)
    again = client.post("/refunds/req-t/complete", headers=H1)
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "invalid_refund_status"
    reject = client.post("/refunds/req-t/reject", headers=H1)
    assert reject.status_code == 409
    # 既有单据金额与状态不被改变
    assert client.get("/refunds/req-t", headers=H1).json()["status"] == "completed"


# ---------- 幂等：请求标识与订单标识分离 ----------

def test_duplicate_request_id_replays_first_result() -> None:
    _make_order("t1", "r9", 1000)
    _pay("r9", 800)
    first = _refund("r9", "req-idem", 300)
    assert first.status_code == 201
    second = _refund("r9", "req-idem", 300)
    assert second.status_code == 200  # 重放而非重复受理
    assert second.json()["created_at"] == first.json()["created_at"]
    order = client.get("/orders/r9", headers=H1).json()
    assert order["pending_refund_cents"] == 300  # 占额只记一次


def test_same_request_id_different_content_is_conflict() -> None:
    _make_order("t1", "r10a", 1000)
    _make_order("t1", "r10b", 1000)
    _pay("r10a", 500)
    _pay("r10b", 500)
    assert _refund("r10a", "req-dup", 100).status_code == 201
    # 同标识指向不同订单
    diff_order = _refund("r10b", "req-dup", 100)
    assert diff_order.status_code == 409
    assert diff_order.json()["detail"]["reason"] == "request_conflict"
    # 同标识不同金额
    diff_amount = _refund("r10a", "req-dup", 200)
    assert diff_amount.status_code == 409
    assert diff_amount.json()["detail"]["reason"] == "request_conflict"
    # 首次单据未被改写，金额仍是 100、订单仍是 r10a
    first = client.get("/refunds/req-dup", headers=H1).json()
    assert first["order_id"] == "r10a" and first["amount_cents"] == 100
    order = client.get("/orders/r10a", headers=H1).json()
    assert order["pending_refund_cents"] == 100


def test_request_id_is_distinct_from_order_id() -> None:
    _make_order("t1", "r11", 1000)
    _pay("r11", 300)
    # 同一订单上两个不同请求标识产生两张退款单
    assert _refund("r11", "req-r11-1", 100).status_code == 201
    assert _refund("r11", "req-r11-2", 100).status_code == 201


def test_rejected_request_id_replay_is_stable() -> None:
    _make_order("t1", "r12", 1000)
    first = _refund("r12", "req-nofund", 10)
    assert first.status_code == 409
    again = _refund("r12", "req-nofund", 10)
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "amount_exceeds_paid"
    doc = client.get("/refunds/req-nofund", headers=H1).json()
    assert doc["status"] == "rejected" and doc["rejection_reason"] == "amount_exceeds_paid"


# ---------- 租户隔离 ----------

def test_refund_is_tenant_isolated() -> None:
    _make_order("t1", "r13", 1000)
    _pay("r13", 500)
    _refund("r13", "req-iso", 200)
    # 跨租户：订单按不存在处理，不泄漏
    assert _refund("r13", "req-iso-x", 10, headers=H2).status_code == 404
    # 跨租户查退款单同样 404
    assert client.get("/refunds/req-iso", headers=H2).status_code == 404
    # 不同租户可复用同一请求标识，互不干扰
    _make_order("t2", "r13", 2000)
    _pay("r13", 2000, headers=H2)
    other = _refund("r13", "req-iso", 999, headers=H2)
    assert other.status_code == 201 and other.json()["amount_cents"] == 999
    # t1 的首次单据不受影响
    assert client.get("/refunds/req-iso", headers=H1).json()["amount_cents"] == 200


def test_cross_tenant_refund_listing_is_404() -> None:
    _make_order("t1", "r14", 1000)
    assert client.get("/orders/r14/refunds", headers=H2).status_code == 404


def test_missing_tenant_header_is_400() -> None:
    _make_order("t1", "r15", 1000)
    assert client.post("/orders/r15/refunds",
                       json={"refund_request_id": "z", "amount_cents": 1}).status_code == 400
    assert client.get("/refunds/z").status_code == 400


# ---------- 订单退款汇总列表 ----------

def test_order_refunds_summary() -> None:
    _make_order("t1", "r16", 1000)
    _pay("r16", 900)
    _refund("r16", "req-s1", 400)
    client.post("/refunds/req-s1/complete", headers=H1)
    _refund("r16", "req-s2", 200)
    client.post("/refunds/req-s2/reject", headers=H1)
    _refund("r16", "req-s3", 300)

    resp = client.get("/orders/r16/refunds", headers=H1)
    assert resp.status_code == 200
    body = resp.json()
    assert body["order"]["net_cents"] == 500
    assert body["order"]["refunded_cents"] == 400
    assert body["order"]["pending_refund_cents"] == 300
    statuses = {r["refund_request_id"]: r["status"] for r in body["refunds"]}
    assert statuses == {"req-s1": "completed", "req-s2": "rejected", "req-s3": "accepted"}


# ---------- 收退交替短序列：守恒恒成立且可重放 ----------

def test_interleaved_pay_refund_sequence_keeps_invariant() -> None:
    _make_order("t1", "r17", 1000)
    _pay("r17", 600)
    _refund("r17", "seq-1", 600)
    client.post("/refunds/seq-1/complete", headers=H1)
    # 全退完后收款仍可继续，订单金额约束不受影响
    assert _pay("r17", 300).status_code == 200
    _refund("r17", "seq-2", 200)
    # 未决 200 + 已退 600：再退 200 会使已退（含未决）超过已收 900
    over = _refund("r17", "seq-3", 200)
    assert over.status_code == 409 and over.json()["detail"]["reason"] == "amount_exceeds_paid"
    client.post("/refunds/seq-2/complete", headers=H1)
    order = client.get("/orders/r17", headers=H1).json()
    # 不变量：已退 <= 已收；未收 = 金额 − 已收；净额 = 已收 − 已退
    assert order["refunded_cents"] <= order["paid_cents"]
    assert order["outstanding_cents"] == order["amount_cents"] - order["paid_cents"]
    assert order["net_cents"] == order["paid_cents"] - order["refunded_cents"]
    assert order["paid_cents"] == 900 and order["refunded_cents"] == 800
    assert order["net_cents"] == 100 and order["pending_refund_cents"] == 0


def test_failed_attempt_does_not_mutate_existing_state() -> None:
    _make_order("t1", "r18", 1000)
    _pay("r18", 100)
    before = client.get("/orders/r18", headers=H1).json()
    over = _refund("r18", "req-over", 500)
    assert over.status_code == 409
    after = client.get("/orders/r18", headers=H1).json()
    for key in ("paid_cents", "refunded_cents", "pending_refund_cents", "status"):
        assert before[key] == after[key]


# ---------- 并发提交同一请求标识 ----------

def test_concurrent_same_request_id_accepted_once() -> None:
    import threading
    _make_order("t1", "r19", 1000)
    _pay("r19", 800)
    results: list[int] = []
    lock = threading.Lock()

    def submit() -> None:
        resp = _refund("r19", "req-conc", 300)
        with lock:
            results.append(resp.status_code)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一次 201，其余全部重放 200，无冲突、无重复占额
    assert sorted(results) == [200] * 7 + [201], results
    order = client.get("/orders/r19", headers=H1).json()
    assert order["pending_refund_cents"] == 300
