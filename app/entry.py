import argparse

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders, plans, refunds
from app.store.db import connect, migrate
from app.store.plans import PlanError
from app.store.refunds import REJECT_REASONS, SUPPLEMENT_REASONS, RefundError

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)

class RefundIn(BaseModel):
    # Client-supplied dedup identity; deliberately separate from order_id/refund_id.
    refund_request_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)

class RejectIn(BaseModel):
    reason_code: str = Field(default="internal_error")

class SupplementReturnIn(BaseModel):
    reason_code: str
    note: str

class SupplementIn(BaseModel):
    amount_cents: int = Field(gt=0)

class PlanItemIn(BaseModel):
    term_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)

class PaymentPlanIn(BaseModel):
    items: list[PlanItemIn] = Field(min_length=1)

def _require_tenant(x_tenant: str) -> str:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    return x_tenant

def _refund_conflict(error: RefundError) -> HTTPException:
    detail: dict = {"code": error.reason_code, "message": str(error)}
    if error.refund is not None:
        detail["refund"] = error.refund
    return HTTPException(status_code=409, detail=detail)

def _plan_conflict(error: PlanError) -> HTTPException:
    detail = {"code": error.reason_code, "message": str(error)}
    return HTTPException(status_code=409, detail=detail)

@app.get("/health")
def health() -> dict:
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}

@app.post("/orders", status_code=201)
def create_order(body: OrderIn) -> dict:
    order_rules.assert_currency(body.currency)
    try:
        orders.insert(body.tenant, body.order_id, body.amount_cents, body.currency)
    except Exception as error:
        if "UNIQUE" in str(error):
            raise HTTPException(status_code=409, detail="order already accepted")
        raise
    return orders.get(body.tenant, body.order_id)

@app.get("/orders/{order_id}")
def read_order(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/payments")
def add_payment(order_id: str, body: PaymentIn, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        order = orders.add_payment(tenant, order_id, body.amount_cents)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/payment-plan", status_code=201)
def create_payment_plan(
    order_id: str, body: PaymentPlanIn, x_tenant: str = Header(default="")
) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        plan = plans.create_plan(
            tenant, order_id, [(item.term_id, item.amount_cents) for item in body.items]
        )
    except PlanError as error:
        raise _plan_conflict(error)
    if plan is None:
        raise HTTPException(status_code=404, detail="order not found")
    return plan

@app.delete("/orders/{order_id}/payment-plan")
def cancel_payment_plan(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        cancelled = plans.cancel_plan(tenant, order_id)
    except PlanError as error:
        raise _plan_conflict(error)
    if cancelled is None:
        raise HTTPException(status_code=404, detail="order not found")
    return orders.get(tenant, order_id)

@app.get("/orders/{order_id}/payment-plan")
def read_payment_plan(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    plan = plans.get_plan(tenant, order_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="payment plan not found")
    return plan

@app.post("/orders/{order_id}/refunds")
def create_refund(
    order_id: str, body: RefundIn, response: Response, x_tenant: str = Header(default="")
) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        refund, created = refunds.create_refund(
            tenant, order_id, body.refund_request_id, body.amount_cents
        )
    except RefundError as error:
        raise _refund_conflict(error)
    if refund is None:
        # Missing or foreign-tenant order: same answer, no existence leak.
        raise HTTPException(status_code=404, detail="order not found")
    response.status_code = 201 if created else 200
    return refund

@app.get("/orders/{order_id}/refunds")
def list_refunds(order_id: str, x_tenant: str = Header(default="")) -> list[dict]:
    tenant = _require_tenant(x_tenant)
    rows = refunds.list_for_order(tenant, order_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="order not found")
    return rows

@app.get("/refunds/{refund_id}")
def read_refund(refund_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    refund, _order = refunds.get_refund(tenant, refund_id=refund_id)
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.post("/refunds/{refund_id}/complete")
def complete_refund(refund_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        refund = refunds.complete_refund(tenant, refund_id)
    except RefundError as error:
        raise _refund_conflict(error)
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.post("/refunds/{refund_id}/reject")
def reject_refund(
    refund_id: str, body: RejectIn, x_tenant: str = Header(default="")
) -> dict:
    tenant = _require_tenant(x_tenant)
    if body.reason_code not in REJECT_REASONS:
        allowed = ", ".join(sorted(REJECT_REASONS))
        raise HTTPException(status_code=400, detail=f"reason_code must be one of: {allowed}")
    try:
        refund = refunds.reject_refund(tenant, refund_id, body.reason_code)
    except RefundError as error:
        raise _refund_conflict(error)
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.post("/refunds/{refund_id}/supplement-return")
def return_refund_for_supplement(
    refund_id: str, body: SupplementReturnIn, response: Response,
    x_tenant: str = Header(default=""),
) -> dict:
    tenant = _require_tenant(x_tenant)
    note = body.note.strip()
    if body.reason_code not in SUPPLEMENT_REASONS or not note:
        allowed = ", ".join(sorted(SUPPLEMENT_REASONS))
        raise HTTPException(
            status_code=400,
            detail=f"reason_code must be one of: {allowed}; note must be non-empty",
        )
    try:
        refund, returned = refunds.return_for_supplement(tenant, refund_id, body.reason_code, note)
    except RefundError as error:
        raise _refund_conflict(error)
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    response.status_code = 201 if returned else 200
    return refund

@app.post("/refunds/{refund_id}/supplement")
def supplement_refund(
    refund_id: str, body: SupplementIn, response: Response,
    x_tenant: str = Header(default=""),
) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        refund, accepted = refunds.submit_supplement(tenant, refund_id, body.amount_cents)
    except RefundError as error:
        raise _refund_conflict(error)
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    response.status_code = 201 if accepted else 200
    return refund

@app.get("/refunds/{refund_id}/supplements")
def list_refund_supplements(refund_id: str, x_tenant: str = Header(default="")) -> list[dict]:
    tenant = _require_tenant(x_tenant)
    events = refunds.list_supplement_events(tenant, refund_id)
    if events is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return events

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    migrate()
    if args.migrate:
        print("migrated")
        return
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)

if __name__ == "__main__":
    main()
