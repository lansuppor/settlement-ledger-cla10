import sqlite3

from app.store import plans
from app.store.db import connect


# Status is derived from the money position:
#   net = paid_cents - refunded_cents
#   - never paid, or 0 < net < amount  -> 'accepted' (partially settled)
#   - net == amount                     -> 'settled'
#   - net == 0 after money was taken    -> 'open' (fully refunded, can take payment again)
def derive_status(amount_cents: int, paid_cents: int, refunded_cents: int) -> str:
    net = paid_cents - refunded_cents
    if paid_cents > 0 and net == 0:
        return "open"
    if net == amount_cents:
        return "settled"
    return "accepted"

def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) "
            "VALUES(?,?,?,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
    finally:
        conn.close()

def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, refunded_cents, "
            "pending_refund_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _summary(row)

def _summary(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["outstanding_cents"] = data["amount_cents"] - data["paid_cents"]
    data["net_cents"] = data["paid_cents"] - data["refunded_cents"]
    return data

def add_payment(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        new_paid = row["paid_cents"] + amount_cents
        if amount_cents <= 0 or new_paid > row["amount_cents"]:
            conn.execute("ROLLBACK")
            raise ValueError("payment exceeds outstanding amount")
        # When a plan exists the money must land on whole consecutive
        # installments; a rejected distribution rolls back together with the
        # (not yet applied) order update, so no installment or order moves.
        try:
            plans.apply_payment_to_plan(conn, tenant, order_id, amount_cents)
        except plans.PlanError as error:
            conn.execute("ROLLBACK")
            raise ValueError(str(error))
        status = derive_status(row["amount_cents"], new_paid, row["refunded_cents"])
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_paid, status, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)
