import json
import os

from app.store.db import connect


class OrderAlreadyAccepted(Exception):
    """同一（租户, 订单标识）已受理过 —— HTTP 409。"""


class FingerprintConflict(Exception):
    """同一（租户, 幂等键）被用于不同请求指纹 —— HTTP 422。"""


class ReversalFingerprintConflict(Exception):
    """同一（租户, 冲正标识）被用于不同请求指纹 —— HTTP 422。"""


class PaymentAlreadyReversed(Exception):
    """收款已被一笔冲正抵回，冲正本身不可再被冲正 —— HTTP 409。"""


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


def add_payment(tenant: str, order_id: str, amount_cents: int) -> tuple[dict, str] | None:
    """登记收款，并在同一事务内由服务分配稳定的 payment_id。返回（订单对象, 收款记录标识）。

    订单金额更新与 payments 行写入在同一个 BEGIN IMMEDIATE 事务中：
    并发登记由写锁串行化，超收在同一事务内判定拒绝。
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
        # payment_id 由服务在登记事务内分配：租户内单调序号，稳定不变，不由调用方指定。
        payment_id = _allocate_payment_id(conn, tenant)
        conn.execute(
            "INSERT INTO payments(tenant, payment_id, seq, order_id, amount_cents, status) VALUES(?,?,?,?,?,'active')",
            (tenant, payment_id, int(payment_id), order_id, amount_cents),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id), payment_id


def _allocate_payment_id(conn, tenant: str) -> str:
    """在当前写事务内分配租户内唯一、单调递增的收款记录标识。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM payments WHERE tenant=?",
        (tenant,),
    ).fetchone()
    return str(row["next_seq"])


def get_payment(tenant: str, payment_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, payment_id, order_id, amount_cents, status FROM payments WHERE tenant=? AND payment_id=?",
            (tenant, payment_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return dict(row)


def reverse_payment(
    tenant: str,
    payment_id: str,
    reversal_id: str,
    request_fingerprint: str,
) -> tuple[dict | None, bool]:
    """可撤销的收款冲正。返回（首次冲正结果快照, 是否为重放）；收款不存在时首元素为 None。

    冲正记录写入、收款状态翻转与订单已收回升在同一个 BEGIN IMMEDIATE 事务中：
    并发冲正由写锁与两个唯一约束串行化 —— (tenant, reversal_id) 保证同一冲正
    标识只生效一次，(tenant, payment_id) 保证同一收款至多被抵回一次；崩溃在
    提交前整体回滚、提交后整体可见。
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
            # 冲正标识已消费：只比对指纹，绝不改写首次冲正记录或已登记收款。
            conn.execute("ROLLBACK")
            if recorded["request_fingerprint"] != request_fingerprint:
                raise ReversalFingerprintConflict
            return json.loads(recorded["response_snapshot"]), True

        payment = conn.execute(
            "SELECT order_id, amount_cents, status FROM payments WHERE tenant=? AND payment_id=?",
            (tenant, payment_id),
        ).fetchone()
        if payment is None:
            conn.execute("ROLLBACK")
            return None, False
        if payment["status"] == "reversed":
            # 收款已被（别的冲正标识）抵回；冲正本身不可再被冲正。
            conn.execute("ROLLBACK")
            raise PaymentAlreadyReversed

        order_id, amount = payment["order_id"], payment["amount_cents"]
        # 已收减少、未收相应回升；未收清零（新已收回到全额应收）为结清，否则回到受理态。
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents - ?, "
            "status = CASE WHEN amount_cents - (paid_cents - ?) <= 0 THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=?",
            (amount, amount, tenant, order_id),
        )
        conn.execute(
            "UPDATE payments SET status='reversed' WHERE tenant=? AND payment_id=?",
            (tenant, payment_id),
        )
        # 快照中的订单必须反映本次未提交的更新，故在同一连接同一事务内回读。
        order_row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status "
            "FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        order = {**dict(order_row), "outstanding_cents": order_row["amount_cents"] - order_row["paid_cents"]}
        snapshot = {
            "tenant": tenant,
            "payment_id": payment_id,
            "reversal_id": reversal_id,
            "reversed_amount_cents": amount,
            "order": order,
        }
        conn.execute(
            "INSERT INTO payment_reversals"
            "(tenant, reversal_id, request_fingerprint, payment_id, amount_cents, response_snapshot) "
            "VALUES(?,?,?,?,?,?)",
            (tenant, reversal_id, request_fingerprint, payment_id, amount, json.dumps(snapshot, ensure_ascii=False)),
        )
        if os.environ.get("APP_CRASH_BEFORE_COMMIT") == "1":
            # 崩溃演练：不提交直接硬退出，未提交事务随连接被丢弃。
            os._exit(2)
        conn.execute("COMMIT")
        return snapshot, False
    finally:
        conn.close()
