-- 批量受理（批次导入）记录：
--   唯一约束：(tenant, batch_id) —— 同一（租户, 批次标识）只允许一批真正导入，
--     命中后按 request_fingerprint 判定整批重放（同指纹返回首次结果清单）或批次指纹冲突（不同指纹拒绝）。
--     该约束与行级幂等键 (tenant, idempotency_key)、订单标识 (tenant, order_id) 的唯一性相互独立，
--     冲突在代码中明确区分，不归为同一类错误。
-- status: processing（已登记、尚有行未处理，可续跑）| completed（全部行已定论，结果清单已固定）。
-- request_fingerprint / response_snapshot 固定为首次导入时的内容，重放与续跑均不改写已完成批次的快照。
CREATE TABLE IF NOT EXISTS import_batches(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL,
  response_snapshot TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, batch_id)
);

-- 批次行结果：每行结论随该行的订单与行级幂等记录在同一事务内原子提交。
-- 中断后已提交行保持已受理，未处理行没有任何记录（不留半笔数据）；
-- 以同一（租户, 批次标识）与同一指纹重新提交时，已定论行直接取用此处保存的首次结论。
CREATE TABLE IF NOT EXISTS import_batch_rows(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  row_no INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  result_snapshot TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, batch_id, row_no),
  FOREIGN KEY(tenant, batch_id) REFERENCES import_batches(tenant, batch_id)
);
