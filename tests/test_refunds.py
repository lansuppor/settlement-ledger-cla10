import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_refunds.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "t1"}

def _order(oid: str, amount: int = 1000, tenant: str = "t1") -> None:
    resp = client.post("/orders", json={"tenant": tenant, "order_id": oid,
                                        "amount_cents": amount, "currency": "CNY"})
    assert resp.status_code == 201, resp.text

def _refund(oid: str, rid_req: str, amount: int, headers: dict | None = None, status: int = 201):
    resp = client.post(f"/orders/{oid}/refunds",
                       json={"refund_request_id": rid_req, "amount_cents": amount},
                       headers=headers if headers is not None else H)
    assert resp.status_code == status, resp.text
    return resp.json()

def test_refund_acceptance_holds_amount_and_order_summary() -> None:
    _order("r1", 1000)
    assert client.post("/orders/r1/payments", json={"amount_cents": 800}, headers=H).status_code == 200
    refund = _refund("r1", "req-1", 300)
    assert refund["status"] == "accepted"
    assert refund["currency"] == "CNY"
    assert refund["order_id"] == "r1"
    assert refund["refund_request_id"] == "req-1"
    assert refund["refund_id"] != "req-1"  # business id must stay separate from request id

    order = client.get("/orders/r1", headers=H).json()
    assert order["paid_cents"] == 800
    assert order["refunded_cents"] == 0          # completion is what reduces paid money
    assert order["pending_refund_cents"] == 300  # acceptance only holds the amount
    assert order["net_cents"] == 800
    assert order["outstanding_cents"] == 200

def test_refund_on_unknown_or_foreign_order_is_404() -> None:
    resp = client.post("/orders/missing/refunds",
                       json={"refund_request_id": "req-x", "amount_cents": 10}, headers=H)
    assert resp.status_code == 404

    _order("r-foreign", 100, tenant="t1")
    resp = client.post("/orders/r-foreign/refunds",
                       json={"refund_request_id": "req-y", "amount_cents": 10},
                       headers={"X-Tenant": "t2"})
    assert resp.status_code == 404  # cross tenant must look like missing

def test_over_refund_is_rejected_without_half_state() -> None:
    _order("r2", 500)
    client.post("/orders/r2/payments", json={"amount_cents": 300}, headers=H)
    resp = client.post("/orders/r2/refunds",
                       json={"refund_request_id": "req-big", "amount_cents": 301}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "amount_exceeds"

    order = client.get("/orders/r2", headers=H).json()
    assert order["pending_refund_cents"] == 0
    assert order["refunded_cents"] == 0
    assert client.get("/orders/r2/refunds", headers=H).json() == []

def test_unpaid_order_cannot_be_refunded() -> None:
    _order("r3", 500)
    resp = client.post("/orders/r3/refunds",
                       json={"refund_request_id": "req-nopaid", "amount_cents": 1}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "amount_exceeds"

def test_rejected_order_cannot_be_refunded() -> None:
    _order("r4", 500)
    conn = connect()
    try:
        conn.execute("UPDATE orders SET status='rejected' WHERE tenant='t1' AND order_id='r4'")
    finally:
        conn.close()
    resp = client.post("/orders/r4/refunds",
                       json={"refund_request_id": "req-ro", "amount_cents": 1}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "order_state"

def test_idempotent_replay_returns_first_acceptance() -> None:
    _order("r5", 500)
    client.post("/orders/r5/payments", json={"amount_cents": 500}, headers=H)
    first = _refund("r5", "req-dup", 100)
    replay = _refund("r5", "req-dup", 100, status=200)
    assert replay["refund_id"] == first["refund_id"]
    order = client.get("/orders/r5", headers=H).json()
    assert order["pending_refund_cents"] == 100  # held exactly once

def test_same_request_id_with_different_content_conflicts() -> None:
    _order("r6a", 500)
    _order("r6b", 500)
    client.post("/orders/r6a/payments", json={"amount_cents": 500}, headers=H)
    client.post("/orders/r6b/payments", json={"amount_cents": 500}, headers=H)
    first = _refund("r6a", "req-c", 100)

    # Same request id, different amount -> conflict, first document untouched.
    resp = client.post("/orders/r6a/refunds",
                       json={"refund_request_id": "req-c", "amount_cents": 200}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "request_conflict"
    assert resp.json()["detail"]["refund"]["refund_id"] == first["refund_id"]

    # Same request id pointed at a different order -> conflict as well.
    resp = client.post("/orders/r6b/refunds",
                       json={"refund_request_id": "req-c", "amount_cents": 100}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "request_conflict"

    order = client.get("/orders/r6a", headers=H).json()
    assert order["pending_refund_cents"] == 100  # nothing rewritten

def test_pending_hold_blocks_further_refunds_until_rejected() -> None:
    _order("r7", 300)
    client.post("/orders/r7/payments", json={"amount_cents": 300}, headers=H)
    held = _refund("r7", "req-hold", 200)
    # Only 100 remains available while 200 is pending.
    assert client.post("/orders/r7/refunds",
                       json={"refund_request_id": "req-hold2", "amount_cents": 101},
                       headers=H).status_code == 409
    assert client.post(f"/refunds/{held['refund_id']}/reject",
                       json={"reason_code": "internal_error"}, headers=H).status_code == 200
    # Hold released: the 200 becomes available again.
    second = _refund("r7", "req-hold2", 200)
    assert second["status"] == "accepted"
    rejected = client.get(f"/refunds/{held['refund_id']}", headers=H).json()
    assert rejected["status"] == "rejected"
    assert rejected["reason_code"] == "internal_error"

def test_complete_moves_money_and_drives_order_status() -> None:
    _order("r8", 500)
    client.post("/orders/r8/payments", json={"amount_cents": 500}, headers=H)
    order = client.get("/orders/r8", headers=H).json()
    assert order["status"] == "settled"

    refund = _refund("r8", "req-full", 500)
    done = client.post(f"/refunds/{refund['refund_id']}/complete", headers=H)
    assert done.status_code == 200 and done.json()["status"] == "completed"

    order = client.get("/orders/r8", headers=H).json()
    assert order["paid_cents"] == 500
    assert order["refunded_cents"] == 500
    assert order["pending_refund_cents"] == 0
    assert order["net_cents"] == 0
    assert order["outstanding_cents"] == 0
    assert order["status"] == "open"  # net 0 after payment -> unsettled again

    # Complete again is an idempotent replay, not an error.
    again = client.post(f"/refunds/{refund['refund_id']}/complete", headers=H)
    assert again.status_code == 200 and again.json()["status"] == "completed"
    assert client.get("/orders/r8", headers=H).json()["refunded_cents"] == 500

def test_partial_completion_leaves_order_partially_settled() -> None:
    _order("r9", 500)
    client.post("/orders/r9/payments", json={"amount_cents": 500}, headers=H)
    refund = _refund("r9", "req-part", 200)
    client.post(f"/refunds/{refund['refund_id']}/complete", headers=H)
    order = client.get("/orders/r9", headers=H).json()
    assert order["status"] == "accepted"
    assert order["net_cents"] == 300

def test_terminal_transitions_are_refused() -> None:
    _order("r10", 200)
    client.post("/orders/r10/payments", json={"amount_cents": 200}, headers=H)
    refund = _refund("r10", "req-term", 50)
    client.post(f"/refunds/{refund['refund_id']}/complete", headers=H)
    resp = client.post(f"/refunds/{refund['refund_id']}/reject",
                       json={"reason_code": "order_state"}, headers=H)
    assert resp.status_code == 409

def test_reject_requires_known_reason() -> None:
    _order("r11", 200)
    client.post("/orders/r11/payments", json={"amount_cents": 200}, headers=H)
    refund = _refund("r11", "req-reason", 50)
    resp = client.post(f"/refunds/{refund['refund_id']}/reject",
                       json={"reason_code": "bogus"}, headers=H)
    assert resp.status_code == 400

def test_cross_tenant_refund_read_is_404() -> None:
    _order("r12", 200)
    client.post("/orders/r12/payments", json={"amount_cents": 200}, headers=H)
    refund = _refund("r12", "req-iso", 50)
    assert client.get(f"/refunds/{refund['refund_id']}", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/orders/r12/refunds", headers={"X-Tenant": "t2"}).status_code == 404
    # Foreign tenant can reuse the same request id freely.
    other = client.post("/orders/r12/refunds",
                        json={"refund_request_id": "req-iso", "amount_cents": 10},
                        headers={"X-Tenant": "t2"})
    # r12 does not exist for t2, so this is still 404, no leak.
    assert other.status_code == 404

def test_concurrent_same_request_id_accepted_once() -> None:
    _order("r13", 1000)
    client.post("/orders/r13/payments", json={"amount_cents": 1000}, headers=H)

    def submit(_: int) -> tuple[int, str]:
        resp = client.post("/orders/r13/refunds",
                           json={"refund_request_id": "req-race", "amount_cents": 100},
                           headers=H)
        return resp.status_code, resp.json()["refund_id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    statuses = sorted(code for code, _ in results)
    assert statuses == [200] * 7 + [201]
    assert len({rid for _, rid in results}) == 1
    order = client.get("/orders/r13", headers=H).json()
    assert order["pending_refund_cents"] == 100  # held exactly once

def test_payment_refund_interleaved_sequence_is_replayable() -> None:
    _order("r14", 1000)
    pay = lambda a: client.post("/orders/r14/payments", json={"amount_cents": a}, headers=H)
    summary = lambda: client.get("/orders/r14", headers=H).json()

    assert pay(1000).status_code == 200
    assert summary()["status"] == "settled"  # net == amount

    r1 = _refund("r14", "seq-1", 400)
    assert client.post(f"/refunds/{r1['refund_id']}/complete", headers=H).status_code == 200
    s = summary()
    assert (s["paid_cents"], s["refunded_cents"], s["net_cents"]) == (1000, 400, 600)
    assert s["status"] == "accepted"

    # 400 already refunded, so another 700 would cross cumulative paid money.
    assert client.post("/orders/r14/refunds",
                       json={"refund_request_id": "seq-over", "amount_cents": 700},
                       headers=H).status_code == 409
    s = summary()  # refusal left no half-state
    assert (s["refunded_cents"], s["pending_refund_cents"], s["net_cents"]) == (400, 0, 600)

    r2 = _refund("r14", "seq-2", 600)  # exact remaining refundable amount
    assert client.post(f"/refunds/{r2['refund_id']}/reject",
                       json={"reason_code": "internal_error"}, headers=H).status_code == 200
    assert summary()["pending_refund_cents"] == 0

    r3 = _refund("r14", "seq-3", 600)
    client.post(f"/refunds/{r3['refund_id']}/complete", headers=H)
    s = summary()
    assert (s["paid_cents"], s["refunded_cents"], s["pending_refund_cents"]) == (1000, 1000, 0)
    assert s["net_cents"] == 0 and s["status"] == "open"

    # Replaying earlier requests returns the same documents and conclusions.
    assert _refund("r14", "seq-1", 400, status=200)["refund_id"] == r1["refund_id"]
    assert _refund("r14", "seq-3", 600, status=200)["refund_id"] == r3["refund_id"]
    ids = {r["refund_id"] for r in client.get("/orders/r14/refunds", headers=H).json()}
    assert ids == {r1["refund_id"], r2["refund_id"], r3["refund_id"]}
