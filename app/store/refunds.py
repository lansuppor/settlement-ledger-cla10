from app.store import orders as order_store
from app.store.db import connect

# 可区分的拒绝原因（写入 refunds.rejection_reason）
REASON_AMOUNT_EXCEEDS_PAID = "amount_exceeds_paid"
REASON_ORDER_STATUS_REJECTED = "order_status_rejected"
REASON_OPERATOR_REJECTED = "operator_rejected"

# 仅用于接口层、不落单据的冲突原因
REASON_REQUEST_CONFLICT = "request_conflict"
REASON_INVALID_REFUND_STATUS = "invalid_refund_status"

REFUND_COLUMNS = (
    "tenant, refund_request_id, order_id, amount_cents, currency, status, "
    "rejection_reason, created_at, completed_at, rejected_at"
)


class RefundError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _row_to_refund(row) -> dict:
    return dict(row)


def get(tenant: str, refund_request_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {REFUND_COLUMNS} FROM refunds WHERE tenant=? AND refund_request_id=?",
            (tenant, refund_request_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _row_to_refund(row)


def list_for_order(tenant: str, order_id: str) -> list[dict] | None:
    """订单不存在（含跨租户）返回 None。"""
    conn = connect()
    try:
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return None
        rows = conn.execute(
            f"SELECT {REFUND_COLUMNS} FROM refunds WHERE tenant=? AND order_id=? ORDER BY created_at, refund_request_id",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_refund(row) for row in rows]


def _persist_rejected(conn, tenant: str, order_id: str, refund_request_id: str,
                      amount_cents: int, currency: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO refunds(tenant, refund_request_id, order_id, amount_cents, currency, status, rejection_reason, rejected_at) "
        "VALUES(?,?,?,?,?,'rejected',?,datetime('now'))",
        (tenant, refund_request_id, order_id, amount_cents, currency, reason),
    )


def create(tenant: str, order_id: str, refund_request_id: str, amount_cents: int) -> tuple[dict, bool]:
    """受理退款。返回 (退款单, 是否首次创建)。

    订单不存在（含跨租户）返回 None；业务冲突抛 RefundError。
    全部写操作在单个 BEGIN IMMEDIATE 事务内完成，失败整体回滚。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            "SELECT order_id, amount_cents, paid_cents, refunded_cents, pending_refund_cents, currency, status "
            "FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None

        existing = conn.execute(
            f"SELECT {REFUND_COLUMNS} FROM refunds WHERE tenant=? AND refund_request_id=?",
            (tenant, refund_request_id),
        ).fetchone()
        if existing is not None:
            # 请求标识是唯一去重身份：内容一致则重放首次结果，不一致则冲突拒绝，绝不改写首次单据
            if existing["order_id"] != order_id or existing["amount_cents"] != amount_cents:
                conn.execute("ROLLBACK")
                raise RefundError(REASON_REQUEST_CONFLICT, "refund request id was already submitted with different content")
            conn.execute("ROLLBACK")
            if existing["status"] == "rejected":
                raise RefundError(
                    existing["rejection_reason"] or REASON_OPERATOR_REJECTED,
                    "refund request was already rejected",
                )
            return _row_to_refund(existing), False

        currency = order["currency"]
        if order["status"] == "rejected":
            _persist_rejected(conn, tenant, order_id, refund_request_id, amount_cents, currency,
                              REASON_ORDER_STATUS_REJECTED)
            conn.execute("COMMIT")
            raise RefundError(REASON_ORDER_STATUS_REJECTED, "order is rejected")

        if amount_cents <= 0:
            conn.execute("ROLLBACK")
            raise RefundError(REASON_AMOUNT_EXCEEDS_PAID, "refund amount must be positive")

        # 守恒：已受理未决退款占用可支配收款；可退额度 = 已收 − 已退 − 未决占额
        available = order["paid_cents"] - order["refunded_cents"] - order["pending_refund_cents"]
        if amount_cents > available:
            _persist_rejected(conn, tenant, order_id, refund_request_id, amount_cents, currency,
                              REASON_AMOUNT_EXCEEDS_PAID)
            conn.execute("COMMIT")
            raise RefundError(REASON_AMOUNT_EXCEEDS_PAID, "refund exceeds paid amount")

        conn.execute(
            "INSERT INTO refunds(tenant, refund_request_id, order_id, amount_cents, currency, status) "
            "VALUES(?,?,?,?,?,'accepted')",
            (tenant, refund_request_id, order_id, amount_cents, currency),
        )
        conn.execute(
            "UPDATE orders SET pending_refund_cents = pending_refund_cents + ? WHERE tenant=? AND order_id=?",
            (amount_cents, tenant, order_id),
        )
        order_store.recalculate_status(conn, tenant, order_id)
        conn.execute("COMMIT")
    except RefundError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get(tenant, refund_request_id), True


def advance(tenant: str, refund_request_id: str, action: str, reason: str | None = None) -> dict | None:
    """完成（complete）或拒绝（reject）已受理退款单。

    不存在（含跨租户）返回 None；状态不允许抛 RefundError。
    """
    if action not in ("complete", "reject"):
        raise ValueError("invalid refund action")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        refund = conn.execute(
            f"SELECT {REFUND_COLUMNS} FROM refunds WHERE tenant=? AND refund_request_id=?",
            (tenant, refund_request_id),
        ).fetchone()
        if refund is None:
            conn.execute("ROLLBACK")
            return None
        if refund["status"] != "accepted":
            conn.execute("ROLLBACK")
            raise RefundError(REASON_INVALID_REFUND_STATUS, f"refund is {refund['status']}, only accepted refunds can {action}")

        amount = refund["amount_cents"]
        order_id = refund["order_id"]
        if action == "complete":
            # 完成：未决占额转为累计已退；累计已收列只增不减，冲减体现在净额 = 已收 − 已退
            conn.execute(
                "UPDATE orders SET pending_refund_cents = pending_refund_cents - ?, "
                "refunded_cents = refunded_cents + ? WHERE tenant=? AND order_id=?",
                (amount, amount, tenant, order_id),
            )
            conn.execute(
                "UPDATE refunds SET status='completed', completed_at=datetime('now') "
                "WHERE tenant=? AND refund_request_id=?",
                (tenant, refund_request_id),
            )
        else:
            # 拒绝：释放未决占额，记录可区分的拒绝原因
            conn.execute(
                "UPDATE orders SET pending_refund_cents = pending_refund_cents - ? WHERE tenant=? AND order_id=?",
                (amount, tenant, order_id),
            )
            conn.execute(
                "UPDATE refunds SET status='rejected', rejection_reason=?, rejected_at=datetime('now') "
                "WHERE tenant=? AND refund_request_id=?",
                (reason or REASON_OPERATOR_REJECTED, tenant, refund_request_id),
            )
        order_store.recalculate_status(conn, tenant, order_id)
        conn.execute("COMMIT")
    except RefundError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get(tenant, refund_request_id)
