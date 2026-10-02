import json
import os
import secrets
import sqlite3

from app.store.db import connect

_IntegrityError = sqlite3.IntegrityError


class OrderAlreadyAccepted(Exception):
    """同一（租户, 订单标识）已受理过 —— HTTP 409。"""


class FingerprintConflict(Exception):
    """同一（租户, 幂等键/冲正标识）被用于不同请求指纹 —— HTTP 422。"""


class PaymentAlreadyReversed(Exception):
    """收款已被冲正：(租户, payment_id) 唯一约束冲突 —— HTTP 409。"""


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
        try:
            order, replayed = _accept_order_locked(
                conn, tenant, idempotency_key, request_fingerprint, order_id, amount_cents, currency
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if not replayed and os.environ.get("APP_CRASH_BEFORE_COMMIT") == "1":
            # 崩溃演练：不提交直接硬退出，未提交事务随连接被丢弃。
            os._exit(2)
        conn.execute("COMMIT")
        return order, replayed
    finally:
        conn.close()


def _accept_order_locked(
    conn: sqlite3.Connection,
    tenant: str,
    idempotency_key: str,
    request_fingerprint: str,
    order_id: str,
    amount_cents: int,
    currency: str,
) -> tuple[dict, bool]:
    """单笔受理规则（调用方已持有 BEGIN IMMEDIATE 事务，提交/回滚由调用方负责）。

    批量受理逐行复用本函数，保证行级判定与单笔入口完全一致：
      - 同键同指纹：返回（首次订单快照, True），不写任何数据；
      - 同键不同指纹：抛 FingerprintConflict（尚未发生任何写入）；
      - 订单标识重复：抛 OrderAlreadyAccepted（尚未发生任何写入）；
      - 首次：在当前事务内写入订单与幂等记录，返回（订单对象, False）。
    """
    recorded = conn.execute(
        "SELECT request_fingerprint, response_snapshot FROM accepted_requests "
        "WHERE tenant=? AND idempotency_key=?",
        (tenant, idempotency_key),
    ).fetchone()
    if recorded is not None:
        # 幂等键已消费：只比对指纹，绝不改写首次受理的任何记录。
        if recorded["request_fingerprint"] != request_fingerprint:
            raise FingerprintConflict
        return json.loads(recorded["response_snapshot"]), True

    if conn.execute(
        "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone() is not None:
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
    return order, False


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
    return _order_from_row(row)


def add_payment(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    """登记收款。收款不经幂等键机制；payment_id 由服务在事务内分配。

    返回订单对象（含本次 payment_id）；订单不存在返回 None；超额抛 ValueError。
    """
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
        payment_id = _insert_payment(conn, tenant, order_id, amount_cents)
        conn.execute("COMMIT")
    finally:
        conn.close()
    order = get(tenant, order_id)
    order["payment_id"] = payment_id
    return order


def get_payment(tenant: str, payment_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, payment_id, order_id, amount_cents, status FROM payments WHERE tenant=? AND payment_id=?",
            (tenant, payment_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def reverse_payment(
    tenant: str,
    payment_id: str,
    reversal_id: str,
    request_fingerprint: str,
) -> tuple[dict, bool] | None:
    """可撤销地冲正一笔已登记收款。返回（首次冲正结果快照, 是否为重放）；收款不存在返回 None。

    两张唯一约束在同一 BEGIN IMMEDIATE 事务内判定，结论可区分：
      - (tenant, reversal_id) 命中：同指纹返回首次快照（重放，不重复抵回）；
        不同指纹抛 FingerprintConflict（422），绝不覆盖首次记录。
      - (tenant, payment_id) 命中（收款已是 reversed）：抛 PaymentAlreadyReversed（409）。
    收款状态翻转、订单已收/未收/状态重算、冲正记录落库原子提交：
    崩溃在提交前整体回滚，重启后以同一冲正标识重试等价于首次冲正。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        recorded = conn.execute(
            "SELECT request_fingerprint, response_snapshot FROM payment_reversals "
            "WHERE tenant=? AND reversal_id=?",
            (tenant, reversal_id),
        ).fetchone()
        if recorded is not None:
            # 冲正标识已消费：只比对指纹，绝不重复抵回金额，绝不改写首次冲正记录。
            conn.execute("ROLLBACK")
            if recorded["request_fingerprint"] != request_fingerprint:
                raise FingerprintConflict
            return json.loads(recorded["response_snapshot"]), True

        payment = conn.execute(
            "SELECT order_id, amount_cents, status FROM payments WHERE tenant=? AND payment_id=?",
            (tenant, payment_id),
        ).fetchone()
        if payment is None:
            # 跨租户与不存在统一表现为“不存在”，不改变订单与收款。
            conn.execute("ROLLBACK")
            return None
        if payment["status"] == "reversed":
            # 另一冲正标识已生效：(tenant, payment_id) 唯一约束冲突，按已处理拒绝。
            conn.execute("ROLLBACK")
            raise PaymentAlreadyReversed

        amount = payment["amount_cents"]
        order_id = payment["order_id"]
        conn.execute(
            "UPDATE payments SET status='reversed' WHERE tenant=? AND payment_id=?",
            (tenant, payment_id),
        )
        # SET 右侧的 paid_cents 均取旧行值：已收抵回，未收随之回升；
        # 未收清零（已收回到订单金额）为已结清，否则回到受理态。
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents - ?, "
            "status = CASE WHEN paid_cents - ? = amount_cents THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=?",
            (amount, amount, tenant, order_id),
        )
        order_row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status "
            "FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        snapshot = {
            "tenant": tenant,
            "payment_id": payment_id,
            "reversal_id": reversal_id,
            "amount_cents": amount,
            "status": "reversed",
            "order": _order_from_row(order_row),
        }
        try:
            conn.execute(
                "INSERT INTO payment_reversals"
                "(tenant, reversal_id, payment_id, request_fingerprint, amount_cents, response_snapshot) "
                "VALUES(?,?,?,?,?,?)",
                (
                    tenant,
                    reversal_id,
                    payment_id,
                    request_fingerprint,
                    amount,
                    json.dumps(snapshot, ensure_ascii=False),
                ),
            )
        except _IntegrityError:
            # 并发兜底：预检查之后仍撞上 (tenant, payment_id) 唯一约束，按已处理拒绝。
            conn.execute("ROLLBACK")
            raise PaymentAlreadyReversed
        if os.environ.get("APP_CRASH_REVERSAL_BEFORE_COMMIT") == "1":
            # 崩溃演练：不提交直接硬退出，收款抵回与冲正记录随事务一起回滚。
            os._exit(2)
        conn.execute("COMMIT")
        return snapshot, False
    finally:
        conn.close()


def _insert_payment(conn, tenant: str, order_id: str, amount_cents: int) -> str:
    """服务分配 payment_id（同租户内唯一、稳定不变）。极小概率撞号时换号重试。"""
    while True:
        payment_id = "P" + secrets.token_hex(8).upper()
        try:
            conn.execute(
                "INSERT INTO payments(tenant, payment_id, order_id, amount_cents, status) "
                "VALUES(?,?,?,?,'active')",
                (tenant, payment_id, order_id, amount_cents),
            )
            return payment_id
        except _IntegrityError:
            continue


def _order_from_row(row) -> dict:
    outstanding = row["amount_cents"] - row["paid_cents"]
    return {**dict(row), "outstanding_cents": outstanding}
