"""幂等受理的真实 HTTP 调用演示。

自包含：使用临时数据库，启动真实 uvicorn 进程，通过 HTTP 依次演示
首次受理 / 重放 / 指纹冲突 / 订单重复 / 缺参数 / 并发 / 崩溃恢复。

    . .venv/bin/activate
    python scripts/demo_idempotency.py
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


def start_server(db_file: str, *, crash_before_commit: bool = False) -> tuple[subprocess.Popen, str]:
    port = free_port()
    env = {**os.environ, "APP_DB": db_file, "PYTHONPATH": str(ROOT)}
    if crash_before_commit:
        env["APP_CRASH_BEFORE_COMMIT"] = "1"
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


def call(url: str, body: dict) -> tuple[int, object, dict | None]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{url}/orders",
        data=data,
        headers={"Content-Type": "application/json", "X-Tenant": body["tenant"]},
        method="POST",
    )
    try:
        with OPENER.open(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, _parse(raw), _title_headers(resp.headers)
    except urllib.error.HTTPError as error:
        return error.code, _parse(error.read()), _title_headers(error.headers)


def _title_headers(headers: object) -> dict:
    # urllib 把头名规范化为小写；标题化后便于按 X-Idempotency-Replay 取值。
    return {key.title(): value for key, value in dict(headers).items()}


def _parse(raw: bytes) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode(errors="replace")


def order_body(order_id: str, key: str, fp: str, *, amount: int = 100, tenant: str = "demo") -> dict:
    return {
        "tenant": tenant,
        "order_id": order_id,
        "amount_cents": amount,
        "currency": "CNY",
        "idempotency_key": key,
        "request_fingerprint": fp,
    }


def show(title: str, status: int, payload: object, headers: dict | None) -> None:
    replay = (headers or {}).get("X-Idempotency-Replay")
    suffix = f"  X-Idempotency-Replay={replay}" if replay else ""
    print(f"  -> HTTP {status}{suffix}: {payload}")


def main() -> None:
    db_file = os.path.join(tempfile.mkdtemp(prefix="idem-demo-"), "app.sqlite")
    proc, url = start_server(db_file)
    try:
        print("1) 首次受理：新建订单，返回 201")
        body = order_body("ORD-1001", "key-1001", "sha256:aaaa", amount=500)
        status, payload, headers = call(url, body)
        show("first", status, payload, headers)
        assert status == 201 and payload["outstanding_cents"] == 500

        print("\n2) 网络重试 / 重复投递：同键同指纹，不新建订单、不重复收款")
        # 先登记一笔收款，证明重放返回的是首次受理快照而非当前订单状态。
        pay_req = urllib.request.Request(
            f"{url}/orders/ORD-1001/payments",
            data=b'{"amount_cents": 200}',
            headers={"Content-Type": "application/json", "X-Tenant": "demo"},
            method="POST",
        )
        with OPENER.open(pay_req, timeout=10) as resp:
            paid = json.loads(resp.read())
        print(f"  已登记收款 200，当前订单 paid_cents={paid['paid_cents']}")
        status, payload, headers = call(url, body)
        show("replay", status, payload, headers)
        assert status == 201 and headers["X-Idempotency-Replay"] == "true"
        assert payload["paid_cents"] == 0 and payload["outstanding_cents"] == 500

        print("\n3) 同键不同指纹：拒绝 422，且不覆盖首次的订单/金额/币种/收款")
        stolen = order_body("ORD-1001", "key-1001", "sha256:bbbb", amount=9999)
        status, payload, headers = call(url, stolen)
        show("conflict", status, payload, headers)
        assert status == 422

        print("\n4) 同订单标识换新幂等键：按订单重复受理返回 409（与 422 可区分）")
        dup_order = order_body("ORD-1001", "key-other", "sha256:cccc")
        status, payload, headers = call(url, dup_order)
        show("dup-order", status, payload, headers)
        assert status == 409

        print("\n5) 缺少幂等键：参数不合法 400，不落任何数据")
        bad = {k: v for k, v in body.items() if k != "idempotency_key"}
        bad["order_id"] = "ORD-NEVER"
        req = urllib.request.Request(
            f"{url}/orders",
            data=json.dumps(bad).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            OPENER.open(req, timeout=10)
            raise AssertionError("应返回错误")
        except urllib.error.HTTPError as error:
            print(f"  -> HTTP {error.code}: {json.loads(error.read())['detail']['error']}")
            assert error.code == 400

        print("\n6) 并发：8 个携带相同幂等键的请求同时到达")
        race_body = order_body("ORD-RACE", "key-race", "sha256:race")

        def one_call() -> tuple[int, str | None, dict]:
            st, pl, hs = call(url, race_body)
            return st, hs.get("X-Idempotency-Replay"), pl

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: one_call(), range(8)))
        firsts = [r for r in results if r[1] is None]
        replays = [r for r in results if r[1] == "true"]
        print(f"  8 个并发响应：首次受理 {len(firsts)} 个，重放 {len(replays)} 个")
        print(f"  8 个响应体完全相同: {len({json.dumps(r[2], sort_keys=True) for r in results}) == 1}")
        assert len(firsts) == 1 and len(replays) == 7

        print("\n7) 崩溃恢复：提交前崩溃（APP_CRASH_BEFORE_COMMIT=1）后重启重试")
        proc.kill()
        proc.wait()
        crash_proc, crash_url = start_server(db_file, crash_before_commit=True)
        crash_body = order_body("ORD-CRASH", "key-crash", "sha256:crash")
        try:
            call(crash_url, crash_body)
            raise AssertionError("崩溃进程不应给出响应")
        except OSError as exc:
            print(f"  请求进行中进程崩溃，连接中断: {type(exc).__name__}")
        crash_proc.wait()
        assert crash_proc.returncode == 2

        proc, url = start_server(db_file)
        print("  重启后用同一幂等键重试：")
        status, payload, headers = call(url, crash_body)
        show("after-restart", status, payload, headers)
        assert status == 201 and headers.get("X-Idempotency-Replay") is None
        print("  再次到达即为确定性重放：")
        status, payload, headers = call(url, crash_body)
        show("replay", status, payload, headers)
        assert status == 201 and headers["X-Idempotency-Replay"] == "true"
    finally:
        proc.kill()
        proc.wait()

    print("\n全部场景符合预期。")


if __name__ == "__main__":
    main()
