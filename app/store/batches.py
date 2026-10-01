"""批量受理（批次导入）：把一批订单资料一次性受理进系统。

批次以（租户, 批次标识）唯一，与行级幂等键、订单标识的唯一性相互独立：
  - 同批次标识同指纹：整批重放，不重复受理任何一行，返回首次导入的结果清单；
    若首次导入中断（批次仍 processing），则只补做尚未受理的行，已受理行按首次结论返回。
  - 同批次标识不同指纹：批次标识被复用于不同业务内容，拒绝（HTTP 422），
    不覆盖首次导入的任何订单与行结果。
每行遵循与单笔受理一致的规则，行级拒绝只影响本行；每行的订单、行级幂等记录与
行结论在同一事务内原子提交，因此中断后可按行续跑，最终结论与一次性完整导入一致。
"""
import json
import os
import sqlite3
import threading

from app.rules import order_rules
from app.store import orders
from app.store.db import connect

_IntegrityError = sqlite3.IntegrityError


class BatchFingerprintConflict(Exception):
    """同一（租户, 批次标识）被用于不同批次请求指纹 —— HTTP 422。"""


# 行结论：成功 / 重放 / 各拒绝原因。计数按这些键闭合：合计等于总行数。
OUTCOME_ACCEPTED = "accepted"
OUTCOME_REPLAYED = "replayed"
OUTCOME_INVALID = "rejected_invalid"
OUTCOME_FP_CONFLICT = "rejected_fingerprint_conflict"
OUTCOME_ORDER_DUPLICATE = "rejected_order_duplicate"
_OUTCOMES = (
    OUTCOME_ACCEPTED,
    OUTCOME_REPLAYED,
    OUTCOME_INVALID,
    OUTCOME_FP_CONFLICT,
    OUTCOME_ORDER_DUPLICATE,
)

# 同一（租户, 批次标识）在进程内串行：并发批次只允许一批真正导入，
# 另一批在锁后按重放或冲突规则给出确定结论。跨进程由数据库唯一约束兜底。
_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()


def _batch_lock(tenant: str, batch_id: str) -> threading.Lock:
    key = (tenant, batch_id)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def accept_batch(
    tenant: str,
    batch_id: str,
    request_fingerprint: str,
    rows: list[dict],
) -> tuple[dict, bool]:
    """批量受理。返回（批次结果清单, 是否为整批重放）。"""
    with _batch_lock(tenant, batch_id):
        return _accept_batch_locked(tenant, batch_id, request_fingerprint, rows)


def _accept_batch_locked(
    tenant: str,
    batch_id: str,
    request_fingerprint: str,
    rows: list[dict],
) -> tuple[dict, bool]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        recorded = conn.execute(
            "SELECT request_fingerprint, status, response_snapshot FROM import_batches "
            "WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
        if recorded is None:
            try:
                conn.execute(
                    "INSERT INTO import_batches(tenant, batch_id, request_fingerprint, status) "
                    "VALUES(?,?,?,'processing')",
                    (tenant, batch_id, request_fingerprint),
                )
            except _IntegrityError:
                # 跨进程并发兜底：另一进程已登记本批次，回滚后以其记录为准重新判定。
                conn.execute("ROLLBACK")
                return _accept_batch_locked(tenant, batch_id, request_fingerprint, rows)
        elif recorded["request_fingerprint"] != request_fingerprint:
            # 批次标识被复用于不同业务内容：与行级冲突明确区分，不覆盖首次导入的任何数据。
            conn.execute("ROLLBACK")
            raise BatchFingerprintConflict
        elif recorded["status"] == "completed":
            # 整批重放：不重复受理任何一行，返回首次导入的结果清单。
            snapshot = json.loads(recorded["response_snapshot"])
            conn.execute("ROLLBACK")
            return snapshot, True
        # 否则为中断后的续跑：已提交行保持已受理，只补做尚未受理的行。
        conn.execute("COMMIT")
    finally:
        conn.close()

    done = _load_row_results(tenant, batch_id)
    results: list[dict] = [None] * len(rows)
    for index, row in enumerate(rows):
        row_no = index + 1
        if row_no in done:
            # 已受理行按重放返回首次结论，不重新执行。
            results[index] = done[row_no]
            continue
        results[index] = _accept_row(tenant, batch_id, row_no, row)
        if os.environ.get("APP_CRASH_BATCH_AFTER_ROW") == str(row_no):
            # 崩溃演练：本行已提交、批次未完成的时刻硬退出，用于演练中断续跑。
            os._exit(2)

    snapshot = _batch_result(tenant, batch_id, results)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE import_batches SET status='completed', response_snapshot=? "
            "WHERE tenant=? AND batch_id=?",
            (json.dumps(snapshot, ensure_ascii=False), tenant, batch_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return snapshot, False


def _accept_row(tenant: str, batch_id: str, row_no: int, row: dict) -> dict:
    """逐行受理：行订单、行级幂等记录与行结论在同一事务内原子提交。

    行级拒绝只影响本行，不回滚其他已合格的行，也不改动该行已有订单与收款。
    """
    normalized, error = _validate_row(row)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if error is not None:
            result = _row_result(row, row_no, OUTCOME_INVALID, error, None)
        else:
            try:
                order, replayed = orders.accept_order_in_tx(
                    conn,
                    normalized["tenant"],
                    normalized["idempotency_key"],
                    normalized["request_fingerprint"],
                    normalized["order_id"],
                    normalized["amount_cents"],
                    normalized["currency"],
                )
                outcome = OUTCOME_REPLAYED if replayed else OUTCOME_ACCEPTED
                result = _row_result(row, row_no, outcome, None, order)
            except orders.FingerprintConflict:
                result = _row_result(
                    row, row_no, OUTCOME_FP_CONFLICT,
                    "idempotency key reused with a different request fingerprint", None,
                )
            except orders.OrderAlreadyAccepted:
                result = _row_result(row, row_no, OUTCOME_ORDER_DUPLICATE, "order already accepted", None)
        try:
            conn.execute(
                "INSERT INTO import_batch_rows(tenant, batch_id, row_no, outcome, result_snapshot) "
                "VALUES(?,?,?,?,?)",
                (tenant, batch_id, row_no, result["outcome"], json.dumps(result, ensure_ascii=False)),
            )
        except _IntegrityError:
            # 跨进程并发兜底：本行结论已被另一进程提交，以其为准，不重复受理。
            conn.execute("ROLLBACK")
            stored = conn.execute(
                "SELECT result_snapshot FROM import_batch_rows WHERE tenant=? AND batch_id=? AND row_no=?",
                (tenant, batch_id, row_no),
            ).fetchone()
            return json.loads(stored["result_snapshot"])
        if os.environ.get("APP_CRASH_BATCH_BEFORE_ROW_COMMIT") == str(row_no):
            # 崩溃演练：行事务提交前硬退出，本行不留半笔数据。
            os._exit(2)
        conn.execute("COMMIT")
        return result
    finally:
        conn.close()


def _validate_row(row: dict) -> tuple[dict | None, str | None]:
    """行级参数校验：缺幂等键或指纹（含空串）等按参数不合法拒绝本行。"""
    if not isinstance(row, dict):
        return None, "row must be an object"
    for field in ("tenant", "order_id", "idempotency_key", "request_fingerprint"):
        value = row.get(field)
        if not isinstance(value, str) or not value:
            return None, f"missing or empty field: {field}"
    amount = row.get("amount_cents")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return None, "amount_cents must be a positive integer"
    currency = row.get("currency")
    if not isinstance(currency, str) or len(currency) != 3:
        return None, "currency must be a 3-letter code"
    try:
        order_rules.assert_currency(currency)
    except ValueError:
        return None, "unsupported currency"
    normalized = {
        "tenant": row["tenant"],
        "order_id": row["order_id"],
        "amount_cents": amount,
        "currency": currency,
        "idempotency_key": row["idempotency_key"],
        "request_fingerprint": row["request_fingerprint"],
    }
    return normalized, None


def _row_result(row: dict, row_no: int, outcome: str, reason: str | None, order: dict | None) -> dict:
    order_id = row.get("order_id") if isinstance(row, dict) else None
    return {
        "row": row_no,
        "order_id": order_id if isinstance(order_id, str) else None,
        "outcome": outcome,
        "reason": reason,
        "order": order,
    }


def _load_row_results(tenant: str, batch_id: str) -> dict[int, dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT row_no, result_snapshot FROM import_batch_rows WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchall()
    finally:
        conn.close()
    return {row["row_no"]: json.loads(row["result_snapshot"]) for row in rows}


def _batch_result(tenant: str, batch_id: str, results: list[dict]) -> dict:
    counts = {outcome: 0 for outcome in _OUTCOMES}
    for result in results:
        counts[result["outcome"]] += 1
    total = len(results)
    rejected = total - counts[OUTCOME_ACCEPTED] - counts[OUTCOME_REPLAYED]
    if rejected == 0:
        status = "accepted"
    elif counts[OUTCOME_ACCEPTED] + counts[OUTCOME_REPLAYED] > 0:
        status = "partial"
    else:
        status = "failed"
    return {
        "tenant": tenant,
        "batch_id": batch_id,
        "status": status,
        "total": total,
        **counts,
        "rows": results,
    }
