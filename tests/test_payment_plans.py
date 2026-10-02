import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_plans.sqlite"))
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

def _plan(oid: str, items: list[dict], headers: dict | None = None, status: int = 201):
    resp = client.post(f"/orders/{oid}/payment-plan",
                       json={"items": items},
                       headers=headers if headers is not None else H)
    assert resp.status_code == status, resp.text
    return resp

def _pay(oid: str, amount: int, status: int = 200):
    resp = client.post(f"/orders/{oid}/payments", json={"amount_cents": amount}, headers=H)
    assert resp.status_code == status, resp.text
    return resp

def _get_plan(oid: str, headers: dict | None = None, status: int = 200):
    resp = client.get(f"/orders/{oid}/payment-plan",
                      headers=headers if headers is not None else H)
    assert resp.status_code == status, resp.text
    return resp

def test_create_plan_and_read_progress() -> None:
    _order("p1", 1000)
    plan = _plan("p1", [{"term_id": "t1", "amount_cents": 400},
                        {"term_id": "t2", "amount_cents": 600}]).json()
    assert plan["planned_cents"] == 1000
    assert plan["settled_count"] == 0
    assert [i["term_id"] for i in plan["items"]] == ["t1", "t2"]
    assert all(i["paid_cents"] == 0 and not i["settled"] for i in plan["items"])
    # Plan creation must not move order money or status.
    order = client.get("/orders/p1", headers=H).json()
    assert order["paid_cents"] == 0
    assert order["outstanding_cents"] == 1000
    assert order["status"] == "accepted"

def test_invalid_plans_are_refused_atomically() -> None:
    _order("p2", 300)
    # Sum mismatch.
    resp = _plan("p2", [{"term_id": "a", "amount_cents": 100}], status=409)
    assert resp.json()["detail"]["code"] == "invalid_plan"
    # Duplicate term id.
    resp = _plan("p2", [{"term_id": "a", "amount_cents": 100},
                        {"term_id": "a", "amount_cents": 200}], status=409)
    assert resp.json()["detail"]["code"] == "invalid_plan"
    # Non-positive installment amount.
    assert _plan("p2", [{"term_id": "a", "amount_cents": 0},
                        {"term_id": "b", "amount_cents": 300}], status=422)
    # Nothing was written by the refused attempts.
    _get_plan("p2", status=404)
    # A valid plan still goes through afterwards.
    _plan("p2", [{"term_id": "a", "amount_cents": 300}])

def test_duplicate_plan_is_refused() -> None:
    _order("p3", 100)
    _plan("p3", [{"term_id": "a", "amount_cents": 100}])
    resp = _plan("p3", [{"term_id": "b", "amount_cents": 100}], status=409)
    assert resp.json()["detail"]["code"] == "plan_exists"
    # The first plan is untouched.
    plan = _get_plan("p3").json()
    assert [i["term_id"] for i in plan["items"]] == ["a"]

def test_plan_requires_unpaid_order() -> None:
    _order("p4", 500)
    _pay("p4", 100)
    resp = _plan("p4", [{"term_id": "a", "amount_cents": 500}], status=409)
    assert resp.json()["detail"]["code"] == "order_state"

def test_cancel_plan_and_recreate() -> None:
    _order("p5", 200)
    _plan("p5", [{"term_id": "a", "amount_cents": 200}])
    resp = client.delete("/orders/p5/payment-plan", headers=H)
    assert resp.status_code == 200, resp.text
    # Cancellation leaves order money and status alone.
    order = resp.json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 200
    _get_plan("p5", status=404)
    # Cancelling again is a conflict; recreating is allowed.
    resp = client.delete("/orders/p5/payment-plan", headers=H)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "plan_not_found"
    _plan("p5", [{"term_id": "x", "amount_cents": 50},
                 {"term_id": "y", "amount_cents": 150}])

def test_cancel_after_payment_is_refused() -> None:
    _order("p6", 300)
    _plan("p6", [{"term_id": "a", "amount_cents": 300}])
    _pay("p6", 300)
    resp = client.delete("/orders/p6/payment-plan", headers=H)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "order_state"
    # Plan survives the refused cancellation.
    plan = _get_plan("p6").json()
    assert plan["settled_count"] == 1

def test_payment_follows_installments() -> None:
    _order("p7", 900)
    _plan("p7", [{"term_id": "a", "amount_cents": 300},
                 {"term_id": "b", "amount_cents": 400},
                 {"term_id": "c", "amount_cents": 200}])
    # One payment may span several consecutive installments.
    order = _pay("p7", 700).json()
    assert order["paid_cents"] == 700 and order["outstanding_cents"] == 200
    plan = _get_plan("p7").json()
    assert [i["settled"] for i in plan["items"]] == [True, True, False]
    assert plan["settled_count"] == 2
    # Paid total equals the sum of settled installments.
    settled_sum = sum(i["amount_cents"] for i in plan["items"] if i["settled"])
    assert order["paid_cents"] == settled_sum
    # Final installment settles the order.
    order = _pay("p7", 200).json()
    assert order["status"] == "settled"
    assert _get_plan("p7").json()["settled_count"] == 3

def test_partial_installment_payment_is_refused_without_side_effects() -> None:
    _order("p8", 500)
    _plan("p8", [{"term_id": "a", "amount_cents": 300},
                 {"term_id": "b", "amount_cents": 200}])
    _pay("p8", 300)
    # 100 would leave a partial installment; 400 exceeds the remainder too.
    _pay("p8", 100, status=409)
    order = client.get("/orders/p8", headers=H).json()
    assert order["paid_cents"] == 300
    plan = _get_plan("p8").json()
    assert [i["paid_cents"] for i in plan["items"]] == [300, 0]
    assert plan["settled_count"] == 1
    # The exact remaining installment is still accepted.
    _pay("p8", 200)

def test_payment_spanning_into_partial_remainder_is_refused() -> None:
    _order("p9", 600)
    _plan("p9", [{"term_id": "a", "amount_cents": 100},
                 {"term_id": "b", "amount_cents": 500}])
    # 150 = installment a + 50 into b: a partial tail is not allowed.
    _pay("p9", 150, status=409)
    order = client.get("/orders/p9", headers=H).json()
    assert order["paid_cents"] == 0
    _pay("p9", 600)  # the whole schedule in one go is fine
    assert client.get("/orders/p9", headers=H).json()["status"] == "settled"

def test_plan_endpoints_hide_missing_and_foreign_orders() -> None:
    _order("p10", 100, tenant="t1")
    foreign = {"X-Tenant": "t2"}
    assert client.get("/orders/nope/payment-plan", headers=H).status_code == 404
    assert client.post("/orders/nope/payment-plan",
                       json={"items": [{"term_id": "a", "amount_cents": 1}]},
                       headers=H).status_code == 404
    assert client.delete("/orders/nope/payment-plan", headers=H).status_code == 404
    # Cross-tenant looks exactly like missing.
    _get_plan("p10", headers=foreign, status=404)
    _plan("p10", [{"term_id": "a", "amount_cents": 100}], headers=foreign, status=404)
    assert client.delete("/orders/p10/payment-plan", headers=foreign).status_code == 404

def test_order_without_plan_keeps_plain_payment_semantics() -> None:
    _order("p11", 400)
    _pay("p11", 150)
    order = _pay("p11", 250).json()
    assert order["status"] == "settled"
    _get_plan("p11", status=404)
