"""批量受理（批次导入）的真实 HTTP 调用演示。

自包含：使用临时数据库，启动真实 uvicorn 进程，通过 HTTP 依次演示
完整导入 / 部分失败 / 批次重放 / 批次指纹冲突 / 缺参数 / 中断续跑 / 提交前崩溃恢复 / 并发批次。

    . .venv/bin/activate
    python scripts/demo_batch.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 本机回环演示，绕过环境中可能存在的 HTTP 代理（崩溃时代理会掩盖真实的断连）。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(db_file: str, *, crash_env: dict | None = None) -> tuple[subprocess.Popen, str]:
    port = free_port()
    env = {**os.environ, "APP_DB": db_file, "PYTHONPATH": str(ROOT), **(crash_env or {})}
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.entry", "--port", str(port)],
        env=env,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with OPENER.open(f"{url}/health", timeout=1) as resp:
                if resp.status == 200:
                    return proc, url
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


def post(url: str, path: str, body: dict) -> tuple[int, object, dict]:
    req = urllib.request.Request(
        f"{url}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with OPENER.open(req, timeout=10) as resp:
            return resp.status, _parse(resp.read()), _title_headers(resp.headers)
    except urllib.error.HTTPError as error:
        return error.code, _parse(error.read()), _title_headers(error.headers)


def _title_headers(headers: object) -> dict:
    return {key.title(): value for key, value in dict(headers).items()}


def _parse(raw: bytes) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode(errors="replace")


def row(order_id: str, key: str, fp: str, *, amount: int = 100, tenant: str = "demo") -> dict:
    return {
        "tenant": tenant,
        "order_id": order_id,
        "amount_cents": amount,
        "currency": "CNY",
        "idempotency_key": key,
        "request_fingerprint": fp,
    }


def batch(batch_id: str, fp: str, rows: list[dict], *, tenant: str = "demo") -> dict:
    return {"tenant": tenant, "batch_id": batch_id, "request_fingerprint": fp, "items": rows}


def show_batch(status: int, payload: object, headers: dict) -> None:
    replay = headers.get("X-Idempotency-Replay")
    suffix = "  X-Idempotency-Replay=true" if replay else ""
    if isinstance(payload, dict) and "rows" in payload:
        counts = {k: payload[k] for k in ("status", "total", "accepted", "replayed",
                                          "rejected_invalid", "rejected_fingerprint_conflict",
                                          "rejected_order_duplicate")}
        print(f"  -> HTTP {status}{suffix}: {counts}")
        for r in payload["rows"]:
            reason = f"  ({r['reason']})" if r["reason"] else ""
            print(f"     行 {r['row']}: {r['outcome']}{reason}")
    else:
        print(f"  -> HTTP {status}{suffix}: {payload}")


def main() -> None:
    db_file = os.path.join(tempfile.mkdtemp(prefix="batch-demo-"), "app.sqlite")
    proc, url = start_server(db_file)
    try:
        print("1) 完整导入：3 行全部合格，一次受理进系统")
        rows = [row("ORD-A1", "key-a1", "sha256:a1", amount=500),
                row("ORD-A2", "key-a2", "sha256:a2", amount=300),
                row("ORD-A3", "key-a3", "sha256:a3", amount=200)]
        status, payload, headers = post(url, "/orders/batch", batch("BATCH-1", "sha256:batch1", rows))
        show_batch(status, payload, headers)
        assert status == 200 and payload["status"] == "accepted" and payload["accepted"] == 3

        print("\n2) 部分失败：行级拒绝只影响本行，合格行照常落库")
        rows = [
            row("ORD-B1", "key-b1", "sha256:b1", amount=100),           # 合格
            {**row("ORD-B2", "key-b2", "sha256:b2"), "idempotency_key": ""},  # 缺幂等键
            row("ORD-A1", "key-b3", "sha256:b3"),                        # 订单标识重复
            row("ORD-B4", "key-a2", "sha256:other"),                     # 同键不同指纹
            row("ORD-B5", "key-a3", "sha256:a3"),                        # 同键同指纹 → 行级重放
        ]
        status, payload, headers = post(url, "/orders/batch", batch("BATCH-2", "sha256:batch2", rows))
        show_batch(status, payload, headers)
        assert payload["status"] == "partial" and payload["accepted"] == 1 and payload["replayed"] == 1
        assert payload["rejected_invalid"] == 1 and payload["rejected_order_duplicate"] == 1
        assert payload["rejected_fingerprint_conflict"] == 1

        print("\n3) 批次重放：同批次标识同指纹，不重复受理任何一行，返回首次结果清单")
        status, payload2, headers = post(url, "/orders/batch", batch("BATCH-2", "sha256:batch2", rows))
        show_batch(status, payload2, headers)
        assert status == 200 and headers["X-Idempotency-Replay"] == "true" and payload2 == payload

        print("\n4) 批次指纹冲突：同批次标识不同指纹 → 422，不覆盖首次导入的任何数据")
        stolen = batch("BATCH-2", "sha256:different", [row("ORD-NEVER", "key-x", "sha256:x")])
        status, payload, headers = post(url, "/orders/batch", stolen)
        show_batch(status, payload, headers)
        assert status == 422

        print("\n5) 缺批次标识或指纹：参数不合法 400，不落任何数据")
        bad = {k: v for k, v in batch("BATCH-3", "sha256:batch3", [row("ORD-C1", "key-c1", "sha256:c1")]).items()
               if k != "request_fingerprint"}
        status, payload, headers = post(url, "/orders/batch", bad)
        print(f"  -> HTTP {status}: {payload['detail']['error']}")
        assert status == 400

        print("\n6) 中断续跑：批次处理到一半进程崩溃，重提交只补做未受理的行")
        proc.kill()
        proc.wait()
        crash_proc, crash_url = start_server(db_file, crash_env={"APP_CRASH_BATCH_AFTER_ROW": "2"})
        rows = [row(f"ORD-D{i}", f"key-d{i}", f"sha256:d{i}", amount=100 * i) for i in range(1, 5)]
        body = batch("BATCH-4", "sha256:batch4", rows)
        try:
            post(crash_url, "/orders/batch", body)
            raise AssertionError("崩溃进程不应给出响应")
        except OSError as exc:
            print(f"  批次处理中进程崩溃（已提交 2 行），连接中断: {type(exc).__name__}")
        crash_proc.wait()
        assert crash_proc.returncode == 2

        proc, url = start_server(db_file)
        print("  重启后用同一（租户, 批次标识）与同一指纹重新提交：")
        status, payload, headers = post(url, "/orders/batch", body)
        show_batch(status, payload, headers)
        assert status == 200 and payload["status"] == "accepted" and payload["accepted"] == 4
        print("  最终结论与一次性完整导入一致；再次提交即为整批重放：")
        status, payload2, headers = post(url, "/orders/batch", body)
        assert headers["X-Idempotency-Replay"] == "true" and payload2 == payload
        print(f"  -> HTTP {status}  X-Idempotency-Replay=true（结果清单与首次完全一致）")

        print("\n7) 行事务提交前崩溃：该行不留半笔数据，续跑按首次受理补做")
        proc.kill()
        proc.wait()
        crash_proc, crash_url = start_server(db_file, crash_env={"APP_CRASH_BATCH_BEFORE_ROW_COMMIT": "2"})
        rows = [row(f"ORD-E{i}", f"key-e{i}", f"sha256:e{i}") for i in range(1, 4)]
        body = batch("BATCH-5", "sha256:batch5", rows)
        try:
            post(crash_url, "/orders/batch", body)
            raise AssertionError("崩溃进程不应给出响应")
        except OSError as exc:
            print(f"  第 2 行提交前进程崩溃，连接中断: {type(exc).__name__}")
        crash_proc.wait()
        proc, url = start_server(db_file)
        status, payload, headers = post(url, "/orders/batch", body)
        show_batch(status, payload, headers)
        assert payload["status"] == "accepted" and payload["accepted"] == 3

        print("\n8) 并发批次：8 个携带相同（批次标识, 指纹）的请求同时到达")
        rows = [row(f"ORD-F{i}", f"key-f{i}", f"sha256:f{i}") for i in range(1, 4)]
        body = batch("BATCH-6", "sha256:batch6", rows)

        def one_call() -> tuple[int, str | None, object]:
            st, pl, hs = post(url, "/orders/batch", body)
            return st, hs.get("X-Idempotency-Replay"), pl

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: one_call(), range(8)))
        firsts = [r for r in results if r[1] is None]
        replays = [r for r in results if r[1] == "true"]
        print(f"  8 个并发响应：真正导入 {len(firsts)} 个，整批重放 {len(replays)} 个")
        print(f"  8 个响应体完全相同: {len({json.dumps(r[2], sort_keys=True) for r in results}) == 1}")
        assert len(firsts) == 1 and len(replays) == 7
    finally:
        proc.kill()
        proc.wait()

    print("\n全部场景符合预期。")


if __name__ == "__main__":
    main()
