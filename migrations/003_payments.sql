-- 收款登记：payment_id 由服务在登记事务内分配（同租户内唯一且稳定不变），不由调用方指定。
-- seq 为租户内单调递增的分配序号，payment_id 为其对外文本形式。
-- status=active 表示仍在抵减未收金额；reversed 表示已被一笔冲正抵回。
CREATE TABLE IF NOT EXISTS payments(
  tenant TEXT NOT NULL,
  payment_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, payment_id),
  UNIQUE(tenant, seq),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

-- 收款冲正记录：reversal_id 唯一范围为（租户, 冲正标识），与 payments 的记录标识相互独立。
-- 同一收款至多一笔生效冲正：UNIQUE(tenant, payment_id) 让并发的第二笔冲正确定失败。
-- request_fingerprint 固定为首次生效时的摘要，重放不改写；response_snapshot 为首次冲正结果。
CREATE TABLE IF NOT EXISTS payment_reversals(
  tenant TEXT NOT NULL,
  reversal_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  payment_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  response_snapshot TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, reversal_id),
  UNIQUE(tenant, payment_id),
  FOREIGN KEY(tenant, payment_id) REFERENCES payments(tenant, payment_id)
);
