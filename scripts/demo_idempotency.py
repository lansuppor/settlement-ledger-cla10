#!/usr/bin/env python3
"""幂等受理演示：通过真实 HTTP 调用观察受理 / 重放 / 冲突 / 并发 / 崩溃恢复。

用法：
  python3 scripts/demo_idempotency.py            # 启动临时服务并演示全部场景
  python3 scripts/demo_idempotency.py --port 8000  # 对已运行的服务演示

场景：
  1. 首次受理            201
  2. 同键同指纹重放       201 + X-Idempotent-Replay: true，同一订单对象
  3. 同键异指纹冲突       409 idempotency key reused with different request fingerprint
  4. 异键同订单标识       409 order already accepted（与上一类明确区分）
  5. 缺幂等键             422，不落数据
  6. 并发同键             8 路真实 HTTP 并发：恰好 1 笔受理、7 笔重放
  7. 崩溃恢复             直接在库中模拟"未提交即被杀"，重启服务后同键重试仍为首次受理
"""
import argparse
import json
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def post(base: str, path: str, body: dict, headers: dict | None = None) -> tuple[int, dict, dict]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read()), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read()), {k.lower(): v for k, v in error.headers.items()}


def get(base: str, path: str, headers: dict | None = None) -> tuple[int, dict | None]:
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as error:
        return error.code, None


def start_server(db: str, port: int) -> subprocess.Popen:
    env = {**os.environ, "APP_DB": db}
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.entry", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            return proc
        except urllib.error.URLError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("server did not start")


def show(title: str, status: int, body: dict, headers: dict) -> None:
    replay = headers.get("x-idempotent-replay")
    extra = f"  X-Idempotent-Replay={replay}" if replay else ""
    print(f"[{title}] HTTP {status}{extra}")
    print(f"    body={body}")


def crash_mid_accept(crash_db: str) -> None:
    """在独立进程中制造"两行已写入、事务未提交即被杀"的崩溃现场。"""
    os.environ["APP_DB"] = crash_db
    sys.path.insert(0, os.getcwd())
    from app.store.db import connect
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) "
        "VALUES(?,?,?,0,?,'accepted')",
        ("demo", "ord-crash", 424, "CNY"),
    )
    conn.execute(
        "INSERT INTO idempotency_keys(tenant, idempotency_key, request_fingerprint, order_id) "
        "VALUES(?,?,?,?)",
        ("demo", "K-CRASH", "F-CRASH", "ord-crash"),
    )
    time.sleep(30)  # 不 COMMIT，等待被杀死


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=None, help="对已运行的服务演示，不再自建服务")
    args = parser.parse_args()

    own_proc = None
    if args.port:
        base = f"http://127.0.0.1:{args.port}"
        db = os.environ.get("APP_DB", "var/app.sqlite")
    else:
        db = os.path.join(tempfile.mkdtemp(prefix="idem-demo-"), "demo.sqlite")
        port = 8931
        own_proc = start_server(db, port)
        base = f"http://127.0.0.1:{port}"
        print(f"临时服务已启动（db={db}）")

    try:
        order = {"tenant": "demo", "order_id": "ord-1", "amount_cents": 1000, "currency": "CNY"}

        print("\n== 1. 首次受理 ==")
        s, b, h = post(base, "/orders", {**order, "idempotency_key": "K1", "request_fingerprint": "F1"})
        show("accept", s, b, h)
        assert s == 201 and h.get("x-idempotent-replay") is None

        print("\n== 2. 同键同指纹重放（网络重试 / 重复投递）==")
        s, b, h = post(base, "/orders", {**order, "idempotency_key": "K1", "request_fingerprint": "F1"})
        show("replay", s, b, h)
        assert s == 201 and h.get("x-idempotent-replay") == "true"
        assert b["order_id"] == "ord-1" and b["amount_cents"] == 1000

        print("\n== 3. 同键异指纹冲突（同一键被用于不同业务内容）==")
        other = {"tenant": "demo", "order_id": "ord-OTHER", "amount_cents": 9999, "currency": "USD"}
        s, b, h = post(base, "/orders", {**other, "idempotency_key": "K1", "request_fingerprint": "F2"})
        show("conflict", s, b, h)
        assert s == 409 and b["detail"] == "idempotency key reused with different request fingerprint"
        # 首次受理数据完好。
        s, b = get(base, "/orders/ord-1", headers={"X-Tenant": "demo"})
        print(f"[first order intact] HTTP {s} amount={b['amount_cents']} currency={b['currency']}")
        assert b["amount_cents"] == 1000 and b["currency"] == "CNY"

        print("\n== 4. 不同幂等键、同一订单标识（两类 409 必须可区分）==")
        s, b, h = post(base, "/orders", {**order, "idempotency_key": "K2", "request_fingerprint": "F1"})
        show("dup-order-id", s, b, h)
        assert s == 409 and b["detail"] == "order already accepted"

        print("\n== 5. 缺少幂等键：422 且不落数据 ==")
        s, b, h = post(base, "/orders", {**order, "request_fingerprint": "F1"})
        print(f"[missing key] HTTP {s} {b}")
        assert s == 422

        print("\n== 6. 8 路并发同一幂等键（真实 HTTP 并发）==")
        payload = {
            "tenant": "demo", "order_id": "ord-conc", "amount_cents": 700, "currency": "CNY",
            "idempotency_key": "K-HOT", "request_fingerprint": "F-HOT",
        }
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _i: post(base, "/orders", payload), range(8)))
        accepted = [o for o in outcomes if o[0] == 201 and o[2].get("x-idempotent-replay") is None]
        replayed = [o for o in outcomes if o[0] == 201 and o[2].get("x-idempotent-replay") == "true"]
        print(f"    结果: 首次受理 {len(accepted)} 笔，重放 {len(replayed)} 笔，其他 {len(outcomes) - len(accepted) - len(replayed)} 笔")
        assert len(accepted) == 1 and len(replayed) == 7
        s, b = get(base, "/orders/ord-conc", headers={"X-Tenant": "demo"})
        assert s == 200 and b["amount_cents"] == 700

        print("\n== 7. 崩溃恢复：模拟受理事务未提交进程被杀，重启后同键重试 ==")
        # 用独立库 + 独立进程制造"提交前崩溃"，再启动全新服务进程验证恢复。
        crash_db = os.path.join(tempfile.mkdtemp(prefix="idem-crash-"), "crash.sqlite")
        crash_port = 8932
        start_server(crash_db, crash_port).terminate()  # 先建表迁移
        crash_payload = {
            "tenant": "demo", "order_id": "ord-crash", "amount_cents": 424, "currency": "CNY",
            "idempotency_key": "K-CRASH", "request_fingerprint": "F-CRASH",
        }

        ctx = mp.get_context("spawn")
        victim = ctx.Process(target=crash_mid_accept, args=(crash_db,))
        victim.start()
        time.sleep(2)
        victim.terminate()
        victim.join()
        # 全新服务进程 = 重启。
        recovered = start_server(crash_db, crash_port)
        try:
            s, b, h = post(f"http://127.0.0.1:{crash_port}", "/orders", crash_payload)
            show("retry-after-crash", s, b, h)
            assert s == 201 and h.get("x-idempotent-replay") is None  # 与未崩溃时一致：首次受理
            s, b, h = post(f"http://127.0.0.1:{crash_port}", "/orders", crash_payload)
            show("replay-after-recovery", s, b, h)
            assert s == 201 and h.get("x-idempotent-replay") == "true"
        finally:
            recovered.terminate()
            recovered.wait()

        print("\n全部场景断言通过。")
    finally:
        if own_proc is not None:
            own_proc.terminate()
            own_proc.wait()


if __name__ == "__main__":
    main()
