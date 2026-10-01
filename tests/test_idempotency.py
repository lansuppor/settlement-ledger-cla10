import multiprocessing as mp
import os
import tempfile

_DB_DIR = tempfile.mkdtemp()
os.environ["APP_DB"] = os.path.join(_DB_DIR, "test_idempotency.sqlite")

from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders
from app.store.db import connect, migrate

migrate()
client = TestClient(app)


def _body(**overrides) -> dict:
    body = {
        "tenant": "t1",
        "order_id": "i1",
        "amount_cents": 500,
        "currency": "CNY",
        "idempotency_key": "k1",
        "request_fingerprint": "fp1",
    }
    body.update(overrides)
    return body


def _count(table: str, tenant: str) -> int:
    conn = connect()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE tenant=?", (tenant,)).fetchone()[0]
    finally:
        conn.close()


# ---------- HTTP 层：受理 / 重放 / 冲突 / 参数 ----------

def test_replay_returns_first_order_without_changes() -> None:
    first = client.post("/orders", json=_body(order_id="r1", idempotency_key="rk1"))
    assert first.status_code == 201
    assert first.headers.get("X-Idempotent-Replay") is None
    # 首次受理后登记一笔收款，重放必须看到同一订单的真实状态，且不改动它。
    paid = client.post("/orders/r1/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    assert paid.status_code == 200

    replay = client.post("/orders", json=_body(order_id="r1", idempotency_key="rk1"))
    assert replay.status_code == 201
    assert replay.headers.get("X-Idempotent-Replay") == "true"
    assert replay.json() == first.json() | {"paid_cents": 200, "outstanding_cents": 300}
    # 没有产生第二张单、第二条指纹记录。
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE tenant='t1' AND order_id='r1'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys WHERE idempotency_key='rk1'").fetchone()[0] == 1
        assert conn.execute("SELECT paid_cents FROM orders WHERE order_id='r1'").fetchone()[0] == 200
    finally:
        conn.close()


def test_fingerprint_conflict_is_distinct_and_does_not_overwrite() -> None:
    client.post("/orders", json=_body(order_id="c1", amount_cents=500, currency="CNY",
                                      idempotency_key="ck1", request_fingerprint="fpA"))
    # 同一幂等键被用于完全不同的业务内容（不同订单标识/金额/币种/指纹）。
    clash = client.post("/orders", json=_body(order_id="c2", amount_cents=999, currency="USD",
                                              idempotency_key="ck1", request_fingerprint="fpB"))
    assert clash.status_code == 409
    assert clash.json()["detail"] == "idempotency key reused with different request fingerprint"

    # 首次受理的订单未被覆盖。
    got = client.get("/orders/c1", headers={"X-Tenant": "t1"}).json()
    assert got["amount_cents"] == 500 and got["currency"] == "CNY"
    # 冲突请求的订单标识不存在，且仍只有一条指纹记录。
    assert client.get("/orders/c2", headers={"X-Tenant": "t1"}).status_code == 404
    conn = connect()
    try:
        rows = conn.execute("SELECT request_fingerprint, order_id FROM idempotency_keys WHERE idempotency_key='ck1'").fetchall()
        assert len(rows) == 1 and rows[0]["request_fingerprint"] == "fpA" and rows[0]["order_id"] == "c1"
    finally:
        conn.close()


def test_order_id_conflict_is_distinct_from_key_conflict() -> None:
    client.post("/orders", json=_body(order_id="d1", idempotency_key="dk1"))
    # 不同幂等键、同一订单标识：订单标识唯一冲突，错误信息必须与幂等键冲突可区分。
    resp = client.post("/orders", json=_body(order_id="d1", idempotency_key="dk2"))
    assert resp.status_code == 409 and resp.json()["detail"] == "order already accepted"


def test_missing_key_or_fingerprint_is_rejected_without_persistence() -> None:
    assert client.post("/orders", json=_body(order_id="m1", idempotency_key=None)).status_code == 422
    assert client.post("/orders", json=_body(order_id="m2", request_fingerprint="")).status_code == 422
    # m1/m2 不得落任何数据。
    assert client.get("/orders/m1", headers={"X-Tenant": "t1"}).status_code == 404
    assert client.get("/orders/m2", headers={"X-Tenant": "t1"}).status_code == 404


def test_same_key_is_independent_across_tenants() -> None:
    a = client.post("/orders", json=_body(tenant="ta", order_id="x1", idempotency_key="shared", request_fingerprint="f"))
    b = client.post("/orders", json=_body(tenant="tb", order_id="x1", idempotency_key="shared", request_fingerprint="f"))
    assert a.status_code == 201 and b.status_code == 201
    assert _count("idempotency_keys", "ta") == 1 and _count("idempotency_keys", "tb") == 1


# ---------- 多进程并发与崩溃恢复（真实独立进程打同一个 SQLite 文件） ----------

def _worker_accept(db: str, payload: dict, out: mp.Queue) -> None:
    os.environ["APP_DB"] = db
    from app.store import orders as store
    try:
        order, replayed = store.accept_order(**payload)
        out.put(("ok", replayed, order["order_id"]))
    except store.OrderAlreadyAccepted:
        out.put(("dup", None, None))
    except store.IdempotencyConflict:
        out.put(("conflict", None, None))


def _spawn_acceptors(db: str, n: int, payload: dict) -> list:
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    procs = [ctx.Process(target=_worker_accept, args=(db, payload, q)) for _ in range(n)]
    for p in procs:
        p.start()
    results = [q.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=10)
    return results


def test_concurrent_same_key_same_fingerprint_single_acceptance() -> None:
    db = os.path.join(_DB_DIR, "concurrent_replay.sqlite")
    os.environ["APP_DB"] = db
    migrate()
    payload = {"tenant": "tc", "order_id": "cc1", "amount_cents": 100, "currency": "CNY",
               "idempotency_key": "hot", "request_fingerprint": "same"}
    results = _spawn_acceptors(db, 8, payload)
    firsts = [r for r in results if r[0] == "ok" and r[1] is False]
    replays = [r for r in results if r[0] == "ok" and r[1] is True]
    assert len(firsts) == 1 and len(replays) == 7
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE tenant='tc'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys WHERE tenant='tc'").fetchone()[0] == 1
    finally:
        conn.close()


def test_concurrent_same_key_different_fingerprints_single_winner() -> None:
    db = os.path.join(_DB_DIR, "concurrent_conflict.sqlite")
    os.environ["APP_DB"] = db
    migrate()
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    procs = []
    for i in range(8):
        payload = {"tenant": "td", "order_id": f"cd{i}", "amount_cents": 100 + i, "currency": "CNY",
                   "idempotency_key": "hot2", "request_fingerprint": f"fp{i}"}
        procs.append(ctx.Process(target=_worker_accept, args=(db, payload, q)))
    for p in procs:
        p.start()
    results = [q.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=10)
    assert len([r for r in results if r[0] == "ok"]) == 1
    assert len([r for r in results if r[0] == "conflict"]) == 7
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE tenant='td'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys WHERE tenant='td'").fetchone()[0] == 1
    finally:
        conn.close()


def _crashed_mid_accept(db: str, payload: dict, started: mp.Event) -> None:
    os.environ["APP_DB"] = db
    from app.store.db import connect
    # 模拟受理过程中崩溃：两行都已写入但事务尚未提交，进程随即被杀。
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) VALUES(?,?,?,0,?,'accepted')",
        (payload["tenant"], payload["order_id"], payload["amount_cents"], payload["currency"]),
    )
    conn.execute(
        "INSERT INTO idempotency_keys(tenant, idempotency_key, request_fingerprint, order_id) VALUES(?,?,?,?)",
        (payload["tenant"], payload["idempotency_key"], payload["request_fingerprint"], payload["order_id"]),
    )
    started.set()
    import time
    time.sleep(30)  # 等待父进程杀死自己，绝不 COMMIT


def test_crash_before_commit_then_retry_succeeds_as_first_acceptance() -> None:
    db = os.path.join(_DB_DIR, "crash.sqlite")
    os.environ["APP_DB"] = db
    migrate()
    payload = {"tenant": "te", "order_id": "ce1", "amount_cents": 100, "currency": "CNY",
               "idempotency_key": "durable", "request_fingerprint": "fp"}
    ctx = mp.get_context("spawn")
    started = ctx.Event()
    victim = ctx.Process(target=_crashed_mid_accept, args=(db, payload, started))
    victim.start()
    assert started.wait(timeout=10)
    victim.terminate()  # 模拟崩溃：未提交事务随进程死亡
    victim.join(timeout=10)

    # 崩溃恢复后不存在任何半笔数据。
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE tenant='te'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys WHERE tenant='te'").fetchone()[0] == 0
    finally:
        conn.close()

    # 同一幂等键重试：按“首次受理”成功，而不是误判为重放或冲突。
    order, replayed = orders.accept_order(**payload)
    assert replayed is False and order["order_id"] == "ce1"
    # 再一次重放结论确定。
    order2, replayed2 = orders.accept_order(**payload)
    assert replayed2 is True and order2 == order
