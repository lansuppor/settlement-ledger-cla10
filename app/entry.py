import argparse

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import batches, installments, orders
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

class InstallmentIn(BaseModel):
    # 分期业务标识与请求指纹均为非空字符串：缺少（含空串）按参数不合法 400 拒绝，不落任何数据。
    installment_key: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)

class InstallmentReversalIn(BaseModel):
    reversal_id: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=1)

class BatchIn(BaseModel):
    # 批次级字段：缺批次标识或指纹（含空串）按参数不合法 400 拒绝，不落任何数据。
    # 行内容逐行校验（行级拒绝只影响本行），故 items 元素在此不加约束。
    tenant: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=1)
    items: list[dict] = Field(min_length=1)

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

@app.post("/orders/batch")
def accept_batch(body: BatchIn, response: Response) -> dict:
    try:
        result, replayed = batches.accept_batch(
            body.tenant,
            body.batch_id,
            body.request_fingerprint,
            body.items,
        )
    except batches.BatchFingerprintConflict:
        # 批次标识被复用于不同业务内容：与行级各拒绝（在结果清单内按行定位）明确区分。
        raise HTTPException(status_code=422, detail="batch id reused with a different request fingerprint")
    if replayed:
        response.headers["X-Idempotency-Replay"] = "true"
    return result

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

@app.get("/payments/{payment_id}")
def read_payment(payment_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    payment = orders.get_payment(x_tenant, payment_id)
    if payment is None:
        # 跨租户与不存在统一 404，不泄漏收款是否存在。
        raise HTTPException(status_code=404, detail="payment not found")
    return payment

@app.post("/payments/{payment_id}/reversal")
def reverse_payment(payment_id: str, body: ReversalIn, response: Response, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        result = orders.reverse_payment(x_tenant, payment_id, body.reversal_id, body.request_fingerprint)
    except orders.FingerprintConflict:
        # 冲正标识被复用给不同业务内容：与收款已冲正（409）明确区分。
        raise HTTPException(status_code=422, detail="reversal id reused with a different request fingerprint")
    except orders.PaymentAlreadyReversed:
        raise HTTPException(status_code=409, detail="payment already reversed")
    if result is None:
        raise HTTPException(status_code=404, detail="payment not found")
    snapshot, replayed = result
    if replayed:
        response.headers["X-Idempotency-Replay"] = "true"
    return snapshot

@app.post("/orders/{order_id}/installments")
def add_installment(order_id: str, body: InstallmentIn, response: Response, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        result = installments.register_installment(
            x_tenant, order_id, body.installment_key, body.request_fingerprint, body.amount_cents
        )
    except installments.FingerprintConflict:
        # 分期业务标识被复用给不同业务内容：与超未收（409）明确区分。
        raise HTTPException(status_code=422, detail="installment key reused with a different request fingerprint")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        raise HTTPException(status_code=404, detail="order not found")
    snapshot, replayed = result
    if replayed:
        response.headers["X-Idempotency-Replay"] = "true"
    return snapshot

@app.get("/installments/{installment_id}")
def read_installment(installment_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    installment = installments.get_installment(x_tenant, installment_id)
    if installment is None:
        # 跨租户与不存在统一 404，不泄漏分期是否存在。
        raise HTTPException(status_code=404, detail="installment not found")
    return installment

@app.post("/installments/{installment_id}/reversal")
def reverse_installment(
    installment_id: str, body: InstallmentReversalIn, response: Response, x_tenant: str = Header(default="")
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        result = installments.reverse_installment(
            x_tenant, installment_id, body.reversal_id, body.request_fingerprint
        )
    except installments.FingerprintConflict:
        # 冲正标识被复用给不同业务内容：与分期已冲正（409）明确区分。
        raise HTTPException(status_code=422, detail="reversal id reused with a different request fingerprint")
    except installments.InstallmentAlreadyReversed:
        raise HTTPException(status_code=409, detail="installment already reversed")
    if result is None:
        raise HTTPException(status_code=404, detail="installment not found")
    snapshot, replayed = result
    if replayed:
        response.headers["X-Idempotency-Replay"] = "true"
    return snapshot

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
