-- 批量受理：批次标识以（租户, 批次标识）唯一，与每行的（租户, 幂等键）、
-- （租户, 订单标识）唯一性相互独立；两类冲突在代码中按批次指纹冲突 /
-- 行级指纹冲突 / 订单重复分别给出确定结论，不归为同一类错误。
--
-- 批次按指纹判定重复：
--   - 同批次标识同指纹视为重放，不补做任何一行，返回首次导入的结果清单。
--   - 同批次标识不同指纹视为标识被复用于不同业务内容，必须拒绝，
--     且不得覆盖首次导入的任何订单、行结果与批次记录。
--
-- 可续跑：状态字段区分“导入进行中（可能中断）”与“已完成”。
--   进程在逐行导入中崩溃后，同（租户, 批次标识）同指纹重新提交时：
--   已落库的行结论固定不变、按重放返回；只补做尚未受理的行（line_no 缺失即未处理）。
-- 逐行原子提交：每行的订单、幂等记录与行结论在独立事务内落库；
--   崩溃时已提交的行保持已受理，未处理的行不留半笔数据。

-- status: running（导入中，可续跑）| completed（全部行已落结论）
-- total_lines: 本次请求的总行数；完成时校验落库行数闭合。
CREATE TABLE IF NOT EXISTS order_batches(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  total_lines INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, batch_id)
);

-- 每行结论（成功 / 重放 / 各拒绝原因），按 line_no 定位。
-- outcome:
--   accepted        本行首次受理成功（订单与幂等记录随本行事务一起落库）
--   replayed        行幂等键此前已消费且指纹一致，返回首次受理快照（不重复受理）
--   rejected_invalid      行内缺幂等键或指纹（含空串），参数不合法
--   rejected_fingerprint  行幂等键被用于不同指纹（指纹冲突，422 类）
--   rejected_duplicate    订单标识重复（409 类）
-- result_snapshot 固定为首次处理该行时的结果（订单对象或拒绝说明），重放/续跑原样返回。
CREATE TABLE IF NOT EXISTS batch_order_lines(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  outcome VARCHAR(31) NOT NULL,
  result_snapshot TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, batch_id, line_no),
  FOREIGN KEY(tenant, batch_id) REFERENCES order_batches(tenant, batch_id)
);
