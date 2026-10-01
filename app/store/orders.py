import json
import os

from app.store.db import connect


class OrderAlreadyAccepted(Exception):
    """同一（租户, 订单标识）已受理过 —— HTTP 409。"""


class FingerprintConflict(Exception):
    """同一（租户, 幂等键）被用于不同请求指纹 —— HTTP 422。"""


def accept_order(
    tenant: str,
    idempotency_key: str,
    request_fingerprint: str,
    order_id: str,
    amount_cents: int,
    currency: str,
) -> tuple[dict, bool]:
    """可重放受理。返回（订单对象, 是否为重放）。

    订单写入与幂等记录写入在同一个 BEGIN IMMEDIATE 事务中：
    并发请求由写锁串行化，崩溃在提交前整体回滚、提交后整体可见。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        recorded = conn.execute(
            "SELECT request_fingerprint, response_snapshot FROM accepted_requests "
            "WHERE tenant=? AND idempotency_key=?",
            (tenant, idempotency_key),
        ).fetchone()
        if recorded is not None:
            # 幂等键已消费：只比对指纹，绝不改写首次受理的任何记录。
            conn.execute("ROLLBACK")
            if recorded["request_fingerprint"] != request_fingerprint:
                raise FingerprintConflict
            return json.loads(recorded["response_snapshot"]), True

        if conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone() is not None:
            conn.execute("ROLLBACK")
            raise OrderAlreadyAccepted

        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) "
            "VALUES(?,?,?,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
        order = {
            "tenant": tenant,
            "order_id": order_id,
            "amount_cents": amount_cents,
            "paid_cents": 0,
            "currency": currency,
            "status": "accepted",
            "outstanding_cents": amount_cents,
        }
        conn.execute(
            "INSERT INTO accepted_requests(tenant, idempotency_key, request_fingerprint, order_id, response_snapshot) "
            "VALUES(?,?,?,?,?)",
            (tenant, idempotency_key, request_fingerprint, order_id, json.dumps(order, ensure_ascii=False)),
        )
        if os.environ.get("APP_CRASH_BEFORE_COMMIT") == "1":
            # 崩溃演练：不提交直接硬退出，未提交事务随连接被丢弃。
            os._exit(2)
        conn.execute("COMMIT")
        return order, False
    finally:
        conn.close()


def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    outstanding = row["amount_cents"] - row["paid_cents"]
    return {**dict(row), "outstanding_cents": outstanding}


def add_payment(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
            conn.execute("ROLLBACK")
            raise ValueError("payment exceeds outstanding amount")
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)
