from app.store.db import connect

# Installment payment plans.
#   - A plan can only be created while the order has received no payment, and
#     the installments must sum exactly to the order amount.
#   - Cancelling a plan is likewise only allowed before any payment; it simply
#     removes the installments and never touches order money or status.
#   - With a plan in place, a payment must settle a consecutive run of
#     installments exactly (from the first unsettled one on); partial
#     installments are refused and nothing is written.

class PlanError(Exception):
    """A refusal that leaves the order and every installment untouched."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code

def _fetch_order(conn, tenant: str, order_id: str):
    return conn.execute(
        "SELECT amount_cents, paid_cents, currency FROM orders "
        "WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()

def _fetch_items(conn, tenant: str, order_id: str):
    return conn.execute(
        "SELECT term_id, seq, amount_cents, paid_cents FROM payment_plan_items "
        "WHERE tenant=? AND order_id=? ORDER BY seq",
        (tenant, order_id),
    ).fetchall()

def _has_plan(conn, tenant: str, order_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM payment_plan_items WHERE tenant=? AND order_id=? LIMIT 1",
        (tenant, order_id),
    ).fetchone() is not None

def create_plan(tenant: str, order_id: str, items: list[tuple[str, int]]) -> dict | None:
    """Create an installment plan. items are (term_id, amount_cents) in order.

    Returns the plan view, or None when the order is invisible to this tenant.
    Raises PlanError on any rule violation; the whole plan is refused and no
    installment is written.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = _fetch_order(conn, tenant, order_id)
        if order is None:
            conn.execute("ROLLBACK")
            return None
        term_ids = [term_id for term_id, _ in items]
        if not items:
            raise PlanError("invalid_plan", "plan must contain at least one installment")
        if len(set(term_ids)) != len(term_ids):
            raise PlanError("invalid_plan", "term id must be unique within the order")
        if any(amount <= 0 for _, amount in items):
            raise PlanError("invalid_plan", "installment amount must be greater than zero")
        if sum(amount for _, amount in items) != order["amount_cents"]:
            raise PlanError("invalid_plan", "installments must sum to the order amount")
        if order["paid_cents"] > 0:
            raise PlanError("order_state", "order already received payments")
        if _has_plan(conn, tenant, order_id):
            raise PlanError("plan_exists", "order already has a payment plan")
        for seq, (term_id, amount) in enumerate(items):
            conn.execute(
                "INSERT INTO payment_plan_items(tenant, order_id, term_id, seq, amount_cents) "
                "VALUES(?,?,?,?,?)",
                (tenant, order_id, term_id, seq, amount),
            )
        conn.execute("COMMIT")
    except PlanError:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get_plan(tenant, order_id)

def cancel_plan(tenant: str, order_id: str) -> bool | None:
    """Cancel the plan. None: order invisible; False is never returned —
    rule violations raise PlanError. Returns True when the plan was removed."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = _fetch_order(conn, tenant, order_id)
        if order is None:
            conn.execute("ROLLBACK")
            return None
        if not _has_plan(conn, tenant, order_id):
            raise PlanError("plan_not_found", "order has no payment plan")
        if order["paid_cents"] > 0:
            raise PlanError("order_state", "order already received payments")
        conn.execute(
            "DELETE FROM payment_plan_items WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        )
        conn.execute("COMMIT")
    except PlanError:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return True

def get_plan(tenant: str, order_id: str) -> dict | None:
    """Plan view with per-installment progress. None when the order is
    invisible to this tenant or has no plan."""
    conn = connect()
    try:
        order = _fetch_order(conn, tenant, order_id)
        if order is None:
            return None
        rows = _fetch_items(conn, tenant, order_id)
    finally:
        conn.close()
    if not rows:
        return None
    items = [
        {
            "term_id": row["term_id"],
            "amount_cents": row["amount_cents"],
            "paid_cents": row["paid_cents"],
            "settled": row["paid_cents"] >= row["amount_cents"],
        }
        for row in rows
    ]
    return {
        "tenant": tenant,
        "order_id": order_id,
        "currency": order["currency"],
        "planned_cents": sum(item["amount_cents"] for item in items),
        "settled_count": sum(1 for item in items if item["settled"]),
        "items": items,
    }

def apply_payment(conn, tenant: str, order_id: str, amount_cents: int) -> bool:
    """Apply a payment to the order's plan inside an open write transaction.

    Returns False when the order has no plan (caller falls back to plain
    payment). Raises ValueError when the amount does not equal the remaining
    amount of one or more consecutive installments starting at the first
    unsettled one; nothing is written in that case.
    """
    items = _fetch_items(conn, tenant, order_id)
    if not items:
        return False
    remaining = amount_cents
    for item in items:
        if remaining == 0:
            break
        due = item["amount_cents"] - item["paid_cents"]
        if due == 0:
            continue
        if remaining < due:
            raise ValueError(
                "payment must settle a whole number of consecutive installments"
            )
        remaining -= due
        conn.execute(
            "UPDATE payment_plan_items SET paid_cents=? "
            "WHERE tenant=? AND order_id=? AND term_id=?",
            (item["amount_cents"], tenant, order_id, item["term_id"]),
        )
    if remaining > 0:
        raise ValueError("payment exceeds the planned schedule")
    return True
