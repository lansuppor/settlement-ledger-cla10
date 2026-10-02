import uuid

from app.store.db import connect
from app.store.orders import derive_status
from app.store.orders import get as get_order

# Refund lifecycle:
#   accepted            -> completed | rejected
#   accepted            -> awaiting_supplement (returned for a supplement)
#   awaiting_supplement -> accepted (supplement accepted, hold recomputed)
#                         | rejected (hold released)
#   accepted  / awaiting_supplement: amount_cents is held in
#                                    orders.pending_refund_cents
#   completed : hold moves into orders.refunded_cents and reduces order net
#   rejected  : hold is released; reason_code records why
REJECT_REASONS = {"amount_exceeds", "order_state", "request_conflict", "internal_error"}
SUPPLEMENT_REASONS = {"missing_proof", "wrong_account", "amount_mismatch"}

class RefundError(Exception):
    """A refusal that leaves every existing document untouched."""

    def __init__(self, reason_code: str, message: str, refund: dict | None = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.refund = refund

_COLUMNS = (
    "tenant, refund_id, refund_request_id, order_id, amount_cents, currency, "
    "status, reason_code, created_at, updated_at"
)
_EVENT_COLUMNS = (
    "tenant, refund_id, seq, event_type, reason_code, note, "
    "amount_before_cents, amount_after_cents, created_at"
)
# Statuses in which the refund's amount still occupies pending_refund_cents.
_HOLDING_STATUSES = ("accepted", "awaiting_supplement")

def _row_to_dict(row) -> dict:
    amount = row["amount_cents"] if row["status"] in _HOLDING_STATUSES else 0
    return {
        "tenant": row["tenant"],
        "refund_id": row["refund_id"],
        "refund_request_id": row["refund_request_id"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "held_cents": amount,
        "currency": row["currency"],
        "status": row["status"],
        "reason_code": row["reason_code"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

def _event_to_dict(row) -> dict:
    return {
        "refund_id": row["refund_id"],
        "seq": row["seq"],
        "event_type": row["event_type"],
        "reason_code": row["reason_code"],
        "note": row["note"],
        "amount_before_cents": row["amount_before_cents"],
        "amount_after_cents": row["amount_after_cents"],
        "created_at": row["created_at"],
    }

def _fetch_by_request(conn, tenant: str, refund_request_id: str):
    return conn.execute(
        f"SELECT {_COLUMNS} FROM refunds WHERE tenant=? AND refund_request_id=?",
        (tenant, refund_request_id),
    ).fetchone()

def _fetch_by_id(conn, tenant: str, refund_id: str):
    return conn.execute(
        f"SELECT {_COLUMNS} FROM refunds WHERE tenant=? AND refund_id=?",
        (tenant, refund_id),
    ).fetchone()

def _latest_event(conn, tenant: str, refund_id: str):
    return conn.execute(
        f"SELECT {_EVENT_COLUMNS} FROM refund_supplement_events "
        "WHERE tenant=? AND refund_id=? ORDER BY seq DESC LIMIT 1",
        (tenant, refund_id),
    ).fetchone()

def _next_event_seq(conn, tenant: str, refund_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
        "FROM refund_supplement_events WHERE tenant=? AND refund_id=?",
        (tenant, refund_id),
    ).fetchone()
    return row["next_seq"]

def create_refund(
    tenant: str, order_id: str, refund_request_id: str, amount_cents: int
) -> tuple[dict | None, bool]:
    """Accept a refund request. Returns (refund, created).

    Replays the first acceptance when the same (tenant, refund_request_id) is
    resubmitted with identical content; raises RefundError on any mismatch or
    conservation violation; returns (None, False) when the order is invisible
    to this tenant (treated as not found).
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, pending_refund_cents, currency, status "
            "FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None, False
        if order["status"] == "rejected":
            conn.execute("ROLLBACK")
            raise RefundError("order_state", "order is rejected and cannot be refunded")

        existing = _fetch_by_request(conn, tenant, refund_request_id)
        if existing is not None:
            # Idempotent replay: the first acceptance is the only truth.
            if existing["order_id"] == order_id and existing["amount_cents"] == amount_cents:
                conn.execute("ROLLBACK")
                return _row_to_dict(existing), False
            conn.execute("ROLLBACK")
            raise RefundError(
                "request_conflict",
                "refund request id was already accepted with different content",
                refund=_row_to_dict(existing),
            )

        if amount_cents <= 0:
            conn.execute("ROLLBACK")
            raise RefundError("amount_exceeds", "refund amount must be greater than zero")

        # Conservation: completed + held + new must never exceed what was paid.
        held = order["refunded_cents"] + order["pending_refund_cents"]
        if held + amount_cents > order["paid_cents"]:
            conn.execute("ROLLBACK")
            raise RefundError("amount_exceeds", "refund exceeds the paid amount of the order")

        refund_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO refunds(tenant, refund_id, refund_request_id, order_id, "
            "amount_cents, currency, status) VALUES(?,?,?,?,?,?,'accepted')",
            (tenant, refund_id, refund_request_id, order_id, amount_cents, order["currency"]),
        )
        conn.execute(
            "UPDATE orders SET pending_refund_cents = pending_refund_cents + ? "
            "WHERE tenant=? AND order_id=?",
            (amount_cents, tenant, order_id),
        )
        conn.execute("COMMIT")
    except RefundError:
        raise
    except Exception as error:
        # Backstop for a concurrent insert of the same refund_request_id: writers
        # are serialized by BEGIN IMMEDIATE, so this only fires under retries.
        if "UNIQUE" not in str(error):
            raise
        conn.execute("ROLLBACK")
        existing = _fetch_by_request(conn, tenant, refund_request_id)
        if existing is None:
            raise
        if existing["order_id"] == order_id and existing["amount_cents"] == amount_cents:
            return _row_to_dict(existing), False
        raise RefundError(
            "request_conflict",
            "refund request id was already accepted with different content",
            refund=_row_to_dict(existing),
        )
    finally:
        conn.close()
    return get_refund(tenant, refund_id=refund_id)[0], True

def get_refund(
    tenant: str, refund_id: str | None = None, refund_request_id: str | None = None
) -> tuple[dict | None, dict | None]:
    """Returns (refund, order_summary); both are None when invisible to the tenant."""
    conn = connect()
    try:
        if refund_id:
            row = _fetch_by_id(conn, tenant, refund_id)
        elif refund_request_id:
            row = _fetch_by_request(conn, tenant, refund_request_id)
        else:
            raise ValueError("refund_id or refund_request_id is required")
    finally:
        conn.close()
    if row is None:
        return None, None
    return _row_to_dict(row), get_order(tenant, row["order_id"])

def list_for_order(tenant: str, order_id: str) -> list[dict] | None:
    """Lists refunds for an order. None means the order is invisible to the tenant."""
    if get_order(tenant, order_id) is None:
        return None
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM refunds WHERE tenant=? AND order_id=? "
            "ORDER BY created_at, refund_id",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(row) for row in rows]

def list_supplement_events(tenant: str, refund_id: str) -> list[dict] | None:
    """Supplement records for a refund, newest first.

    None means the refund is invisible to the tenant (treated as not found).
    """
    conn = connect()
    try:
        if _fetch_by_id(conn, tenant, refund_id) is None:
            return None
        rows = conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM refund_supplement_events "
            "WHERE tenant=? AND refund_id=? ORDER BY seq DESC",
            (tenant, refund_id),
        ).fetchall()
    finally:
        conn.close()
    return [_event_to_dict(row) for row in rows]

def return_for_supplement(
    tenant: str, refund_id: str, reason_code: str, note: str
) -> tuple[dict | None, bool]:
    """Send an accepted refund back to the customer for a supplement.

    Returns (refund, returned). ``returned`` is False on an idempotent replay
    of the same return (same reason and note). A repeat with different reason or
    note conflicts with 409 and the first return record is never rewritten.
    Returns (None, False) when the refund is invisible to the tenant.
    """
    if reason_code not in SUPPLEMENT_REASONS or not note or not note.strip():
        raise ValueError("invalid supplement reason or note")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch_by_id(conn, tenant, refund_id)
        if row is None:
            conn.execute("ROLLBACK")
            return None, False
        if row["status"] in ("completed", "rejected"):
            raise RefundError("refund_final", f"refund is already {row['status']}")

        if row["status"] == "awaiting_supplement":
            # While waiting, a return is only a replay of the active return
            # (status is awaiting_supplement, so the latest event is its 'returned').
            active = _latest_event(conn, tenant, refund_id)
            if active["reason_code"] == reason_code and active["note"] == note:
                conn.execute("ROLLBACK")
                return _row_to_dict(row), False
            raise RefundError(
                "supplement_conflict",
                "refund is already awaiting a supplement with different reason or note",
            )

        conn.execute(
            "UPDATE refunds SET status='awaiting_supplement', "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        )
        conn.execute(
            "INSERT INTO refund_supplement_events(tenant, refund_id, seq, event_type, "
            "reason_code, note, amount_before_cents, amount_after_cents) "
            "VALUES(?,?,?,'returned',?,?,?,?)",
            (
                tenant,
                refund_id,
                _next_event_seq(conn, tenant, refund_id),
                reason_code,
                note,
                row["amount_cents"],
                row["amount_cents"],
            ),
        )
        conn.execute("COMMIT")
    except RefundError:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get_refund(tenant, refund_id=refund_id)[0], True

def submit_supplement(
    tenant: str, refund_id: str, amount_cents: int
) -> tuple[dict | None, bool]:
    """Submit the supplemented, new refund amount and resume review.

    The hold is recomputed against the new amount under the order conservation
    rule refunded + pending (with this refund's hold replaced) <= paid. A failed
    check rolls everything back and leaves the refund awaiting. A resubmission
    identical to the supplement already accepted replays that result.
    Returns (None, False) when the refund is invisible to the tenant.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch_by_id(conn, tenant, refund_id)
        if row is None:
            conn.execute("ROLLBACK")
            return None, False
        if row["status"] in ("completed", "rejected"):
            raise RefundError("refund_final", f"refund is already {row['status']}")

        latest = _latest_event(conn, tenant, refund_id)
        if row["status"] == "accepted":
            # Not waiting: only a retry of the supplement just accepted replays.
            if latest is not None and latest["event_type"] == "supplemented" \
                    and latest["amount_after_cents"] == amount_cents:
                conn.execute("ROLLBACK")
                return _row_to_dict(row), False
            raise RefundError("supplement_conflict", "refund is not awaiting a supplement")

        if amount_cents <= 0:
            raise RefundError("amount_exceeds", "supplement amount must be greater than zero")

        order = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, pending_refund_cents "
            "FROM orders WHERE tenant=? AND order_id=?",
            (tenant, row["order_id"]),
        ).fetchone()
        old_amount = row["amount_cents"]
        # Replace this refund's hold with the new amount, then re-check conservation.
        new_pending = order["pending_refund_cents"] - old_amount + amount_cents
        if order["refunded_cents"] + new_pending > order["paid_cents"]:
            raise RefundError("amount_exceeds", "refund exceeds the paid amount of the order")

        conn.execute(
            "UPDATE orders SET pending_refund_cents=? WHERE tenant=? AND order_id=?",
            (new_pending, tenant, row["order_id"]),
        )
        conn.execute(
            "UPDATE refunds SET amount_cents=?, status='accepted', reason_code=NULL, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE tenant=? AND refund_id=?",
            (amount_cents, tenant, refund_id),
        )
        # awaiting_supplement: the latest event is the active 'returned'.
        returned = latest
        conn.execute(
            "INSERT INTO refund_supplement_events(tenant, refund_id, seq, event_type, "
            "reason_code, note, amount_before_cents, amount_after_cents) "
            "VALUES(?,?,?,'supplemented',?,?,?,?)",
            (
                tenant,
                refund_id,
                _next_event_seq(conn, tenant, refund_id),
                returned["reason_code"],
                returned["note"],
                old_amount,
                amount_cents,
            ),
        )
        conn.execute("COMMIT")
    except RefundError:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get_refund(tenant, refund_id=refund_id)[0], True

def _transition(
    conn, tenant: str, refund_id: str, target: str, reason_code: str | None
) -> dict:
    row = _fetch_by_id(conn, tenant, refund_id)
    if row["status"] == target:
        return _row_to_dict(row)
    if row["status"] in ("completed", "rejected"):
        raise RefundError("refund_final", f"refund is already {row['status']}")
    if row["status"] == "awaiting_supplement" and target == "completed":
        raise RefundError("refund_state", "refund is awaiting a supplement and cannot complete")

    if target == "completed":
        order = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents FROM orders "
            "WHERE tenant=? AND order_id=?",
            (tenant, row["order_id"]),
        ).fetchone()
        new_refunded = order["refunded_cents"] + row["amount_cents"]
        # The hold was taken at acceptance; this guard is defence in depth.
        if new_refunded > order["paid_cents"]:
            raise RefundError("amount_exceeds", "refund exceeds the paid amount of the order")
        status = derive_status(order["amount_cents"], order["paid_cents"], new_refunded)
        conn.execute(
            "UPDATE orders SET refunded_cents=?, pending_refund_cents = pending_refund_cents - ?, "
            "status=? WHERE tenant=? AND order_id=?",
            (new_refunded, row["amount_cents"], status, tenant, row["order_id"]),
        )
    else:  # rejected: release the hold
        conn.execute(
            "UPDATE orders SET pending_refund_cents = pending_refund_cents - ? "
            "WHERE tenant=? AND order_id=?",
            (row["amount_cents"], tenant, row["order_id"]),
        )
    conn.execute(
        "UPDATE refunds SET status=?, reason_code=?, "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE tenant=? AND refund_id=?",
        (target, reason_code, tenant, refund_id),
    )
    return _row_to_dict(_fetch_by_id(conn, tenant, refund_id))

def complete_refund(tenant: str, refund_id: str) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch_by_id(conn, tenant, refund_id)
        if row is None:
            conn.execute("ROLLBACK")
            return None
        result = _transition(conn, tenant, refund_id, "completed", None)
        conn.execute("COMMIT")
    except RefundError:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get_refund(tenant, refund_id=refund_id)[0] or result

def reject_refund(tenant: str, refund_id: str, reason_code: str) -> dict | None:
    if reason_code not in REJECT_REASONS:
        raise ValueError("unknown reject reason")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _fetch_by_id(conn, tenant, refund_id)
        if row is None:
            conn.execute("ROLLBACK")
            return None
        result = _transition(conn, tenant, refund_id, "rejected", reason_code)
        conn.execute("COMMIT")
    except RefundError:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get_refund(tenant, refund_id=refund_id)[0] or result
