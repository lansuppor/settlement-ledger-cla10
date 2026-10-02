import argparse

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders, refunds
from app.store.db import connect, migrate
from app.store.refunds import RefundError

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)

class RefundIn(BaseModel):
    # 客户端提供的请求标识，租户内唯一；与订单标识分离，仅用于请求去重
    refund_request_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)

class RefundRejectIn(BaseModel):
    reason: str | None = Field(default=None, min_length=1)

def _require_tenant(x_tenant: str) -> str:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    return x_tenant

def _refund_conflict(error: RefundError) -> HTTPException:
    # 业务拒绝（金额超限、订单状态不允许、请求冲突）以 409 + 可区分 reason 返回，绝不与 5xx 内部错误混同
    return HTTPException(status_code=409, detail={"message": str(error), "reason": error.reason})

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
def read_order(order_id: str, x_tenant: str = Header(default="", alias=None)) -> dict:
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

@app.post("/orders/{order_id}/refunds")
def create_refund(order_id: str, body: RefundIn, x_tenant: str = Header(default="")) -> Response:
    tenant = _require_tenant(x_tenant)
    try:
        result = refunds.create(tenant, order_id, body.refund_request_id, body.amount_cents)
    except RefundError as error:
        # 业务拒绝（金额超限、订单状态不允许、请求冲突）与 5xx 内部错误严格区分
        raise _refund_conflict(error)
    if result is None:
        # 订单不存在或不属于本租户：一律按不存在处理
        raise HTTPException(status_code=404, detail="order not found")
    refund, first = result
    # 首次受理 201；相同请求标识重放返回首次单据，200 表明未重复受理
    return JSONResponse(status_code=201 if first else 200, content=refund)

@app.get("/orders/{order_id}/refunds")
def list_order_refunds(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    rows = refunds.list_for_order(tenant, order_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="order not found")
    order = orders.get(tenant, order_id)
    return {
        "order": order,
        "refunds": rows,
    }

@app.get("/refunds/{refund_request_id}")
def read_refund(refund_request_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    refund = refunds.get(tenant, refund_request_id)
    if refund is None:
        # 跨租户或不存在均返回 404，不泄漏对象是否存在
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.post("/refunds/{refund_request_id}/complete")
def complete_refund(refund_request_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        refund = refunds.advance(tenant, refund_request_id, "complete")
    except RefundError as error:
        raise _refund_conflict(error)
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.post("/refunds/{refund_request_id}/reject")
def reject_refund(refund_request_id: str, body: RefundRejectIn | None = None,
                  x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    reason = body.reason if body else None
    try:
        refund = refunds.advance(tenant, refund_request_id, "reject", reason)
    except RefundError as error:
        raise _refund_conflict(error)
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

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
