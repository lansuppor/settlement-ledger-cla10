-- 分期收款记录：独立于 payments / accepted_requests 的标识空间，不影响整单收款入口。
--   installment_id 由服务分配，在（租户）内唯一且稳定不变，不由调用方指定；
--   installment_key 为调用方携带的该笔分期业务标识，(tenant, installment_key) 只允许生效一次，
--     与订单标识 (tenant, order_id)、整单收款记录标识 (tenant, payment_id) 的唯一性相互独立。
--   request_fingerprint / response_snapshot 固定为首次登记时的内容与结果快照，重放绝不改写。
-- status: active（有效）| reversed（已冲正）；冲正只改状态，不删除分期行。
CREATE TABLE IF NOT EXISTS installments(
  tenant TEXT NOT NULL,
  installment_id TEXT NOT NULL,
  installment_key TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  request_fingerprint TEXT NOT NULL,
  response_snapshot TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, installment_id),
  UNIQUE(tenant, installment_key),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

-- 分期冲正记录（与整单收款的 payment_reversals 相互独立）：
--   唯一约束一（主约束）：(tenant, reversal_id) —— 同一（租户, 冲正标识）只允许生效一次，
--     命中后按 request_fingerprint 判定重放（同指纹返回首次快照）或指纹冲突（不同指纹拒绝）。
--   唯一约束二：(tenant, installment_id) —— 一笔分期至多被冲正一次，
--     新冲正标识命中已冲正分期时按“已处理”拒绝。两类冲突在代码中明确区分，不归为同一错误。
-- request_fingerprint / response_snapshot 固定为首次生效时的内容，重放绝不改写。
CREATE TABLE IF NOT EXISTS installment_reversals(
  tenant TEXT NOT NULL,
  reversal_id TEXT NOT NULL,
  installment_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  response_snapshot TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, reversal_id),
  UNIQUE(tenant, installment_id),
  FOREIGN KEY(tenant, installment_id) REFERENCES installments(tenant, installment_id)
);
