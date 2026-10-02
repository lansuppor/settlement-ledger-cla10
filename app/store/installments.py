"""分期收款：一张订单可按多笔分期分别登记已收金额，分期明细可独立冲正。

分期使用独立的标识与记录空间，与整单收款（payments / payment_reversals）互不影响，
但与整单收款共用同一订单已收/未收口径：任一时刻 应收 = 已收 + 未收。

标识与幂等：
  - 业务标识 installment_key 由调用方提供，以（租户, 业务标识）唯一，
    与订单标识、收款记录标识、分期记录标识的唯一性相互独立；同租户下只允许生效一次。
    同标识同指纹为重放：不新增分期、不改变订单金额与既有分期，返回首次登记快照；
    同标识不同指纹为标识被复用于不同业务内容，拒绝（HTTP 422）且不覆盖首次记录。
  - 分期记录标识 installment_id 由服务分配，同租户内唯一、稳定不变，可按标识回读。
  - 分期冲正标识 reversal_id 同样以（租户, 冲正标识）唯一：同指纹重放、不同指纹 422；
    对已冲正分期再次冲正（含换新标识）按已处理 409 拒绝，与 422 明确可区分。

所有判定与金额变动均在单个 BEGIN IMMEDIATE 事务内完成：并发由写锁串行化，
崩溃在提交前整体回滚，重启后以同一标识重试结论与未崩溃时一致。
"""
import json
import os
import secrets
import sqlite3

from app.store.db import connect

_IntegrityError = sqlite3.IntegrityError


class FingerprintConflict(Exception):
    """同一（租户, 业务标识/冲正标识）被用于不同请求指纹 —— HTTP 422。"""


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

    业务标识已消费时只比对指纹，绝不新增分期、绝不改写首次记录与订单金额；
    分期写入与订单已收/状态更新在同一 BEGIN IMMEDIATE 事务内原子提交，
    超额（含并发下抢用同一笔未收额度）直接拒绝，不改变订单与任何分期记录。
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
            # 业务标识已消费：同指纹返回首次快照（重放）；不同指纹拒绝，绝不覆盖。
            conn.execute("ROLLBACK")
            if recorded["request_fingerprint"] != request_fingerprint:
                raise FingerprintConflict
            return json.loads(recorded["response_snapshot"]), True

        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            # 跨租户与不存在统一表现为“不存在”，不消耗业务标识。
            conn.execute("ROLLBACK")
            return None
        if amount_cents <= 0 or order["paid_cents"] + amount_cents > order["amount_cents"]:
            # 分期登记超过未收金额：拒绝，不改变订单与任何分期记录。
            conn.execute("ROLLBACK")
            raise ValueError("installment exceeds outstanding amount")

        # SET 右侧的 paid_cents 取旧行值：已收增加，未收随之下降；
        # 未收清零（已收达到订单金额）为已结清，否则保持/回到受理态。
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, "
            "status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        order_row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status "
            "FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()

        def make_snapshot(installment_id: str) -> dict:
            return {
                "tenant": tenant,
                "order_id": order_id,
                "installment_id": installment_id,
                "installment_key": installment_key,
                "amount_cents": amount_cents,
                "status": "active",
                "order": _order_from_row(order_row),
            }

        _installment_id, snapshot = _insert_installment(
            conn, tenant, order_id, installment_key, amount_cents, request_fingerprint, make_snapshot
        )
        if os.environ.get("APP_CRASH_INSTALLMENT_BEFORE_COMMIT") == "1":
            # 崩溃演练：不提交直接硬退出，分期计入与分期记录随事务一起回滚。
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
      - (tenant, installment_id) 命中（分期已是 reversed）：抛 InstallmentAlreadyReversed（409）。
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
            "SELECT installment_key, order_id, amount_cents, status "
            "FROM installments WHERE tenant=? AND installment_id=?",
            (tenant, installment_id),
        ).fetchone()
        if installment is None:
            # 跨租户与不存在统一表现为“不存在”，不改变订单与分期。
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
    make_snapshot,
) -> tuple[str, dict]:
    """服务分配 installment_id（同租户内唯一、稳定不变），极小概率撞号时换号重试。

    调用方持有 BEGIN IMMEDIATE 写锁且已确认 (tenant, installment_key) 不存在，
    故此处唯一可能撞上的是服务分配标识的 UNIQUE 约束；首次快照随 INSERT 一次落库。
    """
    while True:
        installment_id = "I" + secrets.token_hex(8).upper()
        snapshot = make_snapshot(installment_id)
        try:
            conn.execute(
                "INSERT INTO installments"
                "(tenant, installment_key, installment_id, order_id, amount_cents, "
                "request_fingerprint, response_snapshot, status) "
                "VALUES(?,?,?,?,?,?,?,'active')",
                (
                    tenant,
                    installment_key,
                    installment_id,
                    order_id,
                    amount_cents,
                    request_fingerprint,
                    json.dumps(snapshot, ensure_ascii=False),
                ),
            )
            return installment_id, snapshot
        except _IntegrityError:
            continue


def _order_from_row(row) -> dict:
    outstanding = row["amount_cents"] - row["paid_cents"]
    return {**dict(row), "outstanding_cents": outstanding}
