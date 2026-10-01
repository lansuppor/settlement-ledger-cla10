import sqlite3

from app.store.db import connect


class OrderAlreadyAccepted(Exception):
    """同一（租户，订单标识）已受理过。"""

class IdempotencyConflict(Exception):
    """同一（租户，幂等键）已被用于不同的请求指纹。"""

_ORDER_COLUMNS = "tenant, order_id, amount_cents, paid_cents, currency, status"

def _row_to_order(row: sqlite3.Row) -> dict:
    return {**dict(row), "outstanding_cents": row["amount_cents"] - row["paid_cents"]}

def _get(conn: sqlite3.Connection, tenant: str, order_id: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_ORDER_COLUMNS} FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    return None if row is None else _row_to_order(row)

def accept_order(
    tenant: str,
    order_id: str,
    amount_cents: int,
    currency: str,
    idempotency_key: str,
    request_fingerprint: str,
) -> tuple[dict, bool]:
    """在单事务内受理订单。返回（订单对象, 是否为重放）。

    - 首次见到（租户, 幂等键）：插入订单并登记幂等键，二者同生共死。
    - 同键同指纹：不写任何数据，返回首次受理的订单，replayed=True。
    - 同键异指纹：抛 IdempotencyConflict，不动首次受理的任何数据。
    - 幂等键未见过但（租户, 订单标识）已存在：抛 OrderAlreadyAccepted。
    """
    conn = connect()
    try:
        # 立即拿写锁：两个携带相同幂等键的并发请求在此串行化，
        # 后到者一定能看到先到者已提交的幂等记录。
        conn.execute("BEGIN IMMEDIATE")
        seen = conn.execute(
            "SELECT request_fingerprint, order_id FROM idempotency_keys WHERE tenant=? AND idempotency_key=?",
            (tenant, idempotency_key),
        ).fetchone()
        if seen is not None:
            # 重放或冲突都只是只读判定，先结束事务，再按指纹给确定结论。
            conn.execute("COMMIT")
            order = _get(conn, tenant, seen["order_id"])
            if seen["request_fingerprint"] != request_fingerprint:
                raise IdempotencyConflict(
                    f"idempotency key {idempotency_key!r} reused with a different request fingerprint"
                )
            return order, True
        try:
            conn.execute(
                "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) "
                "VALUES(?,?,?,0,?,'accepted')",
                (tenant, order_id, amount_cents, currency),
            )
            conn.execute(
                "INSERT INTO idempotency_keys(tenant, idempotency_key, request_fingerprint, order_id) "
                "VALUES(?,?,?,?)",
                (tenant, idempotency_key, request_fingerprint, order_id),
            )
        except sqlite3.IntegrityError as error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if "UNIQUE constraint failed: orders" in str(error):
                raise OrderAlreadyAccepted(
                    f"order {order_id!r} already accepted for tenant {tenant!r}"
                ) from error
            raise
        conn.execute("COMMIT")
        return _get(conn, tenant, order_id), False
    except IdempotencyConflict:
        raise
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        return _get(conn, tenant, order_id)
    finally:
        conn.close()

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
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get(tenant, order_id)
