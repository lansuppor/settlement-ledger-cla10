"""分期收款登记与分期冲正的真实 HTTP 调用演示。

自包含：使用临时数据库，启动真实 uvicorn 进程，通过 HTTP 依次演示
多笔分期登记 / 重复登记（重放）/ 业务标识指纹冲突 / 超未收拒绝 / 缺参数 /
分期读取（含跨租户）/ 分期冲正 / 冲正重放 / 冲正指纹冲突 / 已冲正再冲正 /
并发登记（同标识一笔生效、不同标识金额闭合）/ 并发冲正 / 提交前崩溃恢复。

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


def start_server(db_file: str, *, env_extra: dict | None = None) -> tuple[subprocess.Popen, str]:
    port = free_port()
    env = {**os.environ, "APP_DB": db_file, "PYTHONPATH": str(ROOT)}
    if env_extra:
        env.update(env_extra)
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
        "request_fingerprint": f"sha256:order-{order_id}",
    }
    status, payload, _ = post(url, "/orders", body, tenant)
    assert status == 201, payload


def install(url: str, order_id: str, key: str, amount: int, fp: str | None = None, *, tenant: str = "demo"):
    return post(
        url, f"/orders/{order_id}/installments",
        {"installment_key": key, "request_fingerprint": fp or f"sha256:{key}", "amount_cents": amount},
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
        print("1) 一张订单按多笔分期登记：已收逐笔上升、未收逐笔下降，最终结清")
        accept_order(url, "ORD-3001", 500)
        status, payload, headers = install(url, "ORD-3001", "INST-3001-1", 200)
        show("installment-1", status, payload, headers)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None
        installment_id = payload["installment_id"]
        assert payload["order"]["paid_cents"] == 200 and payload["order"]["outstanding_cents"] == 300
        assert payload["order"]["amount_cents"] == payload["order"]["paid_cents"] + payload["order"]["outstanding_cents"]
        status, payload, _ = install(url, "ORD-3001", "INST-3001-2", 300)
        show("installment-2", status, payload)
        assert status == 200 and payload["order"]["status"] == "settled" and payload["order"]["outstanding_cents"] == 0

        print("\n2) 按服务分配的分期记录标识读取分期；跨租户读取按不存在处理")
        status, payload = get(url, f"/installments/{installment_id}")
        show("read", status, payload)
        assert status == 200 and payload["status"] == "active"
        status, payload = get(url, f"/installments/{installment_id}", tenant="other-tenant")
        show("cross-tenant-read", status, payload)
        assert status == 404

        print("\n3) 同一业务标识再次到达（同指纹）：重放，不新增分期、不改变订单，返回首次快照")
        status, payload, headers = install(url, "ORD-3001", "INST-3001-1", 200)
        show("replay", status, payload, headers)
        assert status == 200 and headers["X-Idempotency-Replay"] == "true"
        assert payload["order"]["paid_cents"] == 200  # 首次快照，而非当前已收 500

        print("\n4) 同一业务标识不同指纹：422 标识冲突，不覆盖首次记录")
        status, payload, _ = install(url, "ORD-3001", "INST-3001-1", 200, fp="sha256:TAMPERED")
        show("fingerprint-conflict", status, payload)
        assert status == 422

        print("\n5) 分期登记超过未收金额：409 拒绝，不改变订单与任何分期")
        accept_order(url, "ORD-3002", 300)
        assert install(url, "ORD-3002", "INST-3002-1", 200)[0] == 200
        status, payload, _ = install(url, "ORD-3002", "INST-3002-2", 200)
        show("exceeds-outstanding", status, payload)
        assert status == 409
        status, order = get(url, "/orders/ORD-3002")
        assert order["paid_cents"] == 200 and order["outstanding_cents"] == 100

        print("\n6) 缺少业务标识/指纹（含空串）或金额非正：400 参数不合法，不落任何数据")
        base = {"installment_key": "INST-BAD", "request_fingerprint": "sha256:bad", "amount_cents": 100}
        for field in ("installment_key", "request_fingerprint"):
            bad = dict(base)
            del bad[field]
            status, payload, _ = post(url, "/orders/ORD-3002/installments", bad)
            print(f"  [missing-{field}] -> HTTP {status}: {payload['detail']['error']}")
            assert status == 400
        for bad in (
            {"installment_key": "", "request_fingerprint": "fp", "amount_cents": 100},
            {"installment_key": "K", "request_fingerprint": "fp", "amount_cents": 0},
        ):
            assert post(url, "/orders/ORD-3002/installments", bad)[0] == 400

        print("\n7) 作用于不存在/跨租户订单：404，且不消耗业务标识")
        status, payload, _ = install(url, "ORD-NONE", "INST-X", 10)
        show("order-not-found", status, payload)
        assert status == 404
        status, _, _ = install(url, "ORD-3002", "INST-3002-X", 10, tenant="other-tenant")
        assert status == 404
        # 本租户用同一标识仍可首次生效。
        status, _, headers = install(url, "ORD-3002", "INST-3002-X", 10)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None

        print("\n8) 冲正一笔分期：已收抵回、未收回升，已结清 → 受理态")
        accept_order(url, "ORD-3003", 400)
        target = install(url, "ORD-3003", "INST-3003-1", 300)[1]["installment_id"]
        install(url, "ORD-3003", "INST-3003-2", 100)
        status, payload, headers = reverse(url, target, "IREV-3003", "sha256:irev3003")
        show("reverse", status, payload, headers)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None
        order = payload["order"]
        assert order["paid_cents"] == 100 and order["outstanding_cents"] == 300 and order["status"] == "accepted"
        assert order["amount_cents"] == order["paid_cents"] + order["outstanding_cents"]

        print("\n9) 分期冲正重放：同标识同指纹不重复抵回，返回首次冲正快照")
        status, payload, headers = reverse(url, target, "IREV-3003", "sha256:irev3003")
        show("reversal-replay", status, payload, headers)
        assert status == 200 and headers["X-Idempotency-Replay"] == "true"
        assert payload["order"]["paid_cents"] == 100

        print("\n10) 同冲正标识不同指纹：422；换新标识冲正已冲正分期：409（两类可区分）")
        status, payload, _ = reverse(url, target, "IREV-3003", "sha256:TAMPERED")
        show("reversal-fingerprint-conflict", status, payload)
        assert status == 422
        status, payload, _ = reverse(url, target, "IREV-3003-OTHER", "sha256:other")
        show("already-reversed", status, payload)
        assert status == 409

        print("\n11) 冲正缺字段：400；冲正不存在/跨租户分期：404 且不消耗冲正标识")
        for field in ("reversal_id", "request_fingerprint"):
            body = {"reversal_id": "IREV-X", "request_fingerprint": "sha256:x"}
            del body[field]
            assert post(url, f"/installments/{target}/reversal", body)[0] == 400
        assert reverse(url, "I-NONEXISTENT", "IREV-N", "sha256:n")[0] == 404
        accept_order(url, "ORD-3004", 200, tenant="tenant-a")
        cross_inst = install(url, "ORD-3004", "INST-3004", 200, tenant="tenant-a")[1]["installment_id"]
        assert reverse(url, cross_inst, "IREV-3004", "sha256:irev3004", tenant="tenant-b")[0] == 404
        status, _, headers = reverse(url, cross_inst, "IREV-3004", "sha256:irev3004", tenant="tenant-a")
        assert status == 200 and headers.get("X-Idempotency-Replay") is None

        print("\n12) 并发：8 个相同（业务标识, 指纹）分期登记，仅一笔真正计入")
        accept_order(url, "ORD-RACE1", 700)
        body = {"installment_key": "INST-RACE1", "request_fingerprint": "sha256:race1", "amount_cents": 300}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: post(url, "/orders/ORD-RACE1/installments", body), range(8)))
        firsts = [r for r in results if r[2].get("X-Idempotency-Replay") is None]
        replays = [r for r in results if r[2].get("X-Idempotency-Replay") == "true"]
        print(f"  8 个并发响应：首次生效 {len(firsts)} 个，重放 {len(replays)} 个")
        assert len(firsts) == 1 and len(replays) == 7
        assert get(url, "/orders/ORD-RACE1")[1]["paid_cents"] == 300

        print("\n13) 并发：8 个不同业务标识各 200 作用于金额 600 的订单，恰好 3 笔生效、5 笔 409")
        accept_order(url, "ORD-RACE2", 600)

        def one(i: int) -> tuple[int, object, dict]:
            return post(
                url, "/orders/ORD-RACE2/installments",
                {"installment_key": f"INST-RACE2-{i}", "request_fingerprint": f"sha256:{i}", "amount_cents": 200},
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results2 = [f.result() for f in [pool.submit(one, i) for i in range(8)]]
        wins = [r for r in results2 if r[0] == 200]
        refused = [r for r in results2 if r[0] == 409]
        print(f"  8 个并发响应：生效 {len(wins)} 个，超未收拒绝 {len(refused)} 个")
        assert len(wins) == 3 and len(refused) == 5
        race2_order = get(url, "/orders/ORD-RACE2")[1]
        assert race2_order["paid_cents"] == 600 and race2_order["outstanding_cents"] == 0

        print("\n14) 并发：8 个不同冲正标识作用于同一分期，恰好一笔生效，其余 409")
        race_inst = wins[0][1]["installment_id"]

        def rev_one(i: int) -> tuple[int, object, dict]:
            return reverse(url, race_inst, f"IREV-RACE2-{i}", f"sha256:{i}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results3 = [f.result() for f in [pool.submit(rev_one, i) for i in range(8)]]
        rev_wins = [r for r in results3 if r[0] == 200]
        rev_refused = [r for r in results3 if r[0] == 409]
        print(f"  8 个并发响应：生效 {len(rev_wins)} 个，已处理拒绝 {len(rev_refused)} 个")
        assert len(rev_wins) == 1 and len(rev_refused) == 7

        print("\n15) 崩溃恢复：分期登记提交前崩溃（APP_CRASH_INSTALLMENT_BEFORE_COMMIT=1）后重启重试")
        accept_order(url, "ORD-CRASH1", 200)
        proc.kill()
        proc.wait()
        crash_proc, crash_url = start_server(db_file, env_extra={"APP_CRASH_INSTALLMENT_BEFORE_COMMIT": "1"})
        try:
            install(crash_url, "ORD-CRASH1", "INST-CRASH1", 200)
            raise AssertionError("崩溃进程不应给出响应")
        except OSError as exc:
            print(f"  请求进行中进程崩溃，连接中断: {type(exc).__name__}")
        crash_proc.wait()
        assert crash_proc.returncode == 2
        proc, url = start_server(db_file)
        assert get(url, "/orders/ORD-CRASH1")[1]["paid_cents"] == 0
        status, payload, headers = install(url, "ORD-CRASH1", "INST-CRASH1", 200)
        show("after-restart", status, payload, headers)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None
        assert payload["order"]["status"] == "settled"

        print("\n16) 崩溃恢复：分期冲正提交前崩溃（APP_CRASH_INSTALLMENT_REVERSAL_BEFORE_COMMIT=1）后重启重试")
        accept_order(url, "ORD-CRASH2", 200)
        crash_inst = install(url, "ORD-CRASH2", "INST-CRASH2", 200)[1]["installment_id"]
        proc.kill()
        proc.wait()
        crash_proc, crash_url = start_server(
            db_file, env_extra={"APP_CRASH_INSTALLMENT_REVERSAL_BEFORE_COMMIT": "1"}
        )
        try:
            reverse(crash_url, crash_inst, "IREV-CRASH2", "sha256:crash2")
            raise AssertionError("崩溃进程不应给出响应")
        except OSError:
            print("  请求进行中进程崩溃，连接中断")
        crash_proc.wait()
        assert crash_proc.returncode == 2
        proc, url = start_server(db_file)
        assert get(url, f"/installments/{crash_inst}")[1]["status"] == "active"
        status, payload, headers = reverse(url, crash_inst, "IREV-CRASH2", "sha256:crash2")
        show("after-restart", status, payload, headers)
        assert status == 200 and headers.get("X-Idempotency-Replay") is None
        assert payload["order"]["status"] == "accepted" and payload["order"]["outstanding_cents"] == 200
        status, _, headers = reverse(url, crash_inst, "IREV-CRASH2", "sha256:crash2")
        assert status == 200 and headers["X-Idempotency-Replay"] == "true"
    finally:
        proc.kill()
        proc.wait()

    print("\n全部场景符合预期。")


if __name__ == "__main__":
    main()
