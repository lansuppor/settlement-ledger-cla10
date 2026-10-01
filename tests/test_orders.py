import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

def _body(order_id: str, **overrides) -> dict:
    body = {
        "tenant": "t1",
        "order_id": order_id,
        "amount_cents": 500,
        "currency": "CNY",
        "idempotency_key": f"key-{order_id}",
        "request_fingerprint": f"fp-{order_id}",
    }
    body.update(overrides)
    return body

def test_accept_and_read_order() -> None:
    assert client.post("/orders", json=_body("o1")).status_code == 201
    got = client.get("/orders/o1", headers={"X-Tenant": "t1"})
    assert got.status_code == 200 and got.json()["outstanding_cents"] == 500

def test_duplicate_is_refused() -> None:
    assert client.post("/orders", json=_body("o2", amount_cents=100)).status_code == 201
    # 订单标识冲突必须用不同的幂等键发起，否则会先走重放分支。
    dup = client.post("/orders", json=_body("o2", amount_cents=100, idempotency_key="key-o2-again"))
    assert dup.status_code == 409 and dup.json()["detail"] == "order already accepted"

def test_cross_tenant_read_is_not_found() -> None:
    client.post("/orders", json=_body("o3", amount_cents=100))
    assert client.get("/orders/o3", headers={"X-Tenant": "t2"}).status_code == 404

def test_payment_cannot_exceed_outstanding() -> None:
    client.post("/orders", json=_body("o4", amount_cents=300))
    assert client.post("/orders/o4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/o4/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"}).status_code == 409
