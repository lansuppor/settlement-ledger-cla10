"""收款冲正的真实 HTTP 调用演示。

自包含：使用临时数据库，启动真实 uvicorn 进程，通过 HTTP 依次演示
正常冲正 / 重复冲正（重放）/ 指纹冲突 / 已处理拒绝 / 缺参数 /
不存在（含跨租户）/ 并发 / 提交前崩溃恢复。

    . .venv/bin/activate
    python scripts/demo_reversal.py
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


def _parse(raw: bytes) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode(errors="replace")


def _title_headers(headers: object) -> dict:
    return {key.title(): value for key, value in dict(headers).items()}


def post(url: str, path: str, body: dict, *, tenant: str = "demo") -> tuple[int, object, dict]:
    req = urllib.request.Request(
        f"{url}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Tenant": tenant},
        method="POST",
    )
    try:
        with OPENER.open(req, timeout=10) as resp:
            return resp.status, _parse(resp.read()), _title_headers(resp.headers)
    except urllib.error.HTTPError as error:
        return error.code, _parse(error.read()), _title_headers(error.headers)


def accept_order(url: str, order_id: str, *, amount: int = 500, tenant: str = "demo") -> None:
    body = {
        "tenant": tenant,
        "order_id": order_id,
        "amount_cents": amount,
        "currency": "CNY",
        "idempotency_key": f"key-{order_id}",
        "request_fingerprint": f"fp-{order_id}",
    }
    status, _, _ = post(url, "/orders", body, tenant=tenant)
    assert status == 201, status


def register_payment(url: str, order_id: str, amount: int, *, tenant: str = "demo") -> str:
    status, payload, _ = post(url, f"/orders/{order_id}/payments", {"amount_cents": amount}, tenant=tenant)
    assert status == 200, payload
    return payload["payment_id"]


def reverse(url: str, payment_id: str, reversal_id: str, fp: str, *, tenant: str = "demo"):
    return post(
        url,
        f"/payments/{payment_id}/reversals",
        {"reversal_id": reversal_id, "request_fingerprint": fp},
        tenant=tenant,
    )


def show(label: str, status: int, payload: object, headers: dict) -> None:
    replay = (headers or {}).get("X-Idempotency-Replay")
    suffix = f"  X-Idempotency-Replay={replay}" if replay else ""
    print(f"  [{label}] -> HTTP {status}{suffix}: {payload}")


def main() -> None:
    db_file = os.path.join(tempfile.mkdtemp(prefix="reversal-demo-"), "app.sqlite")
    proc, url = start_server(db_file)
    try:
        print("准备：受理订单 ORD-1001（500），登记两笔收款 300 + 200，订单结清")
        accept_order(url, "ORD-1001")
        p300 = register_payment(url, "ORD-1001", 300)
        p200 = register_payment(url, "ORD-1001", 200)
        print(f"  payment_id 由服务分配：300 那笔={p300}，200 那笔={p200}")

        print("\n1) 正常冲正：抵回 300 那笔，已收回到 200、未收回升 300、状态回到受理态")
        status, payload, headers = reverse(url, p300, "rev-1001", "sha256:rev-aaaa")
        show("reverse", status, payload, headers)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None
        order = payload["order"]
        assert order["paid_cents"] == 200 and order["outstanding_cents"] == 300 and order["status"] == "accepted"
        assert order["amount_cents"] == order["paid_cents"] + order["outstanding_cents"]

        print("\n2) 重复冲正（同标识同指纹）：不重复抵回，返回首次冲正结果快照")
        status, payload, headers = reverse(url, p300, "rev-1001", "sha256:rev-aaaa")
        show("replay", status, payload, headers)
        assert status == 200 and headers["X-Idempotency-Replay"] == "true"
        assert payload["order"]["paid_cents"] == 200

        print("\n3) 指纹冲突（同标识不同指纹）：422，不覆盖首次冲正记录与已登记收款")
        status, payload, headers = reverse(url, p300, "rev-1001", "sha256:rev-bbbb")
        show("conflict", status, payload, headers)
        assert status == 422

        print("\n4) 对已冲正收款换新冲正标识再次冲正：按已处理 409（与 422 可区分）")
        status, payload, headers = reverse(url, p300, "rev-1001-other", "sha256:rev-cccc")
        show("already-reversed", status, payload, headers)
        assert status == 409

        print("\n5) 缺少冲正标识或指纹：400，不落任何数据（以缺 reversal_id 为例）")
        req = urllib.request.Request(
            f"{url}/payments/{p200}/reversals",
            data=json.dumps({"request_fingerprint": "sha256:no-id"}).encode(),
            headers={"Content-Type": "application/json", "X-Tenant": "demo"},
            method="POST",
        )
        try:
            OPENER.open(req, timeout=10)
            raise AssertionError("应返回错误")
        except urllib.error.HTTPError as error:
            print(f"  -> HTTP {error.code}: {json.loads(error.read())['detail']['error']}")
            assert error.code == 400

        print("\n6) 不存在 / 跨租户：一律按不存在 404，不改变订单与收款")
        status, payload, _ = reverse(url, "no-such-payment", "rev-x", "sha256:x")
        show("not-found", status, payload, {})
        assert status == 404
        status, payload, _ = reverse(url, p200, "rev-cross", "sha256:cross", tenant="other-tenant")
        show("cross-tenant", status, payload, {})
        assert status == 404

        print("\n7) 并发：8 个携带相同冲正标识的请求同时冲向同一收款")
        accept_order(url, "ORD-RACE")
        p_race = register_payment(url, "ORD-RACE", 200)
        body = {"reversal_id": "rev-race", "request_fingerprint": "sha256:race"}

        def one_call() -> tuple[int, str | None, object]:
            st, pl, hs = reverse(url, p_race, body["reversal_id"], body["request_fingerprint"])
            return st, hs.get("X-Idempotency-Replay"), pl

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: one_call(), range(8)))
        firsts = [r for r in results if r[1] is None]
        replays = [r for r in results if r[1] == "true"]
        print(f"  8 个并发响应：首次生效 {len(firsts)} 个，重放 {len(replays)} 个；响应体一致: "
              f"{len({json.dumps(r[2], sort_keys=True) for r in results}) == 1}")
        assert len(firsts) == 1 and len(replays) == 7

        print("\n8) 并发：8 个不同冲正标识同时冲正同一收款，仅一笔真正生效，其余确定为 409")
        accept_order(url, "ORD-RACE2")
        p_race2 = register_payment(url, "ORD-RACE2", 150)
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(
                lambda i: reverse(url, p_race2, f"rev-race2-{i}", f"sha256:r2-{i}")[0], range(8)
            ))
        print(f"  状态分布：200 x {statuses.count(200)}，409 x {statuses.count(409)}")
        assert statuses.count(200) == 1 and statuses.count(409) == 7

        print("\n9) 崩溃恢复：提交前崩溃（APP_CRASH_BEFORE_COMMIT=1）后重启重试")
        accept_order(url, "ORD-CRASH")
        p_crash = register_payment(url, "ORD-CRASH", 180)
        proc.kill()
        proc.wait()
        crash_proc, crash_url = start_server(db_file, crash_before_commit=True)
        try:
            reverse(crash_url, p_crash, "rev-crash", "sha256:crash")
            raise AssertionError("崩溃进程不应给出响应")
        except OSError as exc:
            print(f"  请求进行中进程崩溃，连接中断: {type(exc).__name__}")
        crash_proc.wait()
        assert crash_proc.returncode == 2

        proc, url = start_server(db_file)
        print("  重启后用同一冲正标识重试：")
        status, payload, headers = reverse(url, p_crash, "rev-crash", "sha256:crash")
        show("after-restart", status, payload, headers)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None
        assert payload["order"]["paid_cents"] == 0
        print("  再次到达即为确定性重放：")
        status, payload, headers = reverse(url, p_crash, "rev-crash", "sha256:crash")
        show("replay", status, payload, headers)
        assert status == 200 and headers["X-Idempotency-Replay"] == "true"
    finally:
        proc.kill()
        proc.wait()

    print("\n全部场景符合预期。")


if __name__ == "__main__":
    main()
