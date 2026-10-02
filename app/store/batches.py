"""批量订单受理的存储与判定。

批次级规则见 migrations/004_batch_orders.sql 与 docs/business-rules.md：
  - 批次标识唯一范围（租户, 批次标识），与行级幂等键、订单标识的唯一性相互独立。
  - 同批次标识同指纹：批次重放（返回首次结果清单，不补做任何一行）或中断续跑
    （只补做尚未受理的行），两者最终结论与一次性完整导入一致。
  - 同批次标识不同指纹：BatchFingerprintConflict（422），绝不覆盖首次导入。
  - 每行在独立事务内原子提交（订单 + 行级幂等记录 + 行结论）：
    中断后已提交行保持已受理，未处理行不留半笔数据。

并发：服务为单进程部署，用进程内登记表保证同一（租户, 批次标识）同时只有一个
调用在判定；其余同指纹调用在登记处排队，前一个结束后依次拿到“重放/冲突”的确定结论。
行事务内另有“行结论已存在则跳过”的落库级兜底，使跨进程/极限交错下结论仍确定。
"""
import json
import os
import sqlite3
import threading

from app.rules import order_rules
from app.store import orders
from app.store.db import connect

OUTCOME_ACCEPTED = "accepted"
OUTCOME_REPLAYED = "replayed"
OUTCOME_INVALID = "rejected_invalid"
OUTCOME_FINGERPRINT = "rejected_fingerprint"
OUTCOME_DUPLICATE = "rejected_duplicate"

_STATUS_RUNNING = "running"
_STATUS_COMPLETED = "completed"

# 进程内导入队列：正在判定的（租户, 批次标识）集合 + 一个条件变量。
# 同一 key 同时只有一个判定者；其他调用阻塞等待，结束后下一个依次进入。
_turn = threading.Condition()
_active: set[tuple[str, str]] = set()


class BatchFingerprintConflict(Exception):
    """同一（租户, 批次标识）被用于不同批次请求指纹 —— HTTP 422。"""


class BatchShapeError(Exception):
    """同指纹批次内容形态不一致（行数变化等调用方摘要错误）—— HTTP 400。"""


def accept_batch(tenant: str, batch_id: str, batch_fingerprint: str, rows: list[dict]) -> dict:
    """受理一批订单。返回批次结果清单（逐行结论按 line_no 定位，计数闭合）。

    调用方需保证 batch_id / batch_fingerprint 为非空字符串、rows 非空（入口模型已约束）。
    """
    key = (tenant, batch_id)
    # 同一（租户, 批次标识）在本进程内串行判定：并发下只有一批真正导入，
    # 其余调用在前一个结束后依次读取批次状态，得到重放或冲突的确定结论。
    _register(key)

    try:
        mode = _claim_batch(key, batch_fingerprint, len(rows))
        if os.environ.get("APP_CRASH_BATCH_AFTER_LINE") == "0":
            # 中断演练：批次登记已提交，但尚未处理任何一行（无行结论、无订单）。
            os._exit(2)

        processed: set[int] = set()
        if mode in ("import", "resume"):
            # 逐行独立事务：任一单行回滚不影响其他行；崩溃只可能丢“未提交的那一行”。
            for index, row in enumerate(rows):
                line_no = index + 1
                if _process_line(tenant, batch_id, line_no, row):
                    processed.add(line_no)
                if os.environ.get("APP_CRASH_BATCH_AFTER_LINE") == str(line_no):
                    # 中断演练：前 line_no 行已逐行提交，后续行尚未开始，无半笔数据。
                    os._exit(2)
            _finalize_if_complete(tenant, batch_id, len(rows))
        return _build_manifest(
            tenant,
            batch_id,
            mode=mode,
            processed_lines=processed,
        )
    finally:
        _release(key)


def _claim_batch(key: tuple[str, str], batch_fingerprint: str, line_count: int) -> str:
    """登记/认领批次，返回本次调用模式：import（首次）| resume（中断续跑）| replay（重放）。

    不同指纹抛 BatchFingerprintConflict；同指纹但行数与首次不一致抛 BatchShapeError。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch = conn.execute(
            "SELECT request_fingerprint, total_lines, status FROM order_batches WHERE tenant=? AND batch_id=?",
            key,
        ).fetchone()
        if batch is None:
            try:
                conn.execute(
                    "INSERT INTO order_batches(tenant, batch_id, request_fingerprint, total_lines, status) "
                    "VALUES(?,?,?,?,?)",
                    (key[0], key[1], batch_fingerprint, line_count, _STATUS_RUNNING),
                )
            except sqlite3.IntegrityError:
                # 跨进程兜底：预检查后另一进程抢先登记，改为读取其批次行判定。
                conn.execute("ROLLBACK")
                return _claim_existing(key, batch_fingerprint, line_count)
            mode = "import"
        else:
            mode = _classify_existing(batch, batch_fingerprint, line_count)
        conn.execute("COMMIT")
        return mode
    finally:
        conn.close()


def _claim_existing(key: tuple[str, str], batch_fingerprint: str, line_count: int) -> str:
    conn = connect()
    try:
        batch = conn.execute(
            "SELECT request_fingerprint, total_lines, status FROM order_batches WHERE tenant=? AND batch_id=?",
            key,
        ).fetchone()
        if batch is None:
            # 另一进程插入后又整体回滚（提交前崩溃）：本次按首次导入登记。
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO order_batches(tenant, batch_id, request_fingerprint, total_lines, status) "
                "VALUES(?,?,?,?,?)",
                (key[0], key[1], batch_fingerprint, line_count, _STATUS_RUNNING),
            )
            conn.execute("COMMIT")
            return "import"
        return _classify_existing(batch, batch_fingerprint, line_count)
    finally:
        conn.close()


def _classify_existing(batch, batch_fingerprint: str, line_count: int) -> str:
    if batch["request_fingerprint"] != batch_fingerprint:
        # 批次标识被复用于不同业务内容：拒绝，绝不覆盖首次导入的订单、行结果与批次记录。
        raise BatchFingerprintConflict
    if batch["total_lines"] != line_count:
        # 同指纹绑定的是整批内容：行数不一致说明调用方摘要与内容不符，按参数不合法拒绝。
        raise BatchShapeError("same batch fingerprint submitted with a different number of lines")
    # 同指纹已完成 → 重放（不补做任何一行）；running → 上一轮中断，续跑（只补做缺失行）。
    return "replay" if batch["status"] == _STATUS_COMPLETED else "resume"


def _process_line(tenant: str, batch_id: str, line_no: int, row: dict) -> bool:
    """处理一行。返回本行是否由本次调用新落结论。

    行结论已存在（续跑/并发兜底）时不重复受理，直接返回 False；
    否则按单笔受理规则判定，并把订单、行级幂等记录与行结论在同一事务内原子提交。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM batch_order_lines WHERE tenant=? AND batch_id=? AND line_no=?",
            (tenant, batch_id, line_no),
        ).fetchone()
        if existing is not None:
            # 上一轮已提交（续跑）或另一连接抢先处理（并发）：结论固定，绝不重复受理。
            conn.execute("ROLLBACK")
            return False

        invalid = _validate_row(row, tenant)
        if invalid is not None:
            _insert_line(conn, tenant, batch_id, line_no, OUTCOME_INVALID, {"detail": invalid})
            conn.execute("COMMIT")
            return True

        try:
            order, replayed = orders._accept_order_locked(
                conn,
                tenant,
                row["idempotency_key"],
                row["request_fingerprint"],
                row["order_id"],
                row["amount_cents"],
                row["currency"],
            )
        except orders.FingerprintConflict:
            # 行幂等键复用给不同指纹：只落本行拒绝结论，不动该行已有订单与收款。
            _insert_line(
                conn,
                tenant,
                batch_id,
                line_no,
                OUTCOME_FINGERPRINT,
                {"detail": "idempotency key reused with a different request fingerprint"},
            )
            conn.execute("COMMIT")
            return True
        except orders.OrderAlreadyAccepted:
            # 订单标识重复：与行级指纹冲突明确区分，不落订单、不改动已有单据。
            _insert_line(
                conn,
                tenant,
                batch_id,
                line_no,
                OUTCOME_DUPLICATE,
                {"detail": "order already accepted"},
            )
            conn.execute("COMMIT")
            return True

        _insert_line(conn, tenant, batch_id, line_no, OUTCOME_REPLAYED if replayed else OUTCOME_ACCEPTED, order)
        conn.execute("COMMIT")
        return True
    finally:
        conn.close()


def _validate_row(row: dict, tenant: str) -> str | None:
    """行内参数校验，字段要求与单笔受理入口一致；不合法返回原因（本行记 rejected_invalid）。"""
    if not isinstance(row, dict):
        return "line must be an object"
    row_tenant = row.get("tenant")
    if not isinstance(row_tenant, str) or not row_tenant:
        return "tenant is required"
    if row_tenant != tenant:
        # 一批只受理同一租户的订单：租户缺失/不一致只拒绝本行，不影响其他行。
        return "line tenant does not match batch tenant"
    if not isinstance(row.get("idempotency_key"), str) or not row["idempotency_key"]:
        return "idempotency_key is required"
    if not isinstance(row.get("request_fingerprint"), str) or not row["request_fingerprint"]:
        return "request_fingerprint is required"
    if not isinstance(row.get("order_id"), str) or not row["order_id"]:
        return "order_id is required"
    amount = row.get("amount_cents")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return "amount_cents must be a positive integer"
    currency = row.get("currency")
    if not isinstance(currency, str):
        return "currency is required"
    try:
        order_rules.assert_currency(currency)
    except ValueError:
        return "unsupported currency"
    return None


def _finalize_if_complete(tenant: str, batch_id: str, total_lines: int) -> None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        landed = conn.execute(
            "SELECT COUNT(*) AS c FROM batch_order_lines WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()["c"]
        if landed == total_lines:
            conn.execute(
                "UPDATE order_batches SET status=? WHERE tenant=? AND batch_id=?",
                (_STATUS_COMPLETED, tenant, batch_id),
            )
            conn.execute("COMMIT")
        else:
            # 有行未落结论（不应发生）：保持 running，等待同指纹续跑补齐。
            conn.execute("ROLLBACK")
    finally:
        conn.close()


def _build_manifest(tenant: str, batch_id: str, *, mode: str, processed_lines: set[int]) -> dict:
    """组装批次结果清单。mode: import（首次）| resume（中断续跑）| replay（批次重放）。"""
    conn = connect()
    try:
        batch = conn.execute(
            "SELECT request_fingerprint, total_lines, status FROM order_batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
        rows = conn.execute(
            "SELECT line_no, outcome, result_snapshot FROM batch_order_lines "
            "WHERE tenant=? AND batch_id=? ORDER BY line_no",
            (tenant, batch_id),
        ).fetchall()
    finally:
        conn.close()

    counts = {
        OUTCOME_ACCEPTED: 0,
        OUTCOME_REPLAYED: 0,
        OUTCOME_INVALID: 0,
        OUTCOME_FINGERPRINT: 0,
        OUTCOME_DUPLICATE: 0,
    }
    lines = []
    for row in rows:
        counts[row["outcome"]] += 1
        if mode == "replay":
            # 纯批次重放：每一行都是首次结果的原样取回。
            line_replayed = True
        elif mode == "resume":
            # 续跑：此前已落结论的行按重放返回，本轮补做的行不是重放。
            line_replayed = row["line_no"] not in processed_lines
        else:
            # 首次导入：所有行均为本轮首次处理。
            line_replayed = False
        lines.append(
            {
                "line_no": row["line_no"],
                "outcome": row["outcome"],
                "replayed": line_replayed,
                "result": json.loads(row["result_snapshot"]),
            }
        )

    total = batch["total_lines"]
    counted = sum(counts.values())
    if counted != total or counted != len(rows):
        # 计数闭合是硬约束：成功 + 重放 + 各拒绝 = 总行数，且与落库行数一致。
        raise RuntimeError(f"batch {batch_id} counts do not close: {counted}/{total}")

    rejected = counts[OUTCOME_INVALID] + counts[OUTCOME_FINGERPRINT] + counts[OUTCOME_DUPLICATE]
    qualified = counts[OUTCOME_ACCEPTED] + counts[OUTCOME_REPLAYED]
    if rejected == 0:
        result_status = "success"
    elif qualified == 0:
        result_status = "failed"
    else:
        result_status = "partial"

    return {
        "tenant": tenant,
        "batch_id": batch_id,
        "request_fingerprint": batch["request_fingerprint"],
        "status": batch["status"],
        "result": result_status,
        "replayed": mode == "replay",
        "resumed": mode == "resume",
        "total_lines": total,
        "counts": {
            "accepted": counts[OUTCOME_ACCEPTED],
            "replayed": counts[OUTCOME_REPLAYED],
            "rejected_invalid": counts[OUTCOME_INVALID],
            "rejected_fingerprint": counts[OUTCOME_FINGERPRINT],
            "rejected_duplicate": counts[OUTCOME_DUPLICATE],
        },
        "lines": lines,
    }


def _insert_line(
    conn,
    tenant: str,
    batch_id: str,
    line_no: int,
    outcome: str,
    result: dict,
) -> None:
    conn.execute(
        "INSERT INTO batch_order_lines(tenant, batch_id, line_no, outcome, result_snapshot) "
        "VALUES(?,?,?,?,?)",
        (tenant, batch_id, line_no, outcome, json.dumps(result, ensure_ascii=False)),
    )


def _register(key: tuple[str, str]) -> None:
    """等待轮到本调用判定 key：已有判定者则阻塞，前一个结束后本调用成为判定者。"""
    with _turn:
        while key in _active:
            _turn.wait()
        _active.add(key)


def _release(key: tuple[str, str]) -> None:
    with _turn:
        _active.discard(key)
        _turn.notify_all()
