import argparse

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders
from app.store.db import connect, migrate

app = FastAPI(title="settlement-ledger")

@app.exception_handler(RequestValidationError)
def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # 参数不合法（含缺少幂等键/指纹）一律 400，不落任何数据。
    return JSONResponse(status_code=400, content={"detail": {"error": "invalid request", "fields": exc.errors()}})

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    idempotency_key: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=1)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)


class ReversalIn(BaseModel):
    reversal_id: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=1)

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
            body.idempotency_key,
            body.request_fingerprint,
            body.order_id,
            body.amount_cents,
            body.currency,
        )
    except orders.FingerprintConflict:
        # 幂等键被复用给不同业务内容：与订单重复受理明确区分。
        raise HTTPException(status_code=422, detail="idempotency key reused with a different request fingerprint")
    except orders.OrderAlreadyAccepted:
        raise HTTPException(status_code=409, detail="order already accepted")
    if replayed:
        response.headers["X-Idempotency-Replay"] = "true"
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
        registered = orders.add_payment(x_tenant, order_id, body.amount_cents)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if registered is None:
        raise HTTPException(status_code=404, detail="order not found")
    order, payment_id = registered
    # payment_id 由服务分配，随登记结果可回读；同租户内唯一且稳定不变。
    return {"payment_id": payment_id, **order}


@app.post("/payments/{payment_id}/reversals")
def reverse_payment(payment_id: str, body: ReversalIn, response: Response, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        result, replayed = orders.reverse_payment(
            x_tenant,
            payment_id,
            body.reversal_id,
            body.request_fingerprint,
        )
    except orders.ReversalFingerprintConflict:
        # 冲正标识被复用给不同业务内容：与收款已处理（409）明确区分。
        raise HTTPException(
            status_code=422,
            detail="reversal id reused with a different request fingerprint",
        )
    except orders.PaymentAlreadyReversed:
        # 冲正不可再被冲正：对已冲正收款再次冲正按已处理拒绝。
        raise HTTPException(status_code=409, detail="payment already reversed")
    if result is None:
        # 含跨租户：不存在的收款一律按不存在处理，不改变订单与收款。
        raise HTTPException(status_code=404, detail="payment not found")
    if replayed:
        response.headers["X-Idempotency-Replay"] = "true"
    return result

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
