"""批量订单受理的真实 HTTP 调用演示。

自包含：使用临时数据库，启动真实 uvicorn 进程，通过 HTTP 依次演示
完整导入 / 部分失败（行级各拒绝）/ 批次重放 / 批次指纹冲突 / 缺批次参数 /
中断续跑 / 提交前崩溃恢复 / 同指纹并发唯一导入。

    . .venv/bin/activate
    python scripts/demo_batch_import.py

中断场景由环境变量 APP_CRASH_BATCH_AFTER_LINE=<行号|0> 触发：
  =0 表示批次登记提交后、任何一行处理前崩溃；=N 表示第 N 行提交后崩溃。
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
TENANT = "batchdemo"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(db_file: str, *, crash_after_line: str | None = None) -> tuple[subprocess.Popen, str]:
    port = free_port()
    env = {**os.environ, "APP_DB": db_file, "PYTHONPATH": str(ROOT)}
    if crash_after_line is not None:
        env["APP_CRASH_BATCH_AFTER_LINE"] = crash_after_line
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.entry", "--port", str(port)],
        env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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


def http(method: str, url: str, body: dict | None = None) -> tuple[int, object, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "X-Tenant": TENANT},
        method=method,
    )
    try:
        with OPENER.open(req, timeout=30) as resp:
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


def order_line(oid: str, key: str, fp: str, *, amount: int = 100, tenant: str = TENANT, **extra) -> dict:
    return {
        "tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY",
        "idempotency_key": key, "request_fingerprint": fp, **extra,
    }


def batch(batch_id: str, fp: str, lines_: list) -> dict:
    return {"batch_id": batch_id, "request_fingerprint": fp, "lines": lines_}


def show_manifest(title: str, status: int, payload: object, headers: dict) -> None:
    replay = headers.get("X-Idempotency-Replay")
    suffix = f"  X-Idempotency-Replay={replay}" if replay else ""
    print(f"  [{title}] HTTP {status}{suffix}")
    if isinstance(payload, dict) and "lines" in payload:
        print(f"    result={payload['result']} status={payload['status']} "
              f"replayed={payload['replayed']} resumed={payload['resumed']} counts={payload['counts']}")
        for item in payload["lines"]:
            mark = "重放" if item["replayed"] else "首受"
            result = item["result"]
            detail = result.get("detail", "") if isinstance(result, dict) else ""
            print(f"      行{item['line_no']}: {item['outcome']:<20} [{mark}] {detail}")
    else:
        print(f"    payload={payload}")


def main() -> None:
    db_file = os.path.join(tempfile.mkdtemp(prefix="batch-demo-"), "app.sqlite")
    proc, url = start_server(db_file)
    try:
        print("1) 完整导入：3 行全部合格")
        st, pl, hs = http("POST", f"{url}/orders/batch",
                          batch("B-100", "sha256:b100",
                                [order_line("ORD-100-1", "k-100-1", "fp-100-1", amount=100),
                                 order_line("ORD-100-2", "k-100-2", "fp-100-2", amount=200),
                                 order_line("ORD-100-3", "k-100-3", "fp-100-3", amount=300)]))
        show_manifest("first", st, pl, hs)
        assert st == 200 and pl["result"] == "success" and pl["counts"]["accepted"] == 3

        print("\n2) 行级各拒绝：先种一单，再用一批覆盖缺键/指纹冲突/订单重复/重放/非法金额")
        http("POST", f"{url}/orders", order_line("ORD-SEED", "k-seed", "fp-seed", amount=500))
        mixed = [
            order_line("ORD-200-1", "k-200-1", "fp-200-1", amount=100),                       # accepted
            {"tenant": TENANT, "order_id": "ORD-200-2", "amount_cents": 100,
             "currency": "CNY", "request_fingerprint": "x"},                                  # invalid 缺幂等键
            {**order_line("ORD-200-3", "k-seed", "fp-TAMPERED"), "amount_cents": 9999},       # fingerprint 冲突
            order_line("ORD-SEED", "k-200-4", "fp-200-4"),                                    # duplicate 订单标识
            order_line("ORD-SEED", "k-seed", "fp-seed"),                                      # replayed
            order_line("ORD-200-6", "k-200-6", "fp-200-6", amount=0),                         # invalid 金额
        ]
        st, pl, hs = http("POST", f"{url}/orders/batch", batch("B-200", "sha256:b200", mixed))
        show_manifest("partial", st, pl, hs)
        assert st == 200 and pl["result"] == "partial"
        assert pl["counts"] == {"accepted": 1, "replayed": 1, "rejected_invalid": 2,
                                "rejected_fingerprint": 1, "rejected_duplicate": 1}
        assert sum(pl["counts"].values()) == 6
        # 被指纹冲突的种子订单金额/收款不被改动。
        _, seeded, _ = http("GET", f"{url}/orders/ORD-SEED")
        assert seeded["amount_cents"] == 500

        print("\n3) 批次重放：同批次标识同指纹，返回首次结果清单，不重复受理任何一行")
        st, pl, hs = http("POST", f"{url}/orders/batch", batch("B-200", "sha256:b200", mixed))
        show_manifest("replay", st, pl, hs)
        assert st == 200 and hs["X-Idempotency-Replay"] == "true" and pl["replayed"] is True
        assert all(item["replayed"] for item in pl["lines"])

        print("\n4) 批次指纹冲突：同批次标识不同指纹，422 拒绝且不覆盖首次导入")
        tampered = [order_line("ORD-100-1", "k-100-1", "fp-100-1", amount=9999)]
        st, pl, hs = http("POST", f"{url}/orders/batch", batch("B-100", "sha256:OTHER", tampered))
        print(f"    HTTP {st}: {pl}")
        assert st == 422
        _, order_now, _ = http("GET", f"{url}/orders/ORD-100-1")
        assert order_now["amount_cents"] == 100  # 首次金额未被覆盖

        print("\n5) 缺批次标识/指纹（含空串）：400，不落任何数据")
        for bad in (
            {"request_fingerprint": "fp", "lines": [order_line("ORD-NEVER", "k", "f")]},
            {"batch_id": "", "request_fingerprint": "fp", "lines": [order_line("ORD-NEVER", "k", "f")]},
            {"batch_id": "B-X", "request_fingerprint": "", "lines": [order_line("ORD-NEVER", "k", "f")]},
        ):
            st, _, _ = http("POST", f"{url}/orders/batch", bad)
            print(f"    HTTP {st}")
            assert st == 400

        print("\n6) 中断续跑：第 2 行提交后进程退出，重启后同（租户,批次）同指纹只补做剩余行")
        proc.kill(); proc.wait()
        crash_lines = [order_line(f"ORD-300-{i}", f"k-300-{i}", f"fp-300-{i}", amount=10 * i)
                       for i in range(1, 6)]
        crash_proc, crash_url = start_server(db_file, crash_after_line="2")
        try:
            try:
                http("POST", f"{crash_url}/orders/batch", batch("B-300", "sha256:b300", crash_lines))
                raise AssertionError("崩溃进程不应给出响应")
            except OSError as exc:
                print(f"    导入进行中进程崩溃，连接中断: {type(exc).__name__}")
            crash_proc.wait()
        finally:
            crash_proc.kill() if crash_proc.poll() is None else None
        proc, url = start_server(db_file)
        st, pl, hs = http("POST", f"{url}/orders/batch", batch("B-300", "sha256:b300", crash_lines))
        show_manifest("resume", st, pl, hs)
        assert st == 200 and pl["resumed"] is True and pl["status"] == "completed"
        assert [(i["line_no"], i["replayed"]) for i in pl["lines"]] == [
            (1, True), (2, True), (3, False), (4, False), (5, False)]
        st, pl, hs = http("POST", f"{url}/orders/batch", batch("B-300", "sha256:b300", crash_lines))
        show_manifest("replay-after-resume", st, pl, hs)
        assert hs["X-Idempotency-Replay"] == "true"

        print("\n7) 提交前崩溃恢复：批次登记后、任何一行处理前（APP_CRASH_BATCH_AFTER_LINE=0）崩溃")
        proc.kill(); proc.wait()
        pre_lines = [order_line("ORD-400-1", "k-400-1", "fp-400-1"),
                     order_line("ORD-400-2", "k-400-2", "fp-400-2")]
        crash_proc, crash_url = start_server(db_file, crash_after_line="0")
        try:
            try:
                http("POST", f"{crash_url}/orders/batch", batch("B-400", "sha256:b400", pre_lines))
                raise AssertionError("崩溃进程不应给出响应")
            except OSError as exc:
                print(f"    连接中断: {type(exc).__name__}（批次 running，0 行落库）")
            crash_proc.wait()
        finally:
            crash_proc.kill() if crash_proc.poll() is None else None
        proc, url = start_server(db_file)
        st, pl, hs = http("POST", f"{url}/orders/batch", batch("B-400", "sha256:b400", pre_lines))
        show_manifest("recover", st, pl, hs)
        assert st == 200 and pl["resumed"] is True and pl["counts"]["accepted"] == 2

        print("\n8) 并发：8 个相同（批次标识, 指纹）请求同时到达，仅一批真正导入，其余重放")
        race_body = batch("B-500", "sha256:b500",
                          [order_line(f"ORD-500-{i}", f"k-500-{i}", f"fp-500-{i}") for i in range(1, 4)])

        def one_call():
            return http("POST", f"{url}/orders/batch", race_body)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: one_call(), range(8)))
        firsts = [r for r in results if r[2].get("X-Idempotency-Replay") is None]
        replays = [r for r in results if r[2].get("X-Idempotency-Replay") == "true"]
        print(f"    8 个响应：首次导入 {len(firsts)} 个，批次重放 {len(replays)} 个")
        assert len(firsts) == 1 and len(replays) == 7
        verdicts = {tuple((i["line_no"], i["outcome"]) for i in r[1]["lines"]) for r in results}
        assert len(verdicts) == 1  # 行结论完全一致
    finally:
        proc.kill()
        proc.wait()

    print("\n全部批量受理场景符合预期。")


if __name__ == "__main__":
    main()
