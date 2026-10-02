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
    assert client.post(f"/orders/{oid}/payments", json={"amount_cents": amount},
                       headers=H).status_code == 200

def _refund(oid: str, rid_req: str, amount: int) -> dict:
    resp = client.post(f"/orders/{oid}/refunds",
                       json={"refund_request_id": rid_req, "amount_cents": amount}, headers=H)
    assert resp.status_code == 201, resp.text
    return resp.json()

def _return(rid: str, reason: str = "missing_proof", note: str = "proof missing",
            status: int = 201) -> dict:
    resp = client.post(f"/refunds/{rid}/supplement-return",
                       json={"reason_code": reason, "note": note}, headers=H)
    assert resp.status_code == status, resp.text
    return resp.json()

def _supplement(rid: str, amount: int, status: int = 201) -> dict:
    resp = client.post(f"/refunds/{rid}/supplement",
                       json={"amount_cents": amount}, headers=H)
    assert resp.status_code == status, resp.text
    return resp.json()

def test_return_moves_refund_to_awaiting_and_writes_record() -> None:
    _order("s1", 1000)
    _pay("s1", 800)
    refund = _refund("s1", "req-s1", 300)
    rid = refund["refund_id"]

    out = _return(rid, note="receipt unreadable")
    assert out["status"] == "awaiting_supplement"
    assert out["amount_cents"] == 300
    assert out["held_cents"] == 300  # hold stays while waiting

    events = client.get(f"/refunds/{rid}/supplements", headers=H).json()
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "returned"
    assert ev["reason_code"] == "missing_proof"
    assert ev["note"] == "receipt unreadable"
    assert ev["amount_before_cents"] == 300
    assert ev["amount_after_cents"] == 300
    assert ev["created_at"]

    # Order books do not move on a return.
    order = client.get("/orders/s1", headers=H).json()
    assert order["pending_refund_cents"] == 300
    assert order["refunded_cents"] == 0
    assert order["net_cents"] == 800

def test_return_replay_with_same_reason_and_note_is_idempotent() -> None:
    _order("s2", 1000)
    _pay("s2", 1000)
    rid = _refund("s2", "req-s2", 200)["refund_id"]
    first = _return(rid, "wrong_account", "fix account")
    replay = _return(rid, "wrong_account", "fix account", status=200)
    assert replay["refund_id"] == first["refund_id"]
    events = client.get(f"/refunds/{rid}/supplements", headers=H).json()
    assert len(events) == 1  # no duplicate record
    assert client.get("/orders/s2", headers=H).json()["pending_refund_cents"] == 200

def test_return_replay_with_different_reason_or_note_conflicts_and_first_stays() -> None:
    _order("s3", 1000)
    _pay("s3", 1000)
    rid = _refund("s3", "req-s3", 200)["refund_id"]
    _return(rid, "missing_proof", "first note")

    for reason, note in [("wrong_account", "first note"), ("missing_proof", "other note")]:
        resp = client.post(f"/refunds/{rid}/supplement-return",
                           json={"reason_code": reason, "note": note}, headers=H)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "supplement_conflict"

    # First return record is immutable; refund still waiting for the first reason.
    cur = client.get(f"/refunds/{rid}", headers=H).json()
    assert cur["status"] == "awaiting_supplement"
    events = client.get(f"/refunds/{rid}/supplements", headers=H).json()
    assert len(events) == 1
    assert events[0]["reason_code"] == "missing_proof"
    assert events[0]["note"] == "first note"
    assert client.get("/orders/s3", headers=H).json()["pending_refund_cents"] == 200

def test_return_validation() -> None:
    _order("s4", 1000)
    _pay("s4", 1000)
    rid = _refund("s4", "req-s4", 100)["refund_id"]
    assert client.post(f"/refunds/{rid}/supplement-return",
                       json={"reason_code": "bogus", "note": "x"}, headers=H).status_code == 400
    assert client.post(f"/refunds/{rid}/supplement-return",
                       json={"reason_code": "missing_proof", "note": "   "},
                       headers=H).status_code == 400

def test_awaiting_refund_cannot_complete_but_can_reject() -> None:
    _order("s5", 1000)
    _pay("s5", 600)
    rid = _refund("s5", "req-s5", 400)["refund_id"]
    _return(rid)
    resp = client.post(f"/refunds/{rid}/complete", headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "refund_state"
    # Still holding.
    assert client.get("/orders/s5", headers=H).json()["pending_refund_cents"] == 400

    resp = client.post(f"/refunds/{rid}/reject",
                       json={"reason_code": "internal_error"}, headers=H)
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    order = client.get("/orders/s5", headers=H).json()
    assert order["pending_refund_cents"] == 0
    assert order["refunded_cents"] == 0
    assert order["net_cents"] == 600

def test_terminal_refunds_cannot_be_returned() -> None:
    _order("s6", 1000)
    _pay("s6", 1000)
    completed = _refund("s6", "req-s6c", 100)
    client.post(f"/refunds/{completed['refund_id']}/complete", headers=H)
    assert client.post(f"/refunds/{completed['refund_id']}/supplement-return",
                       json={"reason_code": "missing_proof", "note": "x"},
                       headers=H).status_code == 409

    rejected = _refund("s6", "req-s6r", 100)
    client.post(f"/refunds/{rejected['refund_id']}/reject",
                json={"reason_code": "internal_error"}, headers=H)
    assert client.post(f"/refunds/{rejected['refund_id']}/supplement-return",
                       json={"reason_code": "missing_proof", "note": "x"},
                       headers=H).status_code == 409

def test_supplement_accepts_smaller_amount_and_releases_excess() -> None:
    _order("s7", 1000)
    _pay("s7", 1000)
    # Another pending refund occupies 600, leaving 400 of headroom.
    other = _refund("s7", "req-s7-other", 600)
    rid = _refund("s7", "req-s7", 300)["refund_id"]
    assert client.get("/orders/s7", headers=H).json()["pending_refund_cents"] == 900
    _return(rid, "amount_mismatch", "claimed too much")

    out = _supplement(rid, 200)
    assert out["status"] == "accepted"
    assert out["amount_cents"] == 200
    assert out["held_cents"] == 200
    order = client.get("/orders/s7", headers=H).json()
    assert order["pending_refund_cents"] == 800  # 600 + 200: 100 released
    assert order["refunded_cents"] == 0
    assert order["net_cents"] == 1000

    events = client.get(f"/refunds/{rid}/supplements", headers=H).json()
    assert [ev["event_type"] for ev in events] == ["supplemented", "returned"]  # newest first
    assert events[0]["amount_before_cents"] == 300
    assert events[0]["amount_after_cents"] == 200
    assert events[0]["reason_code"] == "amount_mismatch"

    # The other refund is untouched.
    assert client.get(f"/refunds/{other['refund_id']}", headers=H).json()["held_cents"] == 600

def test_supplement_accepts_larger_amount_within_conservation() -> None:
    _order("s8", 1000)
    _pay("s8", 1000)
    rid = _refund("s8", "req-s8", 200)["refund_id"]
    _return(rid)
    out = _supplement(rid, 500)
    assert out["status"] == "accepted" and out["amount_cents"] == 500
    order = client.get("/orders/s8", headers=H).json()
    assert order["pending_refund_cents"] == 500
    # Books balance exactly: refunded + pending == 500 <= paid 1000.
    assert order["refunded_cents"] + order["pending_refund_cents"] == 500

def test_supplement_that_breaks_conservation_is_409_without_half_state() -> None:
    _order("s9", 1000)
    _pay("s9", 800)
    other = _refund("s9", "req-s9-other", 500)
    rid = _refund("s9", "req-s9", 200)["refund_id"]  # total hold 700
    _return(rid)
    # Replacing the 200 hold with 301 -> 500 + 301 = 801 > paid 800.
    resp = client.post(f"/refunds/{rid}/supplement",
                       json={"amount_cents": 301}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "amount_exceeds"

    # Nothing moved: refund still awaiting with its original amount and hold.
    cur = client.get(f"/refunds/{rid}", headers=H).json()
    assert cur["status"] == "awaiting_supplement"
    assert cur["amount_cents"] == 200
    assert cur["held_cents"] == 200
    order = client.get("/orders/s9", headers=H).json()
    assert order["pending_refund_cents"] == 700
    assert client.get(f"/refunds/{other['refund_id']}", headers=H).json()["held_cents"] == 500
    # No supplemented record was written for the failed attempt.
    events = client.get(f"/refunds/{rid}/supplements", headers=H).json()
    assert [ev["event_type"] for ev in events] == ["returned"]

    # The failed attempt leaves the refund usable: a smaller amount goes through.
    out = _supplement(rid, 300)  # 500 + 300 == 800, exactly at the boundary
    assert out["status"] == "accepted" and out["amount_cents"] == 300
    assert client.get("/orders/s9", headers=H).json()["pending_refund_cents"] == 800

def test_supplement_amount_must_be_positive_integer() -> None:
    _order("s10", 1000)
    _pay("s10", 1000)
    rid = _refund("s10", "req-s10", 100)["refund_id"]
    _return(rid)
    for payload in ({"amount_cents": 0}, {"amount_cents": -5}, {"amount_cents": 1.5}):
        resp = client.post(f"/refunds/{rid}/supplement", json=payload, headers=H)
        assert resp.status_code == 422, resp.text
    # Rejected input never mutates state.
    cur = client.get(f"/refunds/{rid}", headers=H).json()
    assert cur["status"] == "awaiting_supplement" and cur["amount_cents"] == 100

def test_supplement_replay_after_acceptance_returns_same_result() -> None:
    _order("s11", 1000)
    _pay("s11", 1000)
    rid = _refund("s11", "req-s11", 300)["refund_id"]
    _return(rid)
    first = _supplement(rid, 250)
    # Same amount retried after acceptance: idempotent replay, no new record.
    replay = _supplement(rid, 250, status=200)
    assert replay["refund_id"] == first["refund_id"]
    events = client.get(f"/refunds/{rid}/supplements", headers=H).json()
    assert [ev["event_type"] for ev in events] == ["supplemented", "returned"]
    assert client.get("/orders/s11", headers=H).json()["pending_refund_cents"] == 250

def test_supplement_after_resumed_with_different_amount_conflicts() -> None:
    _order("s12", 1000)
    _pay("s12", 1000)
    rid = _refund("s12", "req-s12", 300)["refund_id"]
    _return(rid)
    _supplement(rid, 250)
    resp = client.post(f"/refunds/{rid}/supplement",
                       json={"amount_cents": 260}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "supplement_conflict"
    assert client.get(f"/refunds/{rid}", headers=H).json()["amount_cents"] == 250

def test_resumed_refund_can_complete() -> None:
    _order("s13", 1000)
    _pay("s13", 1000)
    rid = _refund("s13", "req-s13", 400)["refund_id"]
    _return(rid)
    _supplement(rid, 300)
    done = client.post(f"/refunds/{rid}/complete", headers=H)
    assert done.status_code == 200 and done.json()["status"] == "completed"
    order = client.get("/orders/s13", headers=H).json()
    assert order["refunded_cents"] == 300
    assert order["pending_refund_cents"] == 0
    assert order["net_cents"] == 700
    assert order["status"] == "accepted"

def test_multiple_return_supplement_rounds_keep_full_audit_trail() -> None:
    _order("s14", 1000)
    _pay("s14", 1000)
    rid = _refund("s14", "req-s14", 500)["refund_id"]
    _return(rid, "missing_proof", "round 1")
    _supplement(rid, 400)
    _return(rid, "wrong_account", "round 2")
    _supplement(rid, 350)

    events = client.get(f"/refunds/{rid}/supplements", headers=H).json()
    assert [(ev["event_type"], ev["note"]) for ev in events] == [
        ("supplemented", "round 2"),
        ("returned", "round 2"),
        ("supplemented", "round 1"),
        ("returned", "round 1"),
    ]
    # Each supplemented record carries the amounts around that resubmission.
    assert (events[0]["amount_before_cents"], events[0]["amount_after_cents"]) == (400, 350)
    assert (events[2]["amount_before_cents"], events[2]["amount_after_cents"]) == (500, 400)
    cur = client.get(f"/refunds/{rid}", headers=H).json()
    assert cur["status"] == "accepted" and cur["amount_cents"] == 350 and cur["held_cents"] == 350
    assert client.get("/orders/s14", headers=H).json()["pending_refund_cents"] == 350

def test_supplement_endpoints_on_unknown_or_foreign_refund_are_404() -> None:
    assert client.post("/refunds/missing/supplement-return",
                       json={"reason_code": "missing_proof", "note": "x"},
                       headers=H).status_code == 404
    assert client.post("/refunds/missing/supplement",
                       json={"amount_cents": 10}, headers=H).status_code == 404
    assert client.get("/refunds/missing/supplements", headers=H).status_code == 404

    _order("s15", 1000)
    _pay("s15", 1000)
    rid = _refund("s15", "req-s15", 100)["refund_id"]
    other = {"X-Tenant": "t2"}
    assert client.post(f"/refunds/{rid}/supplement-return",
                       json={"reason_code": "missing_proof", "note": "x"},
                       headers=other).status_code == 404
    assert client.get(f"/refunds/{rid}/supplements", headers=other).status_code == 404
    # Failed cross-tenant access changes nothing.
    assert client.get(f"/refunds/{rid}", headers=H).json()["status"] == "accepted"

def test_concurrent_return_applies_exactly_once() -> None:
    _order("s16", 1000)
    _pay("s16", 1000)
    rid = _refund("s16", "req-s16", 100)["refund_id"]

    def send(_: int) -> int:
        return client.post(f"/refunds/{rid}/supplement-return",
                           json={"reason_code": "missing_proof", "note": "race"},
                           headers=H).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = sorted(pool.map(send, range(8)))
    assert statuses == [200] * 7 + [201]
    events = client.get(f"/refunds/{rid}/supplements", headers=H).json()
    assert len(events) == 1
    assert client.get(f"/refunds/{rid}", headers=H).json()["status"] == "awaiting_supplement"

def test_concurrent_supplement_applies_exactly_once() -> None:
    _order("s17", 1000)
    _pay("s17", 1000)
    rid = _refund("s17", "req-s17", 400)["refund_id"]
    _return(rid)

    def send(_: int) -> int:
        return client.post(f"/refunds/{rid}/supplement",
                           json={"amount_cents": 300}, headers=H).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = sorted(pool.map(send, range(8)))
    assert statuses == [200] * 7 + [201]
    assert client.get(f"/refunds/{rid}", headers=H).json()["amount_cents"] == 300
    assert client.get("/orders/s17", headers=H).json()["pending_refund_cents"] == 300
    events = client.get(f"/refunds/{rid}/supplements", headers=H).json()
    assert [ev["event_type"] for ev in events] == ["supplemented", "returned"]

def test_concurrent_return_and_reject_one_outcome() -> None:
    _order("s18", 1000)
    _pay("s18", 1000)
    rid = _refund("s18", "req-s18", 300)["refund_id"]

    def do_return() -> str:
        r = client.post(f"/refunds/{rid}/supplement-return",
                        json={"reason_code": "missing_proof", "note": "race"}, headers=H)
        return f"return-{r.status_code}"

    def do_reject() -> str:
        r = client.post(f"/refunds/{rid}/reject",
                        json={"reason_code": "internal_error"}, headers=H)
        return f"reject-{r.status_code}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda f: f(), [do_return, do_reject]))
    # reject always applies (from accepted or awaiting); the return is 201 only
    # if it wins the race, otherwise 409. Never a 5xx, hold released exactly once.
    assert "reject-200" in results, results
    assert "return-201" in results or "return-409" in results, results
    final = client.get(f"/refunds/{rid}", headers=H).json()
    order = client.get("/orders/s18", headers=H).json()
    assert final["status"] == "rejected"
    assert order["pending_refund_cents"] == 0  # released exactly once, never negative
