"""批量受理的端到端测试。

覆盖：完整导入、部分失败、全部失败、批次重放、批次指纹冲突、缺批次参数、
行级各拒绝（缺键/缺指纹/指纹冲突/订单重复/金额币种非法/租户不一致）、
计数闭合与落库一致、中断续跑、同指纹并发唯一导入、异指纹并发冲突、
跨租户批次独立、提交前崩溃恢复。
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test-batch.sqlite"))

import httpx
from fastapi.testclient import TestClient
from uvicorn import Config, Server

from app.entry import app
from app.store import orders
from app.store.db import connect, migrate

migrate()
client = TestClient(app)


def line(order_id: str, key: str, fp: str, *, tenant: str = "t1", amount: int = 100, currency: str = "CNY") -> dict:
    return {
        "tenant": tenant,
        "order_id": order_id,
        "amount_cents": amount,
        "currency": currency,
        "idempotency_key": key,
        "request_fingerprint": fp,
    }


def batch_body(batch_id: str, fp: str, lines_: list[dict]) -> dict:
    return {"batch_id": batch_id, "request_fingerprint": fp, "lines": lines_}


def post_batch(body: dict):
    return client.post("/orders/batch", json=body)


def outcome_map(resp_json: dict) -> dict[int, str]:
    return {item["line_no"]: item["outcome"] for item in resp_json["lines"]}


# ---------- 完整导入 / 部分失败 / 全部失败 ----------

def test_full_import_succeeds_and_orders_land() -> None:
    body = batch_body("B-FULL", "fp-full", [line("BF-1", "bk1", "bfp1", amount=100),
                                            line("BF-2", "bk2", "bfp2", amount=200),
                                            line("BF-3", "bk3", "bfp3", amount=300)])
    resp = post_batch(body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["result"] == "success" and data["status"] == "completed"
    assert data["replayed"] is False and data["resumed"] is False
    assert data["counts"] == {
        "accepted": 3, "replayed": 0,
        "rejected_invalid": 0, "rejected_fingerprint": 0, "rejected_duplicate": 0,
    }
    assert [item["line_no"] for item in data["lines"]] == [1, 2, 3]
    assert all(item["outcome"] == "accepted" and item["replayed"] is False for item in data["lines"])
    for amount, oid in ((100, "BF-1"), (200, "BF-2"), (300, "BF-3")):
        got = client.get(f"/orders/{oid}", headers={"X-Tenant": "t1"}).json()
        assert got["amount_cents"] == amount and got["outstanding_cents"] == amount


def test_partial_failure_counts_close_and_only_qualified_land() -> None:
    # 先存在一单与一个幂等键，供行级指纹冲突 / 订单重放 / 订单重复使用。
    orders.accept_order("t1", "pre-bk", "pre-bfp", "B-PRE", 150, "CNY")
    lines_ = [
        line("B-PA-1", "pa1", "pafp1"),                                   # 1 accepted
        {"tenant": "t1", "order_id": "B-PA-X", "amount_cents": 100,
         "currency": "CNY", "request_fingerprint": "x"},                  # 2 invalid: 缺幂等键
        {**line("B-PA-3", "pre-bk", "OTHER-FP"), "amount_cents": 999},    # 3 fingerprint 冲突
        line("B-PRE", "pa4", "pafp4"),                                    # 4 duplicate 订单标识
        line("B-PRE", "pre-bk", "pre-bfp"),                               # 5 replayed（既有键重放）
        line("B-PA-6", "pa6", "pafp6", amount=0),                         # 6 invalid 金额
        line("B-PA-7", "pa7", "pafp7", currency="XXX"),                   # 7 invalid 币种
        line("B-PA-8", "pa8", "pafp8", tenant="t2"),                      # 8 invalid 租户不一致
        line("B-PA-9", "", "pafp9"),                                      # 9 invalid 空幂等键
        line("B-PA-10", "pa10", ""),                                      # 10 invalid 空指纹
    ]
    resp = post_batch(batch_body("B-PARTIAL", "fp-partial", lines_))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["result"] == "partial" and data["status"] == "completed"
    counts = data["counts"]
    assert counts == {
        "accepted": 1, "replayed": 1,
        "rejected_invalid": 6, "rejected_fingerprint": 1, "rejected_duplicate": 1,
    }
    # 计数闭合：成功 + 重放 + 各拒绝 = 总行数，且与落库行结论数一致。
    assert sum(counts.values()) == 10 == len(data["lines"])
    conn = connect()
    try:
        landed_lines = conn.execute(
            "SELECT COUNT(*) AS c FROM batch_order_lines WHERE tenant='t1' AND batch_id='B-PARTIAL'"
        ).fetchone()["c"]
        landed_orders = conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE tenant='t1' AND order_id LIKE 'B-PA-%'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert landed_lines == 10
    # 仅 1 个新订单由本批落库（B-PA-1）；B-PRE 早已存在，其他拒绝行不落单。
    assert landed_orders == 1
    outcomes = outcome_map(data)
    assert outcomes == {
        1: "accepted", 2: "rejected_invalid", 3: "rejected_fingerprint", 4: "rejected_duplicate",
        5: "replayed", 6: "rejected_invalid", 7: "rejected_invalid", 8: "rejected_invalid",
        9: "rejected_invalid", 10: "rejected_invalid",
    }
    # 行级拒绝不改动被冲突行已有的订单与收款。
    assert orders.get("t1", "B-PRE")["amount_cents"] == 150


def test_all_lines_rejected_is_failed_and_persists_no_orders() -> None:
    lines_ = [
        {"tenant": "t1", "order_id": "B-AF-1", "amount_cents": 100, "currency": "CNY"},  # 缺键+缺指纹
        {**line("B-AF-2", "bk-af2", "afp2"), "amount_cents": -5},                       # 金额非法
        "not-an-object",                                                                  # 非对象行
    ]
    resp = post_batch(batch_body("B-ALLFAIL", "fp-allfail", lines_))
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] == "failed" and data["status"] == "completed"
    assert data["counts"]["accepted"] == 0 and data["counts"]["replayed"] == 0
    assert data["counts"]["rejected_invalid"] == 3
    assert orders.get("t1", "B-AF-1") is None and orders.get("t1", "B-AF-2") is None


# ---------- 批次重放 ----------

def test_batch_replay_returns_first_manifest_without_double_accept() -> None:
    lines_ = [line("B-RP-1", "brk1", "brfp1", amount=110), line("B-RP-2", "brk2", "brfp2", amount=220)]
    body = batch_body("B-REPLAY", "fp-replay", lines_)
    first = post_batch(body)
    assert first.status_code == 200 and first.headers.get("X-Idempotency-Replay") is None
    first_json = first.json()

    # 批次外给首单登记收款，改变当前订单状态：重放仍返回首次结果。
    client.post("/orders/B-RP-1/payments", json={"amount_cents": 110}, headers={"X-Tenant": "t1"})

    second = post_batch(body)
    assert second.status_code == 200 and second.headers["X-Idempotency-Replay"] == "true"
    second_json = second.json()
    assert second_json["replayed"] is True and second_json["resumed"] is False
    assert all(item["replayed"] is True for item in second_json["lines"])
    assert second_json["counts"] == first_json["counts"]
    assert [item["outcome"] for item in second_json["lines"]] == [item["outcome"] for item in first_json["lines"]]
    # 重放行的结果为首次受理快照：paid_cents 为 0，而非当前已结清状态。
    assert second_json["lines"][0]["result"]["paid_cents"] == 0
    # 没有重复落订单 / 幂等记录。
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE tenant='t1' AND order_id IN ('B-RP-1','B-RP-2')"
        ).fetchone()["c"] == 2
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM accepted_requests WHERE tenant='t1' AND idempotency_key IN ('brk1','brk2')"
        ).fetchone()["c"] == 2
    finally:
        conn.close()


# ---------- 批次指纹冲突 ----------

def test_same_batch_id_different_fingerprint_is_conflict_and_does_not_overwrite() -> None:
    lines_a = [line("B-CF-1", "bcfk1", "bcffp1", amount=100)]
    first = post_batch(batch_body("B-CONFLICT", "fp-A", lines_a))
    assert first.status_code == 200 and first.json()["result"] == "success"

    # 同批次标识、不同指纹、且内容试图篡改金额/订单：必须 422 拒绝。
    lines_b = [line("B-CF-1", "bcfk1", "bcffp1", amount=9999), line("B-CF-2", "bcfk2", "bcffp2")]
    resp = post_batch(batch_body("B-CONFLICT", "fp-B", lines_b))
    assert resp.status_code == 422 and "batch" in resp.json()["detail"]
    # 首次导入的订单、行结果均不得被覆盖，新订单不得落库。
    assert orders.get("t1", "B-CF-1")["amount_cents"] == 100
    assert orders.get("t1", "B-CF-2") is None
    conn = connect()
    try:
        stored_fp = conn.execute(
            "SELECT request_fingerprint, total_lines FROM order_batches WHERE tenant='t1' AND batch_id='B-CONFLICT'"
        ).fetchone()
        line_count = conn.execute(
            "SELECT COUNT(*) AS c FROM batch_order_lines WHERE tenant='t1' AND batch_id='B-CONFLICT'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert stored_fp["request_fingerprint"] == "fp-A" and stored_fp["total_lines"] == 1 and line_count == 1
    # 首次批次仍可同指纹重放。
    again = post_batch(batch_body("B-CONFLICT", "fp-A", lines_a))
    assert again.status_code == 200 and again.headers["X-Idempotency-Replay"] == "true"


def test_batch_conflict_is_distinct_from_line_conflict_and_duplicate() -> None:
    orders.accept_order("t1", "lk-seed", "lfp-seed", "B-LD", 100, "CNY")
    lines_ = [
        line("B-LC-1", "lk-seed", "DIFFERENT"),   # 行级指纹冲突 → 行 outcome（HTTP 仍 200）
        line("B-LD", "lk-dup", "lfp-dup"),        # 行级订单重复 → 行 outcome（HTTP 仍 200）
    ]
    resp = post_batch(batch_body("B-LINECF", "fp-line", lines_))
    assert resp.status_code == 200
    outcomes = outcome_map(resp.json())
    assert outcomes[1] == "rejected_fingerprint" and outcomes[2] == "rejected_duplicate"
    # 批次标识指纹冲突则是整个请求 422，与上面行级结论不同层级、可区分。
    assert post_batch(batch_body("B-LINECF", "fp-other", lines_)).status_code == 422


# ---------- 批次级参数不合法 ----------

def test_missing_batch_id_or_fingerprint_is_400_and_persists_nothing() -> None:
    good_line = line("B-NV-1", "bnk1", "bnfp1")
    for stripped in ("batch_id", "request_fingerprint"):
        body = {"batch_id": "B-NOVAL", "request_fingerprint": "fp", "lines": [good_line]}
        del body[stripped]
        resp = post_batch(body)
        assert resp.status_code == 400, (stripped, resp.status_code)
    for body in (
        {"batch_id": "", "request_fingerprint": "fp", "lines": [good_line]},
        {"batch_id": "B-EMPTYFP", "request_fingerprint": "", "lines": [good_line]},
        {"batch_id": "B-EMPTYLINES", "request_fingerprint": "fp", "lines": []},
    ):
        assert post_batch(body).status_code == 400, body
    conn = connect()
    try:
        rejected_batch_ids = ("B-NOVAL", "B-EMPTYFP", "B-EMPTYLINES")
        placeholders = ",".join("?" for _ in rejected_batch_ids)
        landed_batches = conn.execute(
            f"SELECT COUNT(*) AS c FROM order_batches WHERE batch_id IN ({placeholders})",
            rejected_batch_ids,
        ).fetchone()["c"]
        landed_orders = conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE order_id LIKE 'B-NV-%'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert landed_batches == 0 and landed_orders == 0


def test_batch_requires_tenant_in_first_line() -> None:
    body = {"batch_id": "B-NOTENANT", "request_fingerprint": "fp",
            "lines": [{"order_id": "X", "amount_cents": 100, "currency": "CNY",
                       "idempotency_key": "k", "request_fingerprint": "f"}]}
    resp = post_batch(body)
    assert resp.status_code == 400 and "tenant" in resp.json()["detail"]


# ---------- 中断续跑 ----------

def test_interrupted_batch_resumes_only_missing_lines_and_matches_full_import() -> None:
    total = 6
    lines_ = [line(f"B-RS-{i}", f"brsk{i}", f"brsfp{i}", amount=100 + i) for i in range(1, total + 1)]

    # 在第 3 行提交后硬退出：前 3 行已受理，批次停留 running，无半笔数据。
    code = (
        "from app.store.db import migrate; from app.store import batches; migrate(); "
        "lines_=["
        + ",".join(
            f"{{'tenant':'t1','order_id':'B-RS-{i}','amount_cents':{100+i},'currency':'CNY',"
            f"'idempotency_key':'brsk{i}','request_fingerprint':'brsfp{i}'}}"
            for i in range(1, total + 1)
        )
        + "]; batches.accept_batch('t1','B-RESUME','fp-rs',lines_)"
    )
    env = {**os.environ, "APP_CRASH_BATCH_AFTER_LINE": "3", "APP_DB": os.environ["APP_DB"]}
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=Path(__file__).resolve().parents[1],
        capture_output=True, check=False,
    )
    assert result.returncode == 2, result.stderr
    conn = connect()
    try:
        batch_row = conn.execute(
            "SELECT status, total_lines FROM order_batches WHERE tenant='t1' AND batch_id='B-RESUME'"
        ).fetchone()
        landed_lines = conn.execute(
            "SELECT COUNT(*) AS c FROM batch_order_lines WHERE tenant='t1' AND batch_id='B-RESUME'"
        ).fetchone()["c"]
        landed_orders = conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE tenant='t1' AND order_id LIKE 'B-RS-%'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert batch_row["status"] == "running" and batch_row["total_lines"] == total
    assert landed_lines == 3 and landed_orders == 3

    # 同（租户, 批次标识）同指纹重新提交：只补做 4-6 行。
    resp = post_batch(batch_body("B-RESUME", "fp-rs", lines_))
    assert resp.status_code == 200
    data = resp.json()
    assert data["resumed"] is True and data["replayed"] is False and data["status"] == "completed"
    assert data["counts"]["accepted"] == total
    assert [(item["line_no"], item["replayed"]) for item in data["lines"]] == [
        (1, True), (2, True), (3, True), (4, False), (5, False), (6, False)
    ]
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE tenant='t1' AND order_id LIKE 'B-RS-%'"
        ).fetchone()["c"] == total
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM accepted_requests WHERE tenant='t1' AND idempotency_key LIKE 'brsk%'"
        ).fetchone()["c"] == total
    finally:
        conn.close()

    # 再提交即为纯批次重放，结论与续跑完成时一致。
    replay = post_batch(batch_body("B-RESUME", "fp-rs", lines_))
    assert replay.status_code == 200 and replay.headers["X-Idempotency-Replay"] == "true"
    assert replay.json()["counts"] == data["counts"]


def test_crash_after_claim_before_any_line_then_resume_completes() -> None:
    lines_ = [line("B-CR-1", "bcrk1", "bcrfp1"), line("B-CR-2", "bcrk2", "bcrfp2")]
    code = (
        "from app.store.db import migrate; from app.store import batches; migrate(); "
        "batches.accept_batch('t1','B-CRASH','fp-cr',"
        "[{'tenant':'t1','order_id':'B-CR-1','amount_cents':100,'currency':'CNY',"
        "'idempotency_key':'bcrk1','request_fingerprint':'bcrfp1'},"
        "{'tenant':'t1','order_id':'B-CR-2','amount_cents':100,'currency':'CNY',"
        "'idempotency_key':'bcrk2','request_fingerprint':'bcrfp2'}])"
    )
    # 在批次登记提交后、任何一行处理前硬退出：批次 running，无行结论、无订单。
    env = {**os.environ, "APP_CRASH_BATCH_AFTER_LINE": "0", "APP_DB": os.environ["APP_DB"]}
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=Path(__file__).resolve().parents[1],
        capture_output=True, check=False,
    )
    assert result.returncode == 2, result.stderr
    assert orders.get("t1", "B-CR-1") is None and orders.get("t1", "B-CR-2") is None
    conn = connect()
    try:
        assert conn.execute(
            "SELECT status FROM order_batches WHERE tenant='t1' AND batch_id='B-CRASH'"
        ).fetchone()["status"] == "running"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM batch_order_lines WHERE tenant='t1' AND batch_id='B-CRASH'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()

    # 同（租户, 批次标识）同指纹续跑：补齐两行，最终结论与一次性完整导入一致。
    resp = post_batch(batch_body("B-CRASH", "fp-cr", lines_))
    assert resp.status_code == 200
    data = resp.json()
    assert data["resumed"] is True and data["status"] == "completed"
    assert data["counts"]["accepted"] == 2
    assert all(item["replayed"] is False for item in data["lines"])
    assert orders.get("t1", "B-CR-1") is not None and orders.get("t1", "B-CR-2") is not None
    # 再提交即为确定性批次重放。
    replay = post_batch(batch_body("B-CRASH", "fp-cr", lines_))
    assert replay.status_code == 200 and replay.headers["X-Idempotency-Replay"] == "true"


# ---------- 并发 ----------

_server: tuple | None = None


def _start_server() -> str:
    global _server
    if _server is not None:
        return _server[0]
    config = Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    _server = (url, server, thread)
    return url


def test_concurrent_same_fingerprint_only_one_batch_imports() -> None:
    url = _start_server()
    n = 5
    body = batch_body("B-CC1", "fp-cc1", [line(f"B-CC1-{i}", f"bcc1k{i}", f"bcc1fp{i}") for i in range(1, n + 1)])
    with httpx.Client(base_url=url, timeout=30) as http, ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: http.post("/orders/batch", json=body), range(8)))
    assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]
    import_count = sum(1 for r in responses if r.json()["replayed"] is False and r.json()["resumed"] is False)
    replay_count = sum(1 for r in responses if r.json()["replayed"] is True)
    assert import_count == 1 and replay_count == 7, (import_count, replay_count)
    # 首次导入与各重放的行结论、订单快照、计数完全一致（仅重放标记不同）。
    first_manifest = next(r.json() for r in responses if r.json()["replayed"] is False)
    for resp in responses:
        data = resp.json()
        assert data["counts"] == first_manifest["counts"]
        assert [(i["line_no"], i["outcome"], i["result"]) for i in data["lines"]] == [
            (i["line_no"], i["outcome"], i["result"]) for i in first_manifest["lines"]
        ]
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE tenant='t1' AND order_id LIKE 'B-CC1-%'"
        ).fetchone()["c"] == n
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM order_batches WHERE tenant='t1' AND batch_id='B-CC1'"
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM batch_order_lines WHERE tenant='t1' AND batch_id='B-CC1'"
        ).fetchone()["c"] == n
    finally:
        conn.close()


def test_concurrent_different_fingerprints_exactly_one_wins() -> None:
    url = _start_server()
    n = 4

    def one(i: int) -> httpx.Response:
        body = batch_body(
            "B-CC2", f"fp-cc2-{i}",
            [line(f"B-CC2-{i}-{j}", f"bcc2k-{i}-{j}", f"bcc2fp-{i}-{j}") for j in range(n)],
        )
        with httpx.Client(base_url=url, timeout=30) as http:
            return http.post("/orders/batch", json=body)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = [f.result() for f in [pool.submit(one, i) for i in range(8)]]
    winners = [r for r in responses if r.status_code == 200 and r.json()["replayed"] is False]
    conflicts = [r for r in responses if r.status_code == 422]
    assert len(winners) == 1 and len(conflicts) == 7, [(r.status_code) for r in responses]
    # 只有赢家批次的订单落库，冲突方一个订单都不得写入。
    winner = winners[0].json()
    conn = connect()
    try:
        stored = conn.execute(
            "SELECT request_fingerprint FROM order_batches WHERE tenant='t1' AND batch_id='B-CC2'"
        ).fetchone()
        order_count = conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE tenant='t1' AND order_id LIKE 'B-CC2-%'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert stored["request_fingerprint"] == winner["request_fingerprint"]
    assert order_count == n


# ---------- 跨租户 ----------

def test_batch_id_scoped_per_tenant() -> None:
    body_a = batch_body("B-SAME", "fp-same", [line("B-SA-1", "bsak1", "bsafp1", tenant="ta")])
    body_b = batch_body("B-SAME", "fp-same", [line("B-SB-1", "bsbk1", "bsbfp1", tenant="tb")])
    ra = post_batch(body_a)
    rb = post_batch(body_b)
    assert ra.status_code == 200 and rb.status_code == 200
    assert ra.json()["counts"]["accepted"] == 1 and rb.json()["counts"]["accepted"] == 1
    assert orders.get("ta", "B-SA-1") is not None and orders.get("tb", "B-SB-1") is not None
    # 同批次标识在另一租户换指纹也互不构成冲突。
    other = post_batch(batch_body("B-SAME", "fp-different", [line("B-SB-2", "bsbk2", "bsbfp2", tenant="tb")]))
    assert other.status_code == 422
    assert post_batch(body_a).headers["X-Idempotency-Replay"] == "true"
