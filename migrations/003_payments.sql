-- 收款记录：payment_id 由服务分配，在（租户）内唯一且稳定不变，与订单标识、幂等键相互独立。
-- status: active（有效）| reversed（已冲正）；冲正只改状态，不删除收款行。
CREATE TABLE IF NOT EXISTS payments(
  tenant TEXT NOT NULL,
  payment_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, payment_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

-- 收款冲正记录：
--   唯一约束一（主约束）：(tenant, reversal_id) —— 同一（租户, 冲正标识）只允许生效一次，
--     命中后按 request_fingerprint 判定重放（同指纹返回首次快照）或指纹冲突（不同指纹拒绝）。
--   唯一约束二：(tenant, payment_id) —— 一笔收款至多被冲正一次，
--     新冲正标识命中已冲正收款时按“已处理”拒绝。两类冲突在代码中明确区分，不归为同一错误。
-- request_fingerprint / response_snapshot 固定为首次生效时的内容，重放绝不改写。
CREATE TABLE IF NOT EXISTS payment_reversals(
  tenant TEXT NOT NULL,
  reversal_id TEXT NOT NULL,
  payment_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  response_snapshot TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, reversal_id),
  UNIQUE(tenant, payment_id),
  FOREIGN KEY(tenant, payment_id) REFERENCES payments(tenant, payment_id)
);
