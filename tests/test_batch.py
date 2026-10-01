import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))

import httpx
from fastapi.testclient import TestClient
from uvicorn import Config, Server

from app.entry import app
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

TENANT = "tb"


def row(order_id: str, key: str, fp: str, *, tenant: str = TENANT, amount: int = 100) -> dict:
    return {
        "tenant": tenant,
        "order_id": order_id,
        "amount_cents": amount,
        "currency": "CNY",
        "idempotency_key": key,
        "request_fingerprint": fp,
    }


def batch_body(batch_id: str, fp: str, rows: list[dict], *, tenant: str = TENANT) -> dict:
    return {"tenant": tenant, "batch_id": batch_id, "request_fingerprint": fp, "items": rows}


def post_batch(body: dict):
    return client.post("/orders/batch", json=body)


def assert_counts_close(payload: dict) -> None:
    total = payload["total"]
    assert total == len(payload["rows"])
    assert (
        payload["accepted"]
        + payload["replayed"]
        + payload["rejected_invalid"]
        + payload["rejected_fingerprint_conflict"]
        + payload["rejected_order_duplicate"]
        == total
    )


def db_scalar(sql: str, params: tuple = ()):
    conn = connect()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


# ---------- 完整导入与部分失败 ----------


def test_full_import_all_rows_accepted() -> None:
    rows = [row("b1-o1", "b1-k1", "b1-f1", amount=500), row("b1-o2", "b1-k2", "b1-f2", amount=300)]
    resp = post_batch(batch_body("B-1", "sha256:b1", rows))
    assert resp.status_code == 200 and resp.headers.get("X-Idempotency-Replay") is None
    payload = resp.json()
    assert payload["status"] == "accepted" and payload["accepted"] == 2
    assert_counts_close(payload)
    assert [r["row"] for r in payload["rows"]] == [1, 2]
    assert all(r["outcome"] == "accepted" and r["reason"] is None for r in payload["rows"])
    # 计数与实际落库订单数一致，订单可按标识读取。
    assert db_scalar("SELECT COUNT(*) FROM orders WHERE tenant=? AND order_id IN ('b1-o1','b1-o2')", (TENANT,)) == 2
    got = client.get("/orders/b1-o1", headers={"X-Tenant": TENANT})
    assert got.status_code == 200 and got.json()["outstanding_cents"] == 500


def test_partial_failure_rejects_only_bad_rows() -> None:
    # 预备：单笔受理一单，供行级重放 / 指纹冲突 / 订单重复场景使用。
    client.post("/orders", json=row("b2-pre", "b2-pre-key", "b2-pre-fp", amount=800))
    rows = [
        row("b2-o1", "b2-k1", "b2-f1", amount=100),  # 合格
        {**row("b2-o2", "b2-k2", "b2-f2"), "idempotency_key": ""},  # 空幂等键 → 参数不合法
        {k: v for k, v in row("b2-o3", "b2-k3", "b2-f3").items() if k != "request_fingerprint"},  # 缺指纹
        row("b2-o4", "b2-pre-key", "b2-other-fp"),  # 同键不同指纹 → 指纹冲突
        row("b2-pre", "b2-k5", "b2-f5"),  # 订单标识重复（新幂等键）
        row("b2-o6", "b2-pre-key", "b2-pre-fp"),  # 同键同指纹 → 行级重放
        row("b2-o7", "b2-k7", "b2-f7", amount=200),  # 合格
    ]
    resp = post_batch(batch_body("B-2", "sha256:b2", rows))
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "partial"
    assert payload["accepted"] == 2 and payload["replayed"] == 1
    assert payload["rejected_invalid"] == 2
    assert payload["rejected_fingerprint_conflict"] == 1
    assert payload["rejected_order_duplicate"] == 1
    assert_counts_close(payload)
    outcomes = [r["outcome"] for r in payload["rows"]]
    assert outcomes == [
        "accepted",
        "rejected_invalid",
        "rejected_invalid",
        "rejected_fingerprint_conflict",
        "rejected_order_duplicate",
        "replayed",
        "accepted",
    ]
    # 行级拒绝只影响本行：合格行已落库，被拒行不落任何数据。
    assert client.get("/orders/b2-o1", headers={"X-Tenant": TENANT}).status_code == 200
    assert client.get("/orders/b2-o7", headers={"X-Tenant": TENANT}).json()["outstanding_cents"] == 200
    for rejected_id in ("b2-o2", "b2-o3", "b2-o4"):
        assert client.get(f"/orders/{rejected_id}", headers={"X-Tenant": TENANT}).status_code == 404
    # 行级重放返回首次受理的订单快照，且不改动已有订单与收款。
    replay_row = payload["rows"][5]
    assert replay_row["order"]["order_id"] == "b2-pre" and replay_row["order"]["amount_cents"] == 800
    assert db_scalar("SELECT COUNT(*) FROM orders WHERE tenant=? AND order_id='b2-pre'", (TENANT,)) == 1


def test_all_rows_rejected_is_failed_and_persists_no_order() -> None:
    rows = [
        {**row("b3-o1", "b3-k1", "b3-f1"), "idempotency_key": ""},
        {**row("b3-o2", "b3-k2", "b3-f2"), "amount_cents": -5},
    ]
    resp = post_batch(batch_body("B-3", "sha256:b3", rows))
    payload = resp.json()
    assert payload["status"] == "failed" and payload["accepted"] == 0 and payload["replayed"] == 0
    assert_counts_close(payload)
    assert db_scalar("SELECT COUNT(*) FROM orders WHERE tenant=? AND order_id IN ('b3-o1','b3-o2')", (TENANT,)) == 0
    assert db_scalar("SELECT COUNT(*) FROM accepted_requests WHERE tenant=? AND idempotency_key LIKE 'b3-%'", (TENANT,)) == 0


# ---------- 批次级重放与指纹冲突 ----------


def test_batch_replay_returns_first_result_without_side_effects() -> None:
    rows = [row("b4-o1", "b4-k1", "b4-f1", amount=400), row("b4-o2", "b4-k2", "b4-f2", amount=600)]
    first = post_batch(batch_body("B-4", "sha256:b4", rows))
    assert first.status_code == 200 and first.json()["status"] == "accepted"

    # 改变订单当前状态，证明重放返回的是首次结果清单而非现态。
    assert client.post(
        "/orders/b4-o1/payments", json={"amount_cents": 150}, headers={"X-Tenant": TENANT}
    ).status_code == 200

    replay = post_batch(batch_body("B-4", "sha256:b4", rows))
    assert replay.status_code == 200 and replay.headers["X-Idempotency-Replay"] == "true"
    assert replay.json() == first.json()
    # 不重复受理任何一行：订单数不变，收款未被回滚或重复。
    assert db_scalar("SELECT COUNT(*) FROM orders WHERE tenant=? AND order_id IN ('b4-o1','b4-o2')", (TENANT,)) == 2
    assert client.get("/orders/b4-o1", headers={"X-Tenant": TENANT}).json()["paid_cents"] == 150


def test_batch_fingerprint_conflict_is_422_and_overwrites_nothing() -> None:
    rows = [row("b5-o1", "b5-k1", "b5-f1", amount=100)]
    assert post_batch(batch_body("B-5", "sha256:b5-a", rows)).status_code == 200

    other_rows = [row("b5-other", "b5-kx", "b5-fx", amount=9999)]
    resp = post_batch(batch_body("B-5", "sha256:b5-b", other_rows))
    assert resp.status_code == 422 and "fingerprint" in resp.json()["detail"]
    # 首次导入的订单与行结果不被覆盖，第二批内容不落任何数据。
    assert client.get("/orders/b5-o1", headers={"X-Tenant": TENANT}).json()["amount_cents"] == 100
    assert client.get("/orders/b5-other", headers={"X-Tenant": TENANT}).status_code == 404
    assert db_scalar(
        "SELECT COUNT(*) FROM import_batch_rows WHERE tenant=? AND batch_id='B-5'", (TENANT,)
    ) == 1


def test_batch_missing_or_empty_fields_is_400_and_persists_nothing() -> None:
    rows = [row("b6-o1", "b6-k1", "b6-f1")]
    base = batch_body("B-6", "sha256:b6", rows)
    for stripped in ("batch_id", "request_fingerprint"):
        bad = {k: v for k, v in base.items() if k != stripped}
        resp = post_batch(bad)
        assert resp.status_code == 400, (stripped, resp.status_code)
    for field in ("batch_id", "request_fingerprint"):
        bad = {**base, field: ""}
        assert post_batch(bad).status_code == 400
    # 不落任何数据：批次、行结果、订单、幂等记录均不存在。
    assert db_scalar("SELECT COUNT(*) FROM import_batches WHERE tenant=? AND batch_id='B-6'", (TENANT,)) == 0
    assert db_scalar("SELECT COUNT(*) FROM orders WHERE tenant=? AND order_id='b6-o1'", (TENANT,)) == 0
    assert db_scalar("SELECT COUNT(*) FROM accepted_requests WHERE tenant=? AND idempotency_key='b6-k1'", (TENANT,)) == 0


def test_batch_id_is_scoped_per_tenant() -> None:
    assert post_batch(batch_body("B-7", "sha256:b7", [row("b7-oa", "b7-ka", "b7-fa")], tenant="ta")).status_code == 200
    # 不同租户可使用相同批次标识，互不影响。
    resp = post_batch(batch_body("B-7", "sha256:b7", [row("b7-ob", "b7-kb", "b7-fb")], tenant="tbb"))
    assert resp.status_code == 200 and resp.headers.get("X-Idempotency-Replay") is None
    assert resp.json()["accepted"] == 1


# ---------- 中断续跑与提交前崩溃恢复 ----------


def _run_batch_subprocess(env_extra: dict, code: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )


def _batch_code(tenant: str, batch_id: str, fp: str, rows: list[dict]) -> str:
    return (
        "from app.store.db import migrate; "
        "from app.store import batches; "
        "migrate(); "
        f"batches.accept_batch('{tenant}', '{batch_id}', '{fp}', {json.dumps(rows)})"
    )


def test_interrupted_batch_resumes_only_remaining_rows() -> None:
    rows = [row(f"b8-o{i}", f"b8-k{i}", f"b8-f{i}", amount=100 + i) for i in range(1, 5)]
    # 提交完第 2 行后进程崩溃：批次中断。
    result = _run_batch_subprocess(
        {"APP_CRASH_BATCH_AFTER_ROW": "2"}, _batch_code(TENANT, "B-8", "sha256:b8", rows)
    )
    assert result.returncode == 2
    # 已提交的行保持已受理，未处理的行不留半笔数据。
    assert db_scalar("SELECT COUNT(*) FROM orders WHERE tenant=? AND order_id LIKE 'b8-o%'", (TENANT,)) == 2
    assert db_scalar(
        "SELECT status FROM import_batches WHERE tenant=? AND batch_id='B-8'", (TENANT,)
    ) == "processing"
    assert db_scalar(
        "SELECT COUNT(*) FROM import_batch_rows WHERE tenant=? AND batch_id='B-8'", (TENANT,)
    ) == 2

    # 用同一（租户, 批次标识）与同一指纹重新提交：只补做尚未受理的行。
    resp = post_batch(batch_body("B-8", "sha256:b8", rows))
    assert resp.status_code == 200 and resp.headers.get("X-Idempotency-Replay") is None
    payload = resp.json()
    # 最终结论与一次性完整导入一致。
    assert payload["status"] == "accepted" and payload["accepted"] == 4
    assert_counts_close(payload)
    assert all(r["outcome"] == "accepted" for r in payload["rows"])
    assert db_scalar("SELECT COUNT(*) FROM orders WHERE tenant=? AND order_id LIKE 'b8-o%'", (TENANT,)) == 4
    # 行级幂等记录每键仅一条。
    assert db_scalar(
        "SELECT COUNT(*) FROM accepted_requests WHERE tenant=? AND idempotency_key LIKE 'b8-k%'", (TENANT,)
    ) == 4
    # 完成后再次提交即为整批重放。
    replay = post_batch(batch_body("B-8", "sha256:b8", rows))
    assert replay.headers["X-Idempotency-Replay"] == "true" and replay.json() == payload


def test_crash_before_row_commit_leaves_no_partial_data() -> None:
    rows = [row(f"b9-o{i}", f"b9-k{i}", f"b9-f{i}") for i in range(1, 4)]
    # 第 2 行事务提交前崩溃：第 2 行不留半笔数据。
    result = _run_batch_subprocess(
        {"APP_CRASH_BATCH_BEFORE_ROW_COMMIT": "2"}, _batch_code(TENANT, "B-9", "sha256:b9", rows)
    )
    assert result.returncode == 2
    assert db_scalar("SELECT COUNT(*) FROM orders WHERE tenant=? AND order_id LIKE 'b9-o%'", (TENANT,)) == 1
    assert db_scalar(
        "SELECT COUNT(*) FROM accepted_requests WHERE tenant=? AND idempotency_key='b9-k2'", (TENANT,)
    ) == 0
    assert db_scalar(
        "SELECT COUNT(*) FROM import_batch_rows WHERE tenant=? AND batch_id='B-9'", (TENANT,)
    ) == 1

    # 重启续跑：第 2 行按首次受理补做，最终结论与一次性完整导入一致。
    resp = post_batch(batch_body("B-9", "sha256:b9", rows))
    payload = resp.json()
    assert payload["status"] == "accepted" and payload["accepted"] == 3
    assert_counts_close(payload)
    assert db_scalar("SELECT COUNT(*) FROM orders WHERE tenant=? AND order_id LIKE 'b9-o%'", (TENANT,)) == 3


# ---------- 并发批次 ----------


def test_concurrent_same_batch_only_one_imports() -> None:
    url = _start_server()
    rows = [row(f"b10-o{i}", f"b10-k{i}", f"b10-f{i}") for i in range(1, 4)]
    body = batch_body("B-10", "sha256:b10", rows)
    with httpx.Client(base_url=url, timeout=30) as http, ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: http.post("/orders/batch", json=body), range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert sum(1 for r in responses if r.headers.get("X-Idempotency-Replay") == "true") == 7
    # 全部响应体一致：一批真正导入，其余按重放返回首次结果清单。
    assert len({r.text for r in responses}) == 1
    assert db_scalar("SELECT COUNT(*) FROM orders WHERE tenant=? AND order_id LIKE 'b10-o%'", (TENANT,)) == 3
    assert db_scalar(
        "SELECT COUNT(*) FROM accepted_requests WHERE tenant=? AND idempotency_key LIKE 'b10-k%'", (TENANT,)
    ) == 3


def test_concurrent_same_batch_id_different_fingerprints_exactly_one_wins() -> None:
    url = _start_server()

    def one(i: int) -> httpx.Response:
        with httpx.Client(base_url=url, timeout=30) as http:
            body = batch_body("B-11", f"sha256:b11-{i}", [row(f"b11-o{i}", f"b11-k{i}", f"b11-f{i}")])
            return http.post("/orders/batch", json=body)

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = [f.result() for f in [pool.submit(one, i) for i in range(6)]]
    assert sum(1 for r in responses if r.status_code == 200) == 1
    assert sum(1 for r in responses if r.status_code == 422) == 5
    # 只有胜出一批真正导入。
    assert db_scalar("SELECT COUNT(*) FROM orders WHERE tenant=? AND order_id LIKE 'b11-o%'", (TENANT,)) == 1
    assert db_scalar("SELECT COUNT(*) FROM import_batches WHERE tenant=? AND batch_id='B-11'", (TENANT,)) == 1


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
