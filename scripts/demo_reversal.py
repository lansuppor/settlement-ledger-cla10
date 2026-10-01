"""收款冲正的真实 HTTP 调用演示。

自包含：使用临时数据库，启动真实 uvicorn 进程，通过 HTTP 依次演示
正常冲正 / 重复冲正（重放）/ 指纹冲突 / 参数不合法 / 不存在（含跨租户）
/ 并发（同冲正标识一笔生效、不同标识仅一笔生效）/ 提交前崩溃恢复。

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
        env["APP_CRASH_REVERSAL_BEFORE_COMMIT"] = "1"
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


def _headers(headers: object) -> dict:
    return {key.title(): value for key, value in dict(headers).items()}


def post(url: str, path: str, body: dict, tenant: str = "demo") -> tuple[int, object, dict]:
    req = urllib.request.Request(
        f"{url}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Tenant": tenant},
        method="POST",
    )
    try:
        with OPENER.open(req, timeout=10) as resp:
            return resp.status, _parse(resp.read()), _headers(resp.headers)
    except urllib.error.HTTPError as error:
        return error.code, _parse(error.read()), _headers(error.headers)


def get(url: str, path: str, tenant: str = "demo") -> tuple[int, object]:
    req = urllib.request.Request(f"{url}{path}", headers={"X-Tenant": tenant}, method="GET")
    try:
        with OPENER.open(req, timeout=10) as resp:
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as error:
        return error.code, _parse(error.read())


def show(label: str, status: int, payload: object, headers: dict | None = None) -> None:
    replay = (headers or {}).get("X-Idempotency-Replay")
    suffix = f"  X-Idempotency-Replay={replay}" if replay else ""
    print(f"  [{label}] -> HTTP {status}{suffix}: {payload}")


def accept_and_pay(url: str, order_id: str, amount: int, *, tenant: str = "demo") -> str:
    """受理订单并登记一笔全额收款，返回服务分配的 payment_id。"""
    body = {
        "tenant": tenant,
        "order_id": order_id,
        "amount_cents": amount,
        "currency": "CNY",
        "idempotency_key": f"key-{order_id}",
        "request_fingerprint": f"sha256:{order_id}",
    }
    status, payload, _ = post(url, "/orders", body, tenant)
    assert status == 201, payload
    status, payload, _ = post(url, f"/orders/{order_id}/payments", {"amount_cents": amount}, tenant)
    assert status == 200, payload["status"] == "settled"
    return payload["payment_id"]


def main() -> None:
    db_file = os.path.join(tempfile.mkdtemp(prefix="reversal-demo-"), "app.sqlite")
    proc, url = start_server(db_file)
    try:
        print("1) 正常冲正：已收抵回、未收回升，已结清 → 受理态")
        payment_id = accept_and_pay(url, "ORD-2001", 500)
        print(f"  收款记录标识 payment_id={payment_id}")
        status, payload, headers = post(
            url, f"/payments/{payment_id}/reversal",
            {"reversal_id": "REV-2001", "request_fingerprint": "sha256:rev2001"},
        )
        show("reverse", status, payload, headers)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None
        order = payload["order"]
        assert order["paid_cents"] == 0 and order["outstanding_cents"] == 500 and order["status"] == "accepted"
        assert order["amount_cents"] == order["paid_cents"] + order["outstanding_cents"]

        print("\n2) 重复冲正（同标识同指纹）：不重复抵回，返回首次结果快照")
        status, payload, headers = post(
            url, f"/payments/{payment_id}/reversal",
            {"reversal_id": "REV-2001", "request_fingerprint": "sha256:rev2001"},
        )
        show("replay", status, payload, headers)
        assert status == 200 and headers["X-Idempotency-Replay"] == "true"
        assert payload["order"]["outstanding_cents"] == 500

        print("\n3) 同冲正标识不同指纹：422 冲突，不覆盖首次冲正与订单")
        status, payload, headers = post(
            url, f"/payments/{payment_id}/reversal",
            {"reversal_id": "REV-2001", "request_fingerprint": "sha256:TAMPERED"},
        )
        show("fingerprint-conflict", status, payload, headers)
        assert status == 422

        print("\n4) 新冲正标识作用于已冲正收款：409 已处理（冲正不可再被冲正，与 422 可区分）")
        status, payload, headers = post(
            url, f"/payments/{payment_id}/reversal",
            {"reversal_id": "REV-2001-OTHER", "request_fingerprint": "sha256:other"},
        )
        show("already-reversed", status, payload, headers)
        assert status == 409

        print("\n5) 缺少冲正标识/指纹：400 参数不合法，不落任何数据")
        fresh_payment = accept_and_pay(url, "ORD-2002", 300)
        for field in ("reversal_id", "request_fingerprint"):
            body = {"reversal_id": "REV-2002", "request_fingerprint": "sha256:rev2002"}
            del body[field]
            status, payload, _ = post(url, f"/payments/{fresh_payment}/reversal", body)
            print(f"  [missing-{field}] -> HTTP {status}: {payload['detail']['error']}")
            assert status == 400
        status, payment_view = get(url, f"/payments/{fresh_payment}")
        assert status == 200 and payment_view["status"] == "active"

        print("\n6) 不存在的收款：404；跨租户冲正同样 404，且不消耗冲正标识")
        status, payload, _ = post(
            url, "/payments/P-NONEXISTENT/reversal",
            {"reversal_id": "REV-X", "request_fingerprint": "sha256:x"},
        )
        show("not-found", status, payload)
        assert status == 404
        status, payload, _ = post(
            url, f"/payments/{fresh_payment}/reversal",
            {"reversal_id": "REV-2002", "request_fingerprint": "sha256:rev2002"},
            tenant="other-tenant",
        )
        show("cross-tenant", status, payload)
        assert status == 404
        # 本租户用同一冲正标识仍可首次生效。
        status, payload, headers = post(
            url, f"/payments/{fresh_payment}/reversal",
            {"reversal_id": "REV-2002", "request_fingerprint": "sha256:rev2002"},
        )
        show("same-id-in-own-tenant", status, payload, headers)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None

        print("\n7) 并发：8 个相同（冲正标识, 指纹）请求，仅一笔真正抵回")
        race_payment = accept_and_pay(url, "ORD-RACE1", 700)
        race_body = {"reversal_id": "REV-RACE1", "request_fingerprint": "sha256:race1"}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: post(url, f"/payments/{race_payment}/reversal", race_body),
                    range(8),
                )
            )
        firsts = [r for r in results if r[2].get("X-Idempotency-Replay") is None]
        replays = [r for r in results if r[2].get("X-Idempotency-Replay") == "true"]
        print(f"  8 个并发响应：首次生效 {len(firsts)} 个，重放 {len(replays)} 个")
        assert len(firsts) == 1 and len(replays) == 7
        status, payload = get(url, "/orders/ORD-RACE1")
        assert payload["paid_cents"] == 0 and payload["outstanding_cents"] == 700

        print("\n8) 并发：8 个不同冲正标识作用于同一收款，恰好一笔生效，其余 409")
        race_payment2 = accept_and_pay(url, "ORD-RACE2", 700)

        def one(i: int) -> tuple[int, object, dict]:
            return post(
                url, f"/payments/{race_payment2}/reversal",
                {"reversal_id": f"REV-RACE2-{i}", "request_fingerprint": f"sha256:{i}"},
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results2 = [f.result() for f in [pool.submit(one, i) for i in range(8)]]
        wins = [r for r in results2 if r[0] == 200]
        refused = [r for r in results2 if r[0] == 409]
        print(f"  8 个并发响应：生效 {len(wins)} 个，已处理拒绝 {len(refused)} 个")
        assert len(wins) == 1 and len(refused) == 7
        status, payload = get(url, "/orders/ORD-RACE2")
        assert payload["paid_cents"] == 0 and payload["outstanding_cents"] == 700

        print("\n9) 崩溃恢复：冲正提交前崩溃（APP_CRASH_REVERSAL_BEFORE_COMMIT=1）后重启重试")
        crash_payment = accept_and_pay(url, "ORD-CRASH", 200)
        proc.kill()
        proc.wait()
        crash_proc, crash_url = start_server(db_file, crash_before_commit=True)
        try:
            post(
                crash_url, f"/payments/{crash_payment}/reversal",
                {"reversal_id": "REV-CRASH", "request_fingerprint": "sha256:crash"},
            )
            raise AssertionError("崩溃进程不应给出响应")
        except OSError as exc:
            print(f"  请求进行中进程崩溃，连接中断: {type(exc).__name__}")
        crash_proc.wait()
        assert crash_proc.returncode == 2

        proc, url = start_server(db_file)
        status, payload = get(url, f"/payments/{crash_payment}")
        print(f"  崩溃重启后收款仍为: {payload['status']}（冲正未落库）")
        assert payload["status"] == "active"
        print("  用同一冲正标识重试：")
        status, payload, headers = post(
            url, f"/payments/{crash_payment}/reversal",
            {"reversal_id": "REV-CRASH", "request_fingerprint": "sha256:crash"},
        )
        show("after-restart", status, payload, headers)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None
        assert payload["order"]["outstanding_cents"] == 200 and payload["order"]["status"] == "accepted"
        print("  再次到达即为确定性重放：")
        status, payload, headers = post(
            url, f"/payments/{crash_payment}/reversal",
            {"reversal_id": "REV-CRASH", "request_fingerprint": "sha256:crash"},
        )
        show("replay", status, payload, headers)
        assert status == 200 and headers["X-Idempotency-Replay"] == "true"
    finally:
        proc.kill()
        proc.wait()

    print("\n全部场景符合预期。")


if __name__ == "__main__":
    main()
