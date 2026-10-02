"""分期收款的真实 HTTP 调用演示。

自包含：使用临时数据库，启动真实 uvicorn 进程，通过 HTTP 依次演示
多笔分期登记 / 超应收拒绝 / 同标识重放 / 业务标识指纹冲突 / 参数不合法 /
分期读取（含跨租户）/ 分期冲正 / 冲正重放与冲突 / 已冲正再冲正 /
并发（同业务标识一笔生效、不同冲正标识仅一笔生效）/ 提交前崩溃恢复。

    . .venv/bin/activate
    python scripts/demo_installments.py
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


def start_server(db_file: str, *, crash_register: bool = False, crash_reversal: bool = False) -> tuple[subprocess.Popen, str]:
    port = free_port()
    env = {**os.environ, "APP_DB": db_file, "PYTHONPATH": str(ROOT)}
    if crash_register:
        env["APP_CRASH_INSTALLMENT_BEFORE_COMMIT"] = "1"
    if crash_reversal:
        env["APP_CRASH_INSTALLMENT_REVERSAL_BEFORE_COMMIT"] = "1"
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


def accept_order(url: str, order_id: str, amount: int, *, tenant: str = "demo") -> None:
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


def register(url: str, order_id: str, key: str, fp: str, amount: int, *, tenant: str = "demo"):
    return post(
        url, f"/orders/{order_id}/installments",
        {"installment_key": key, "request_fingerprint": fp, "amount_cents": amount},
        tenant,
    )


def reverse(url: str, installment_id: str, reversal_id: str, fp: str, *, tenant: str = "demo"):
    return post(
        url, f"/installments/{installment_id}/reversal",
        {"reversal_id": reversal_id, "request_fingerprint": fp},
        tenant,
    )


def main() -> None:
    db_file = os.path.join(tempfile.mkdtemp(prefix="installment-demo-"), "app.sqlite")
    proc, url = start_server(db_file)
    try:
        print("1) 多笔分期登记：已收逐笔增加、未收逐笔下降，未收清零为已结清")
        accept_order(url, "ORD-3001", 1000)
        status, payload, headers = register(url, "ORD-3001", "INST-3001-1", "sha256:inst1", 300)
        show("installment-1", status, payload, headers)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None
        installment_id = payload["installment_id"]
        order = payload["order"]
        assert order["paid_cents"] == 300 and order["outstanding_cents"] == 700 and order["status"] == "accepted"
        status, payload, _ = register(url, "ORD-3001", "INST-3001-2", "sha256:inst2", 700)
        show("installment-2", status, payload)
        assert status == 200 and payload["order"]["status"] == "settled"
        assert payload["order"]["paid_cents"] == 1000 and payload["order"]["outstanding_cents"] == 0
        print(f"  服务分配的分期记录标识 installment_id={installment_id}")

        print("\n2) 分期超过未收金额：409 拒绝，不改变订单与任何分期")
        status, payload, _ = register(url, "ORD-3001", "INST-3001-X", "sha256:instx", 1)
        show("exceeds-outstanding", status, payload)
        assert status == 409

        print("\n3) 同业务标识同指纹重放：不新增分期，返回首次登记结果快照")
        status, payload, headers = register(url, "ORD-3001", "INST-3001-1", "sha256:inst1", 300)
        show("replay", status, payload, headers)
        assert status == 200 and headers["X-Idempotency-Replay"] == "true"
        assert payload["installment_id"] == installment_id
        assert payload["order"]["paid_cents"] == 300 and payload["order"]["outstanding_cents"] == 700

        print("\n4) 同业务标识不同指纹：422 冲突，不覆盖首次记录")
        status, payload, _ = register(url, "ORD-3001", "INST-3001-1", "sha256:TAMPERED", 999)
        show("fingerprint-conflict", status, payload)
        assert status == 422

        print("\n5) 缺少业务标识/指纹或非法金额：400 参数不合法，不落任何数据")
        accept_order(url, "ORD-3002", 300)
        for field in ("installment_key", "request_fingerprint"):
            body = {"installment_key": "INST-3002", "request_fingerprint": "sha256:i3002", "amount_cents": 100}
            del body[field]
            status, payload, _ = post(url, "/orders/ORD-3002/installments", body)
            print(f"  [missing-{field}] -> HTTP {status}: {payload['detail']['error']}")
            assert status == 400
        status, _, _ = register(url, "ORD-3002", "", "sha256:i3002", 100)
        assert status == 400
        status, _, _ = register(url, "ORD-3002", "INST-3002", "sha256:i3002", 0)
        assert status == 400
        status, order_view = get(url, "/orders/ORD-3002")
        assert order_view["paid_cents"] == 0 and order_view["outstanding_cents"] == 300

        print("\n6) 分期读取：按服务分配标识回读；不存在与跨租户均 404")
        status, payload = get(url, f"/installments/{installment_id}")
        show("read", status, payload)
        assert status == 200 and payload["status"] == "active"
        assert get(url, f"/installments/{installment_id}", tenant="other-tenant")[0] == 404
        assert get(url, "/installments/INONEXISTENT")[0] == 404

        print("\n7) 分期冲正：已收减少、未收回升，已结清 → 受理态")
        status, payload, headers = reverse(url, installment_id, "IREV-3001", "sha256:rev3001")
        show("reverse", status, payload, headers)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None
        order = payload["order"]
        assert order["paid_cents"] == 700 and order["outstanding_cents"] == 300 and order["status"] == "accepted"
        assert order["amount_cents"] == order["paid_cents"] + order["outstanding_cents"]
        assert get(url, f"/installments/{installment_id}")[1]["status"] == "reversed"

        print("\n8) 冲正重放 / 指纹冲突 / 换新标识再冲正：200重放、422冲突、409已处理")
        status, payload, headers = reverse(url, installment_id, "IREV-3001", "sha256:rev3001")
        show("reversal-replay", status, payload, headers)
        assert status == 200 and headers["X-Idempotency-Replay"] == "true"
        status, payload, _ = reverse(url, installment_id, "IREV-3001", "sha256:OTHER")
        show("reversal-fingerprint-conflict", status, payload)
        assert status == 422
        status, payload, _ = reverse(url, installment_id, "IREV-3001-OTHER", "sha256:other")
        show("already-reversed", status, payload)
        assert status == 409

        print("\n9) 并发：8 个相同（业务标识, 指纹）登记请求，仅一笔真正计入")
        accept_order(url, "ORD-RACE1", 700)
        race_body = {"installment_key": "INST-RACE1", "request_fingerprint": "sha256:race1", "amount_cents": 200}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(lambda _: post(url, "/orders/ORD-RACE1/installments", race_body), range(8))
            )
        firsts = [r for r in results if r[2].get("X-Idempotency-Replay") is None]
        replays = [r for r in results if r[2].get("X-Idempotency-Replay") == "true"]
        print(f"  8 个并发响应：首次生效 {len(firsts)} 个，重放 {len(replays)} 个")
        assert len(firsts) == 1 and len(replays) == 7
        assert get(url, "/orders/ORD-RACE1")[1]["paid_cents"] == 200

        print("\n10) 并发：8 个不同冲正标识作用于同一分期，恰好一笔生效，其余 409")
        race_iid = results[0][1]["installment_id"]

        def one(i: int) -> tuple[int, object, dict]:
            return reverse(url, race_iid, f"IREV-RACE1-{i}", f"sha256:{i}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results2 = [f.result() for f in [pool.submit(one, i) for i in range(8)]]
        wins = [r for r in results2 if r[0] == 200]
        refused = [r for r in results2 if r[0] == 409]
        print(f"  8 个并发响应：生效 {len(wins)} 个，已处理拒绝 {len(refused)} 个")
        assert len(wins) == 1 and len(refused) == 7
        assert get(url, "/orders/ORD-RACE1")[1]["paid_cents"] == 0

        print("\n11) 崩溃恢复：分期登记提交前崩溃（APP_CRASH_INSTALLMENT_BEFORE_COMMIT=1）后重启重试")
        accept_order(url, "ORD-CRASH1", 200)
        proc.kill()
        proc.wait()
        crash_proc, crash_url = start_server(db_file, crash_register=True)
        try:
            register(crash_url, "ORD-CRASH1", "INST-CRASH1", "sha256:crash1", 200)
            raise AssertionError("崩溃进程不应给出响应")
        except OSError as exc:
            print(f"  请求进行中进程崩溃，连接中断: {type(exc).__name__}")
        crash_proc.wait()
        assert crash_proc.returncode == 2
        proc, url = start_server(db_file)
        assert get(url, "/orders/ORD-CRASH1")[1]["outstanding_cents"] == 200
        status, payload, headers = register(url, "ORD-CRASH1", "INST-CRASH1", "sha256:crash1", 200)
        show("after-restart", status, payload, headers)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None
        assert payload["order"]["status"] == "settled"
        crash_iid = payload["installment_id"]

        print("\n12) 崩溃恢复：分期冲正提交前崩溃（APP_CRASH_INSTALLMENT_REVERSAL_BEFORE_COMMIT=1）后重启重试")
        proc.kill()
        proc.wait()
        crash_proc, crash_url = start_server(db_file, crash_reversal=True)
        try:
            reverse(crash_url, crash_iid, "IREV-CRASH", "sha256:rcrash")
            raise AssertionError("崩溃进程不应给出响应")
        except OSError as exc:
            print(f"  请求进行中进程崩溃，连接中断: {type(exc).__name__}")
        crash_proc.wait()
        assert crash_proc.returncode == 2
        proc, url = start_server(db_file)
        assert get(url, f"/installments/{crash_iid}")[1]["status"] == "active"
        status, payload, headers = reverse(url, crash_iid, "IREV-CRASH", "sha256:rcrash")
        show("after-restart", status, payload, headers)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None
        assert payload["order"]["status"] == "accepted" and payload["order"]["outstanding_cents"] == 200
        status, payload, headers = reverse(url, crash_iid, "IREV-CRASH", "sha256:rcrash")
        show("replay", status, payload, headers)
        assert status == 200 and headers["X-Idempotency-Replay"] == "true"
    finally:
        proc.kill()
        proc.wait()

    print("\n全部场景符合预期。")


if __name__ == "__main__":
    main()
