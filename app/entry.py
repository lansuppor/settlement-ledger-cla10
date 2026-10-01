import argparse

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders
from app.store.db import connect, migrate

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    # 调用方对订单内容做稳定摘要得到；（租户, 幂等键）唯一，与订单标识唯一性相互独立。
    idempotency_key: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=1)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)

@app.get("/health")
def health() -> dict:
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}

@app.post("/orders", status_code=201)
def create_order(body: OrderIn, response: Response) -> dict:
    order_rules.assert_currency(body.currency)
    try:
        order, replayed = orders.accept_order(
            body.tenant,
            body.order_id,
            body.amount_cents,
            body.currency,
            body.idempotency_key,
            body.request_fingerprint,
        )
    except orders.OrderAlreadyAccepted:
        # 同一（租户, 订单标识）重复受理：409，detail 与幂等键冲突明确区分。
        raise HTTPException(status_code=409, detail="order already accepted")
    except orders.IdempotencyConflict:
        # 同一幂等键被用于不同业务内容：拒绝，且不覆盖首次受理的任何数据。
        raise HTTPException(status_code=409, detail="idempotency key reused with different request fingerprint")
    if replayed:
        # 同键同指纹的重放：返回首次受理的订单对象与相同的成功状态，不新建任何数据。
        response.headers["X-Idempotent-Replay"] = "true"
    return order

@app.get("/orders/{order_id}")
def read_order(order_id: str, x_tenant: str = Header(default="", alias=None)) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/payments")
def add_payment(order_id: str, body: PaymentIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        order = orders.add_payment(x_tenant, order_id, body.amount_cents)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

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
