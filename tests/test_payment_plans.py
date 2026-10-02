import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_plans.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "t1"}


def _order(oid: str, amount: int = 1000, tenant: str = "t1", currency: str = "CNY") -> None:
    resp = client.post(
        "/orders",
        json={"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": currency},
    )
    assert resp.status_code == 201, resp.text


def _plan(oid: str, installments: list, headers: dict | None = None, status: int = 201):
    resp = client.post(
        f"/orders/{oid}/payment-plan",
        json={"installments": [
            {"installment_id": label, "amount_cents": amount} for label, amount in installments
        ]},
        headers=headers if headers is not None else H,
    )
    assert resp.status_code == status, resp.text
    return resp.json()


def test_create_plan_and_read_progress() -> None:
    _order("p1", 1000)
    plan = _plan("p1", [("a", 300), ("b", 300), ("c", 400)])
    assert plan["planned_amount_cents"] == 1000
    assert plan["received_cents"] == 0
    assert plan["settled_installments"] == 0
    assert plan["currency"] == "CNY"
    assert [i["installment_id"] for i in plan["installments"]] == ["a", "b", "c"]
    assert [i["amount_cents"] for i in plan["installments"]] == [300, 300, 400]
    assert [i["paid_cents"] for i in plan["installments"]] == [0, 0, 0]
    assert [i["install_seq"] for i in plan["installments"]] == [1, 2, 3]
    assert all(i["settled"] is False for i in plan["installments"])

    # Creating a plan changes neither the money position nor the status.
    order = client.get("/orders/p1", headers=H).json()
    assert (order["paid_cents"], order["outstanding_cents"], order["status"]) == (0, 1000, "accepted")


def test_plan_validation_rejects_and_writes_nothing() -> None:
    _order("p2", 1000)

    # Installment amounts do not sum to the order amount.
    resp = client.post("/orders/p2/payment-plan",
                       json={"installments": [{"installment_id": "a", "amount_cents": 400},
                                              {"installment_id": "b", "amount_cents": 400}]},
                       headers=H)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "plan_total_mismatch"

    # Duplicate installment identifiers.
    resp = client.post("/orders/p2/payment-plan",
                       json={"installments": [{"installment_id": "a", "amount_cents": 500},
                                              {"installment_id": "a", "amount_cents": 500}]},
                       headers=H)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "installment_duplicate"

    # Non-positive installment amount is rejected by the request model.
    resp = client.post("/orders/p2/payment-plan",
                       json={"installments": [{"installment_id": "a", "amount_cents": 0}]},
                       headers=H)
    assert resp.status_code == 422

    # Empty plan is rejected as well.
    resp = client.post("/orders/p2/payment-plan", json={"installments": []}, headers=H)
    assert resp.status_code == 422

    # Nothing was written: the order still has no plan and one can be created now.
    assert client.get("/orders/p2/payment-plan", headers=H).status_code == 404
    _plan("p2", [("a", 1000)])


def test_duplicate_plan_is_rejected() -> None:
    _order("p3", 1000)
    _plan("p3", [("a", 1000)])
    resp = client.post("/orders/p3/payment-plan",
                       json={"installments": [{"installment_id": "b", "amount_cents": 1000}]},
                       headers=H)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "plan_exists"
    # The first plan is untouched.
    assert [i["installment_id"] for i in client.get("/orders/p3/payment-plan", headers=H)
            .json()["installments"]] == ["a"]


def test_plan_only_before_any_payment() -> None:
    _order("p4", 1000)
    assert client.post("/orders/p4/payments", json={"amount_cents": 100}, headers=H).status_code == 200
    resp = client.post("/orders/p4/payment-plan",
                       json={"installments": [{"installment_id": "a", "amount_cents": 1000}]},
                       headers=H)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "order_state"
    assert client.get("/orders/p4/payment-plan", headers=H).status_code == 404


def test_plan_endpoints_404_on_missing_or_foreign_order() -> None:
    assert client.post("/orders/nope/payment-plan",
                       json={"installments": [{"installment_id": "a", "amount_cents": 1}]},
                       headers=H).status_code == 404
    assert client.get("/orders/nope/payment-plan", headers=H).status_code == 404
    assert client.delete("/orders/nope/payment-plan", headers=H).status_code == 404

    _order("p-foreign", 1000)
    _plan("p-foreign", [("a", 1000)])
    other = {"X-Tenant": "t2"}
    assert client.get("/orders/p-foreign/payment-plan", headers=other).status_code == 404
    assert client.post("/orders/p-foreign/payment-plan",
                       json={"installments": [{"installment_id": "a", "amount_cents": 1000}]},
                       headers=other).status_code == 404
    assert client.delete("/orders/p-foreign/payment-plan", headers=other).status_code == 404
    # Foreign-tenant rejection must not have canceled the plan.
    assert client.get("/orders/p-foreign/payment-plan", headers=H).status_code == 200


def test_cancel_plan_only_before_any_payment_and_can_rebuild() -> None:
    _order("p5", 1000)

    # No plan to cancel.
    resp = client.delete("/orders/p5/payment-plan", headers=H)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "plan_missing"

    _plan("p5", [("a", 400), ("b", 600)])
    before = client.get("/orders/p5", headers=H).json()
    canceled = client.delete("/orders/p5/payment-plan", headers=H)
    assert canceled.status_code == 200
    assert client.get("/orders/p5/payment-plan", headers=H).status_code == 404
    after = client.get("/orders/p5", headers=H).json()
    assert after["paid_cents"] == before["paid_cents"] == 0
    assert after["status"] == before["status"] == "accepted"

    # A fresh plan can be established after cancellation.
    _plan("p5", [("x", 1000)])
    assert client.get("/orders/p5/payment-plan", headers=H).status_code == 200

    # Once money arrived the plan is locked against cancellation.
    assert client.post("/orders/p5/payments", json={"amount_cents": 1000}, headers=H).status_code == 200
    resp = client.delete("/orders/p5/payment-plan", headers=H)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "order_state"
    assert client.get("/orders/p5/payment-plan", headers=H).status_code == 200


def test_payment_settles_whole_installments_in_order() -> None:
    _order("p6", 1000)
    _plan("p6", [("a", 300), ("b", 300), ("c", 400)])

    assert client.post("/orders/p6/payments", json={"amount_cents": 600}, headers=H).status_code == 200
    plan = client.get("/orders/p6/payment-plan", headers=H).json()
    assert [(i["installment_id"], i["paid_cents"], i["settled"]) for i in plan["installments"]] == [
        ("a", 300, True), ("b", 300, True), ("c", 0, False),
    ]
    assert plan["received_cents"] == 600 and plan["settled_installments"] == 2
    order = client.get("/orders/p6", headers=H).json()
    assert (order["paid_cents"], order["outstanding_cents"], order["status"]) == (600, 400, "accepted")

    assert client.post("/orders/p6/payments", json={"amount_cents": 400}, headers=H).status_code == 200
    plan = client.get("/orders/p6/payment-plan", headers=H).json()
    assert plan["received_cents"] == 1000 and plan["settled_installments"] == 3
    assert all(i["settled"] for i in plan["installments"])
    order = client.get("/orders/p6", headers=H).json()
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0
    assert order["status"] == "settled"


def test_partial_installment_payment_is_409_without_any_change() -> None:
    _order("p7", 1000)
    _plan("p7", [("a", 300), ("b", 300), ("c", 400)])

    # 200 would leave a 100 remainder on the first installment.
    resp = client.post("/orders/p7/payments", json={"amount_cents": 200}, headers=H)
    assert resp.status_code == 409
    # 400 would over-fill the first installment and short the second one.
    resp = client.post("/orders/p7/payments", json={"amount_cents": 400}, headers=H)
    assert resp.status_code == 409
    # Any amount past the plan total is refused too.
    resp = client.post("/orders/p7/payments", json={"amount_cents": 1001}, headers=H)
    assert resp.status_code == 409

    order = client.get("/orders/p7", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 1000
    plan = client.get("/orders/p7/payment-plan", headers=H).json()
    assert plan["received_cents"] == 0 and plan["settled_installments"] == 0
    assert [i["paid_cents"] for i in plan["installments"]] == [0, 0, 0]

    # A valid whole-installment payment still goes through after the refusals.
    assert client.post("/orders/p7/payments", json={"amount_cents": 300}, headers=H).status_code == 200
    # Duplicate submission keeps existing payment semantics: it is counted again
    # only if legal; 300 cannot close the 300-remainder second installment plus
    # anything, and must not leave the second one half filled.
    resp = client.post("/orders/p7/payments", json={"amount_cents": 200}, headers=H)
    assert resp.status_code == 409
    plan = client.get("/orders/p7/payment-plan", headers=H).json()
    assert [(i["paid_cents"], i["settled"]) for i in plan["installments"]] == [
        (300, True), (0, False), (0, False),
    ]


def test_residual_payment_must_cover_consecutive_remainders() -> None:
    _order("p8", 1000)
    _plan("p8", [("a", 300), ("b", 300), ("c", 400)])
    assert client.post("/orders/p8/payments", json={"amount_cents": 300}, headers=H).status_code == 200
    # From the second installment onward, only 700 (= 300 + 400) is a legal payment.
    assert client.post("/orders/p8/payments", json={"amount_cents": 400}, headers=H).status_code == 409
    assert client.post("/orders/p8/payments", json={"amount_cents": 700}, headers=H).status_code == 200
    plan = client.get("/orders/p8/payment-plan", headers=H).json()
    assert plan["settled_installments"] == 3 and plan["received_cents"] == 1000


def test_plan_payments_coexist_with_refunds() -> None:
    _order("p9", 1000)
    _plan("p9", [("a", 400), ("b", 600)])
    assert client.post("/orders/p9/payments", json={"amount_cents": 400}, headers=H).status_code == 200

    refund = client.post("/orders/p9/refunds",
                         json={"refund_request_id": "pr-1", "amount_cents": 400}, headers=H)
    assert refund.status_code == 201
    assert client.post(f"/refunds/{refund.json()['refund_id']}/complete",
                       headers=H).status_code == 200

    order = client.get("/orders/p9", headers=H).json()
    assert (order["paid_cents"], order["refunded_cents"], order["net_cents"]) == (400, 400, 0)
    assert order["status"] == "open"  # net back to zero after money was taken
    plan = client.get("/orders/p9/payment-plan", headers=H).json()
    # Installment progress reflects registered receipts, unaffected by refunds.
    assert [(i["paid_cents"], i["settled"]) for i in plan["installments"]] == [
        (400, True), (0, False),
    ]
    assert plan["received_cents"] == 400
