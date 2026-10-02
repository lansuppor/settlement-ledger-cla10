"""分期收款登记与分期冲正。

分期使用独立于整单收款（payments / payment_reversals）的标识与记录空间，
但与整单收款共用订单的 paid_cents：分期计入订单已收，冲正分期把相应金额抵回，
任一时刻 amount_cents = paid_cents + outstanding_cents。

幂等判定沿用整单受理与收款冲正的同一套规则：
  - 分期业务标识 installment_key 以（租户, 标识）只允许生效一次，与订单标识、
    收款记录标识、服务分配的分期记录标识的唯一性相互独立；
  - 同标识同指纹为重放：不新增分期、不改变订单与既有分期，返回首次登记的结果快照；
  - 同标识不同指纹为标识被复用给不同业务内容：拒绝（422），绝不覆盖首次记录。
所有判定与金额变动在同一个 BEGIN IMMEDIATE 事务内完成，提交前崩溃整体回滚。
"""
import json
import os
import secrets
import sqlite3

from app.store.db import connect

_IntegrityError = sqlite3.IntegrityError


class FingerprintConflict(Exception):
    """同一（租户, 分期业务标识/冲正标识）被用于不同请求指纹 —— HTTP 422。"""


class InstallmentAlreadyReversed(Exception):
    """分期已被冲正：(租户, installment_id) 唯一约束冲突 —— HTTP 409。"""


def register_installment(
    tenant: str,
    order_id: str,
    installment_key: str,
    request_fingerprint: str,
    amount_cents: int,
) -> tuple[dict, bool] | None:
    """登记一笔分期收款。返回（首次登记结果快照, 是否为重放）；订单不存在返回 None。

    分期写入、订单已收/未收/状态变动与幂等记录（快照即存于分期行）在同一
    BEGIN IMMEDIATE 事务内提交：并发登记由写锁串行化，超过未收金额必被拒绝；
    崩溃在提交前整体回滚，重启后以同一业务标识重试结论与未崩溃时一致。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        recorded = conn.execute(
            "SELECT request_fingerprint, response_snapshot FROM installments "
            "WHERE tenant=? AND installment_key=?",
            (tenant, installment_key),
        ).fetchone()
        if recorded is not None:
            # 业务标识已消费：只比对指纹，不新增分期、不改变订单金额与既有分期。
            conn.execute("ROLLBACK")
            if recorded["request_fingerprint"] != request_fingerprint:
                raise FingerprintConflict
            return json.loads(recorded["response_snapshot"]), True

        order = conn.execute(
            "SELECT amount_cents, paid_cents, currency FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            # 跨租户与不存在统一表现为“不存在”，不消耗分期业务标识。
            conn.execute("ROLLBACK")
            return None
        if amount_cents <= 0 or order["paid_cents"] + amount_cents > order["amount_cents"]:
            # 分期登记超过未收金额：拒绝，不改变订单与任何分期记录。
            conn.execute("ROLLBACK")
            raise ValueError("installment exceeds outstanding amount")

        # SET 右侧均取旧行值：已收增加，未收随之下降；未收清零为已结清，否则仍受理态。
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, "
            "status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        installment_id = _insert_installment(
            conn, tenant, order_id, installment_key, amount_cents, request_fingerprint
        )
        order_row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status "
            "FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        snapshot = {
            "tenant": tenant,
            "installment_id": installment_id,
            "installment_key": installment_key,
            "order_id": order_id,
            "amount_cents": amount_cents,
            "status": "active",
            "order": _order_from_row(order_row),
        }
        # 回填首次登记结果快照：重放时原样返回，与首次完全一致。
        conn.execute(
            "UPDATE installments SET response_snapshot=? WHERE tenant=? AND installment_id=?",
            (json.dumps(snapshot, ensure_ascii=False), tenant, installment_id),
        )
        if os.environ.get("APP_CRASH_INSTALLMENT_BEFORE_COMMIT") == "1":
            # 崩溃演练：不提交直接硬退出，分期、订单变动随事务一起回滚。
            os._exit(2)
        conn.execute("COMMIT")
        return snapshot, False
    finally:
        conn.close()


def get_installment(tenant: str, installment_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, installment_id, installment_key, order_id, amount_cents, status "
            "FROM installments WHERE tenant=? AND installment_id=?",
            (tenant, installment_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def reverse_installment(
    tenant: str,
    installment_id: str,
    reversal_id: str,
    request_fingerprint: str,
) -> tuple[dict, bool] | None:
    """冲正一笔已登记分期。返回（首次冲正结果快照, 是否为重放）；分期不存在返回 None。

    两张唯一约束在同一 BEGIN IMMEDIATE 事务内判定，结论可区分：
      - (tenant, reversal_id) 命中：同指纹返回首次快照（重放，不重复抵回）；
        不同指纹抛 FingerprintConflict（422），绝不覆盖首次记录。
      - (tenant, installment_id) 命中（分期已是 reversed）：
        抛 InstallmentAlreadyReversed（409，含换新冲正标识的再次冲正）。
    分期状态翻转、订单已收/未收/状态重算、冲正记录落库原子提交：
    崩溃在提交前整体回滚，重启后以同一冲正标识重试等价于首次冲正。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        recorded = conn.execute(
            "SELECT request_fingerprint, response_snapshot FROM installment_reversals "
            "WHERE tenant=? AND reversal_id=?",
            (tenant, reversal_id),
        ).fetchone()
        if recorded is not None:
            # 冲正标识已消费：只比对指纹，绝不重复抵回金额，绝不改写首次冲正记录。
            conn.execute("ROLLBACK")
            if recorded["request_fingerprint"] != request_fingerprint:
                raise FingerprintConflict
            return json.loads(recorded["response_snapshot"]), True

        installment = conn.execute(
            "SELECT installment_key, order_id, amount_cents, status FROM installments "
            "WHERE tenant=? AND installment_id=?",
            (tenant, installment_id),
        ).fetchone()
        if installment is None:
            # 跨租户与不存在统一表现为“不存在”，不改变订单与分期，也不消耗冲正标识。
            conn.execute("ROLLBACK")
            return None
        if installment["status"] == "reversed":
            # 另一冲正标识已生效：(tenant, installment_id) 唯一约束冲突，按已处理拒绝。
            conn.execute("ROLLBACK")
            raise InstallmentAlreadyReversed

        amount = installment["amount_cents"]
        order_id = installment["order_id"]
        conn.execute(
            "UPDATE installments SET status='reversed' WHERE tenant=? AND installment_id=?",
            (tenant, installment_id),
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
            "installment_id": installment_id,
            "reversal_id": reversal_id,
            "amount_cents": amount,
            "status": "reversed",
            "order": _order_from_row(order_row),
        }
        try:
            conn.execute(
                "INSERT INTO installment_reversals"
                "(tenant, reversal_id, installment_id, request_fingerprint, amount_cents, response_snapshot) "
                "VALUES(?,?,?,?,?,?)",
                (
                    tenant,
                    reversal_id,
                    installment_id,
                    request_fingerprint,
                    amount,
                    json.dumps(snapshot, ensure_ascii=False),
                ),
            )
        except _IntegrityError:
            # 并发兜底：预检查之后仍撞上 (tenant, installment_id) 唯一约束，按已处理拒绝。
            conn.execute("ROLLBACK")
            raise InstallmentAlreadyReversed
        if os.environ.get("APP_CRASH_INSTALLMENT_REVERSAL_BEFORE_COMMIT") == "1":
            # 崩溃演练：不提交直接硬退出，分期抵回与冲正记录随事务一起回滚。
            os._exit(2)
        conn.execute("COMMIT")
        return snapshot, False
    finally:
        conn.close()


def _insert_installment(
    conn,
    tenant: str,
    order_id: str,
    installment_key: str,
    amount_cents: int,
    request_fingerprint: str,
) -> str:
    """服务分配 installment_id（同租户内唯一、稳定不变）。极小概率撞号时换号重试。

    调用方业务标识 (tenant, installment_key) 的唯一冲突无需在此处理：
    BEGIN IMMEDIATE 已串行化同库写事务，后来者拿到写锁后其预检查必能读到
    已提交的首次登记并按重放/指纹冲突定论（与 accepted_requests 的写入同理）。
    """
    while True:
        installment_id = "I" + secrets.token_hex(8).upper()
        try:
            conn.execute(
                "INSERT INTO installments"
                "(tenant, installment_id, installment_key, order_id, amount_cents, "
                " request_fingerprint, response_snapshot, status) "
                "VALUES(?,?,?,?,?,?,'','active')",
                (tenant, installment_id, installment_key, order_id, amount_cents, request_fingerprint),
            )
            return installment_id
        except _IntegrityError:
            # 仅可能撞上服务分配标识的极小概率撞号：换号重试。
            continue


def _order_from_row(row) -> dict:
    outstanding = row["amount_cents"] - row["paid_cents"]
    return {**dict(row), "outstanding_cents": outstanding}
