from app.store.db import connect

ORDER_COLUMNS = (
    "tenant, order_id, amount_cents, paid_cents, refunded_cents, pending_refund_cents, "
    "currency, status"
)


def recalculate_status(conn, tenant: str, order_id: str) -> None:
    """在写事务内按净额重算订单状态：
    - 被拒订单保持 rejected；从未收款保持 accepted；
    - 净额等于订单金额 → settled；净额为零且曾收款 → unsettled；
    - 中间态：曾从结清退回的保留 unsettled，其余（如部分收款）维持 accepted。"""
    conn.execute(
        "UPDATE orders SET status = CASE "
        "WHEN status = 'rejected' THEN 'rejected' "
        "WHEN paid_cents = 0 AND refunded_cents = 0 THEN 'accepted' "
        "WHEN paid_cents - refunded_cents >= amount_cents THEN 'settled' "
        "WHEN paid_cents - refunded_cents = 0 THEN 'unsettled' "
        "WHEN status = 'settled' THEN 'unsettled' "
        "ELSE status END "
        "WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    )

def _row_to_order(row) -> dict:
    order = dict(row)
    order["outstanding_cents"] = row["amount_cents"] - row["paid_cents"]
    order["net_cents"] = row["paid_cents"] - row["refunded_cents"]
    return order

def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, refunded_cents, pending_refund_cents, currency, status) "
            "VALUES(?,?,?,0,0,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
    finally:
        conn.close()

def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {ORDER_COLUMNS} FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _row_to_order(row)

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
        # 收款不得超过未收金额 = 订单金额 − 累计已收（累计已收只增，退款不回改该列）
        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
            conn.execute("ROLLBACK")
            raise ValueError("payment exceeds outstanding amount")
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ? WHERE tenant=? AND order_id=?",
            (amount_cents, tenant, order_id),
        )
        recalculate_status(conn, tenant, order_id)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)
