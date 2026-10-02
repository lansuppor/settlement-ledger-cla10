import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_supplements.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "t1"}

def _order(oid: str, amount: int = 1000, tenant: str = "t1") -> None:
    resp = client.post("/orders", json={"tenant": tenant, "order_id": oid,
                                        "amount_cents": amount, "currency": "CNY"})
    assert resp.status_code == 201, resp.text

def _pay(oid: str, amount: int) -> None:
    resp = client.post(f"/orders/{oid}/payments", json={"amount_cents": amount}, headers=H)
    assert resp.status_code == 200, resp.text

def _refund(oid: str, rid_req: str, amount: int) -> dict:
    resp = client.post(f"/orders/{oid}/refunds",
                       json={"refund_request_id": rid_req, "amount_cents": amount}, headers=H)
    assert resp.status_code == 201, resp.text
    return resp.json()

def _return(rid: str, reason: str = "missing_proof", note: str = "请补充凭证",
            headers: dict | None = None, status: int = 201) -> dict:
    resp = client.post(f"/refunds/{rid}/return",
                       json={"reason_code": reason, "note": note},
                       headers=headers if headers is not None else H)
    assert resp.status_code == status, resp.text
    return resp.json()

def _supplements(rid: str) -> list[dict]:
    resp = client.get(f"/refunds/{rid}/supplements", headers=H)
    assert resp.status_code == 200, resp.text
    return resp.json()

def test_return_for_supplement_keeps_hold_and_blocks_complete() -> None:
    _order("s1", 1000)
    _pay("s1", 800)
    refund = _refund("s1", "sreq-1", 300)

    returned = _return(refund["refund_id"])
    assert returned["status"] == "awaiting_supplement"
    assert returned["amount_cents"] == 300  # hold unchanged while waiting

    order = client.get("/orders/s1", headers=H).json()
    assert order["pending_refund_cents"] == 300
    assert order["refunded_cents"] == 0
    assert order["net_cents"] == 800

    # Completion is refused while awaiting supplement; nothing changes.
    resp = client.post(f"/refunds/{refund['refund_id']}/complete", headers=H)
    assert resp.status_code == 409
    order = client.get("/orders/s1", headers=H).json()
    assert order["pending_refund_cents"] == 300
    assert client.get(f"/refunds/{refund['refund_id']}", headers=H).json()["status"] == \
        "awaiting_supplement"

def test_return_replay_and_conflicting_content() -> None:
    _order("s2", 1000)
    _pay("s2", 500)
    refund = _refund("s2", "sreq-2", 200)
    _return(refund["refund_id"], "wrong_account", "账号有误")

    # Same reason and note: replay of the first return, no new record.
    replay = _return(refund["refund_id"], "wrong_account", "账号有误", status=200)
    assert replay["status"] == "awaiting_supplement"
    records = _supplements(refund["refund_id"])
    assert len(records) == 1
    assert records[0]["kind"] == "returned"
    assert records[0]["reason_code"] == "wrong_account"
    assert records[0]["note"] == "账号有误"

    # Different reason or note: 409, first record not rewritten.
    resp = client.post(f"/refunds/{refund['refund_id']}/return",
                       json={"reason_code": "missing_proof", "note": "账号有误"}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "request_conflict"
    resp = client.post(f"/refunds/{refund['refund_id']}/return",
                       json={"reason_code": "wrong_account", "note": "别的说明"}, headers=H)
    assert resp.status_code == 409
    records = _supplements(refund["refund_id"])
    assert len(records) == 1
    assert records[0]["reason_code"] == "wrong_account"
    assert records[0]["note"] == "账号有误"

def test_return_validation_and_unknown_refund() -> None:
    _order("s3", 1000)
    _pay("s3", 500)
    refund = _refund("s3", "sreq-3", 100)
    resp = client.post(f"/refunds/{refund['refund_id']}/return",
                       json={"reason_code": "bogus", "note": "x"}, headers=H)
    assert resp.status_code == 400
    resp = client.post(f"/refunds/{refund['refund_id']}/return",
                       json={"reason_code": "missing_proof", "note": ""}, headers=H)
    assert resp.status_code == 422
    assert client.post("/refunds/missing/return",
                       json={"reason_code": "missing_proof", "note": "x"},
                       headers=H).status_code == 404
    # Cross-tenant looks like missing.
    assert client.post(f"/refunds/{refund['refund_id']}/return",
                       json={"reason_code": "missing_proof", "note": "x"},
                       headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get(f"/refunds/{refund['refund_id']}/supplements",
                      headers={"X-Tenant": "t2"}).status_code == 404

def test_terminal_refunds_cannot_be_returned() -> None:
    _order("s4", 500)
    _pay("s4", 500)
    done = _refund("s4", "sreq-4a", 100)
    client.post(f"/refunds/{done['refund_id']}/complete", headers=H)
    resp = client.post(f"/refunds/{done['refund_id']}/return",
                       json={"reason_code": "missing_proof", "note": "x"}, headers=H)
    assert resp.status_code == 409

    rejected = _refund("s4", "sreq-4b", 100)
    client.post(f"/refunds/{rejected['refund_id']}/reject",
                json={"reason_code": "internal_error"}, headers=H)
    resp = client.post(f"/refunds/{rejected['refund_id']}/return",
                       json={"reason_code": "missing_proof", "note": "x"}, headers=H)
    assert resp.status_code == 409

def test_supplement_recomputes_hold_and_returns_to_accepted() -> None:
    _order("s5", 1000)
    _pay("s5", 1000)
    refund = _refund("s5", "sreq-5", 400)
    _return(refund["refund_id"], "amount_mismatch", "金额对不上")

    # Smaller amount: excess hold released immediately, back to accepted.
    resp = client.post(f"/refunds/{refund['refund_id']}/supplement",
                       json={"amount_cents": 250}, headers=H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["amount_cents"] == 250
    order = client.get("/orders/s5", headers=H).json()
    assert order["pending_refund_cents"] == 250
    assert order["refunded_cents"] == 0
    assert order["net_cents"] == 1000
    assert order["status"] == "settled"

    records = _supplements(refund["refund_id"])
    assert [r["kind"] for r in records] == ["supplemented", "returned"]  # newest first
    assert records[0]["amount_before_cents"] == 400
    assert records[0]["amount_after_cents"] == 250
    assert records[0]["reason_code"] == "amount_mismatch"
    assert records[0]["note"] == "金额对不上"
    assert records[1]["amount_before_cents"] == 400
    assert records[1]["amount_after_cents"] == 400
    assert all(r["created_at"] for r in records)

    # The recomputed hold is what completes into refunded_cents.
    assert client.post(f"/refunds/{refund['refund_id']}/complete", headers=H).status_code == 200
    order = client.get("/orders/s5", headers=H).json()
    assert order["refunded_cents"] == 250
    assert order["pending_refund_cents"] == 0
    assert order["net_cents"] == 750

def test_supplement_conservation_counts_own_hold() -> None:
    _order("s6", 1000)
    _pay("s6", 1000)
    first = _refund("s6", "sreq-6a", 300)
    _refund("s6", "sreq-6b", 400)  # another pending refund on the same order
    _return(first["refund_id"])

    # refunded(0) + pending(700) + new must not exceed paid(1000): 301 is too much.
    resp = client.post(f"/refunds/{first['refund_id']}/supplement",
                       json={"amount_cents": 301}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "amount_exceeds"
    # Failure leaves no half-state.
    refund = client.get(f"/refunds/{first['refund_id']}", headers=H).json()
    assert refund["status"] == "awaiting_supplement"
    assert refund["amount_cents"] == 300
    order = client.get("/orders/s6", headers=H).json()
    assert order["pending_refund_cents"] == 700
    assert _supplements(first["refund_id"])[0]["kind"] == "returned"

    # Exactly 300 fits (0 + 700 + 300 == 1000).
    resp = client.post(f"/refunds/{first['refund_id']}/supplement",
                       json={"amount_cents": 300}, headers=H)
    assert resp.status_code == 200
    order = client.get("/orders/s6", headers=H).json()
    assert order["pending_refund_cents"] == 700

def test_supplement_requires_awaiting_state_and_positive_amount() -> None:
    _order("s7", 500)
    _pay("s7", 500)
    refund = _refund("s7", "sreq-7", 100)
    # Never returned: cannot supplement.
    resp = client.post(f"/refunds/{refund['refund_id']}/supplement",
                       json={"amount_cents": 100}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "refund_state"
    assert client.post("/refunds/missing/supplement",
                       json={"amount_cents": 1}, headers=H).status_code == 404

    _return(refund["refund_id"])
    assert client.post(f"/refunds/{refund['refund_id']}/supplement",
                       json={"amount_cents": 0}, headers=H).status_code == 422
    done = client.post(f"/refunds/{refund['refund_id']}/supplement",
                       json={"amount_cents": 150}, headers=H)
    assert done.status_code == 200

    # Replaying the same supplement returns the same result, exactly once applied.
    replay = client.post(f"/refunds/{refund['refund_id']}/supplement",
                         json={"amount_cents": 150}, headers=H)
    assert replay.status_code == 200
    assert replay.json()["amount_cents"] == 150
    order = client.get("/orders/s7", headers=H).json()
    assert order["pending_refund_cents"] == 150
    # A different amount after the supplement is a conflict, not a rewrite.
    resp = client.post(f"/refunds/{refund['refund_id']}/supplement",
                       json={"amount_cents": 200}, headers=H)
    assert resp.status_code == 409
    assert client.get("/orders/s7", headers=H).json()["pending_refund_cents"] == 150

def test_awaiting_supplement_can_be_rejected() -> None:
    _order("s8", 500)
    _pay("s8", 500)
    refund = _refund("s8", "sreq-8", 200)
    _return(refund["refund_id"])
    resp = client.post(f"/refunds/{refund['refund_id']}/reject",
                       json={"reason_code": "order_state"}, headers=H)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["reason_code"] == "order_state"
    assert client.get("/orders/s8", headers=H).json()["pending_refund_cents"] == 0

def test_return_after_supplement_starts_a_new_episode() -> None:
    _order("s9", 1000)
    _pay("s9", 1000)
    refund = _refund("s9", "sreq-9", 300)
    _return(refund["refund_id"], "missing_proof", "第一次退回")
    client.post(f"/refunds/{refund['refund_id']}/supplement",
                json={"amount_cents": 300}, headers=H)
    # Back to accepted: a fresh return with new content is allowed.
    _return(refund["refund_id"], "wrong_account", "第二次退回")
    replay = _return(refund["refund_id"], "wrong_account", "第二次退回", status=200)
    assert replay["status"] == "awaiting_supplement"
    resp = client.post(f"/refunds/{refund['refund_id']}/return",
                       json={"reason_code": "missing_proof", "note": "第一次退回"}, headers=H)
    assert resp.status_code == 409  # conflicts with the current episode's first return
    records = _supplements(refund["refund_id"])
    assert [r["kind"] for r in records] == ["returned", "supplemented", "returned"]

def test_concurrent_returns_take_effect_once() -> None:
    _order("s10", 1000)
    _pay("s10", 1000)
    refund = _refund("s10", "sreq-10", 300)

    def submit(_: int) -> int:
        resp = client.post(f"/refunds/{refund['refund_id']}/return",
                           json={"reason_code": "missing_proof", "note": "补件"}, headers=H)
        return resp.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = sorted(pool.map(submit, range(8)))
    assert statuses == [200] * 7 + [201]
    assert len(_supplements(refund["refund_id"])) == 1
    assert client.get(f"/refunds/{refund['refund_id']}", headers=H).json()["status"] == \
        "awaiting_supplement"

def test_concurrent_supplements_take_effect_once() -> None:
    _order("s11", 1000)
    _pay("s11", 1000)
    refund = _refund("s11", "sreq-11", 300)
    _return(refund["refund_id"])

    def submit(_: int) -> int:
        resp = client.post(f"/refunds/{refund['refund_id']}/supplement",
                           json={"amount_cents": 200}, headers=H)
        return resp.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(submit, range(8)))
    assert statuses == [200] * 8  # replays all agree with the first
    order = client.get("/orders/s11", headers=H).json()
    assert order["pending_refund_cents"] == 200  # recomputed exactly once
    assert len([r for r in _supplements(refund["refund_id"])
                if r["kind"] == "supplemented"]) == 1
