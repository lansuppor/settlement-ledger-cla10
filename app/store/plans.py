"""Payment (installment) plans.

A plan belongs to one order and is made of ordered installments. It may be
created or canceled only while the order has never received any money
(``orders.paid_cents = 0``); the installment amounts must sum exactly to the
order amount and every installment identifier must be unique within the order.

When a plan exists, payments are distributed through installments in order:
each payment must fully settle the remaining amount of one or more consecutive
installments starting at the first unsettled one — a payment that would leave a
partial installment is rejected wholesale.

All write paths run inside a single ``BEGIN IMMEDIATE`` transaction; a rejected
request rolls back and never leaves a partially written plan or installment.
"""

from app.store.db import connect


class PlanError(Exception):
    """A refusal that leaves the order and every installment untouched."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def _fetch_order(conn, tenant: str, order_id: str):
    return conn.execute(
        "SELECT amount_cents, paid_cents, currency FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()


def _plan_exists(conn, tenant: str, order_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM payment_plans WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        is not None
    )


def _fetch_installments(conn, tenant: str, order_id: str) -> list:
    return conn.execute(
        "SELECT install_seq, install_label, amount_cents, paid_cents "
        "FROM payment_plan_installments WHERE tenant=? AND order_id=? ORDER BY install_seq",
        (tenant, order_id),
    ).fetchall()


def create_plan(
    tenant: str, order_id: str, installments: list[tuple[str, int]]
) -> dict | None:
    """Create a payment plan from ``(installment_id, amount_cents)`` pairs.

    Returns the plan view, or None when the order is invisible to the tenant.
    Raises PlanError on any rule violation; nothing is written in that case.
    """
    conn = connect()
    result: dict | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = _fetch_order(conn, tenant, order_id)
        if order is None:
            # Missing and foreign-tenant order share one outcome.
            conn.execute("ROLLBACK")
            return None
        if order["paid_cents"] != 0:
            raise PlanError(
                "order_state",
                "a payment plan can only be created before the order receives any payment",
            )
        if _plan_exists(conn, tenant, order_id):
            raise PlanError("plan_exists", "the order already has a payment plan")

        labels = [label for label, _amount in installments]
        if not labels or any(not label for label in labels):
            raise PlanError("invalid_installment", "each installment needs a non-empty identifier")
        if len(labels) != len(set(labels)):
            raise PlanError(
                "installment_duplicate", "installment identifiers must be unique within the order"
            )
        if any(amount <= 0 for _label, amount in installments):
            raise PlanError("invalid_installment", "each installment amount must be greater than zero")
        if sum(amount for _label, amount in installments) != order["amount_cents"]:
            raise PlanError(
                "plan_total_mismatch",
                "installment amounts must sum exactly to the order amount",
            )

        conn.execute(
            "INSERT INTO payment_plans(tenant, order_id) VALUES(?,?)",
            (tenant, order_id),
        )
        for seq, (label, amount) in enumerate(installments, start=1):
            conn.execute(
                "INSERT INTO payment_plan_installments(tenant, order_id, install_seq, "
                "install_label, amount_cents, paid_cents) VALUES(?,?,?,?,?,0)",
                (tenant, order_id, seq, label, amount),
            )
        conn.execute("COMMIT")
        result = get_plan(tenant, order_id)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return result


def cancel_plan(tenant: str, order_id: str) -> dict | None:
    """Delete the plan of an order that never received money.

    Returns the order summary on success, None when the order is invisible, and
    raises PlanError when money was received or no plan exists.
    """
    # Imported lazily: orders.add_payment imports this module in return.
    from app.store.orders import get as get_order

    conn = connect()
    result: dict | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = _fetch_order(conn, tenant, order_id)
        if order is None:
            conn.execute("ROLLBACK")
            return None
        if order["paid_cents"] != 0:
            raise PlanError(
                "order_state",
                "a payment plan can only be canceled before the order receives any payment",
            )
        if not _plan_exists(conn, tenant, order_id):
            raise PlanError("plan_missing", "the order has no payment plan")
        conn.execute(
            "DELETE FROM payment_plan_installments WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        )
        conn.execute(
            "DELETE FROM payment_plans WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        )
        conn.execute("COMMIT")
        result = get_order(tenant, order_id)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return result


def _installment_view(row) -> dict:
    return {
        "install_seq": row["install_seq"],
        "installment_id": row["install_label"],
        "amount_cents": row["amount_cents"],
        "paid_cents": row["paid_cents"],
        "settled": row["paid_cents"] == row["amount_cents"],
    }


def get_plan(tenant: str, order_id: str) -> dict | None:
    """Return the plan with per-installment progress.

    None covers both an invisible (or missing) order and an order without a
    plan, so callers answer 404 in either case without leaking existence.
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT p.order_id, o.currency FROM payment_plans p "
            "JOIN orders o ON o.tenant = p.tenant AND o.order_id = p.order_id "
            "WHERE p.tenant=? AND p.order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            return None
        rows = _fetch_installments(conn, tenant, order_id)
    finally:
        conn.close()
    installments = [_installment_view(r) for r in rows]
    return {
        "order_id": row["order_id"],
        "currency": row["currency"],
        "planned_amount_cents": sum(item["amount_cents"] for item in installments),
        "received_cents": sum(item["paid_cents"] for item in installments),
        "settled_installments": sum(1 for item in installments if item["settled"]),
        "installments": installments,
    }


def apply_payment_to_plan(
    conn, tenant: str, order_id: str, amount_cents: int
) -> bool:
    """Distribute a payment across installments inside the caller's transaction.

    Returns False when the order has no plan (plain order-level payment).
    With a plan, the amount must equal the remaining amounts of one or more
    consecutive installments from the first unsettled one; otherwise raises
    PlanError("installment_short") and the caller rolls the whole transaction
    back. On success every covered installment is updated to settled in full.
    """
    rows = _fetch_installments(conn, tenant, order_id)
    if not rows:
        return False
    remaining = amount_cents
    for row in rows:
        if remaining == 0:
            break  # the payment closed a whole run of installments; later ones are untouched
        inst_left = row["amount_cents"] - row["paid_cents"]
        if inst_left == 0:
            continue
        if remaining < inst_left:
            # Not enough to finish this installment, or every installment is
            # already settled (money overshoots the plan total).
            raise PlanError(
                "installment_short",
                "payment must settle one or more whole consecutive installments, "
                "without leaving a partial remainder",
            )
        remaining -= inst_left
        conn.execute(
            "UPDATE payment_plan_installments SET paid_cents=? "
            "WHERE tenant=? AND order_id=? AND install_seq=?",
            (row["amount_cents"], tenant, order_id, row["install_seq"]),
        )
    if remaining > 0:
        raise PlanError("installment_short", "payment exceeds the planned installments total")
    return True
