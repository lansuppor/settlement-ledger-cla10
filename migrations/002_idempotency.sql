-- 幂等受理记录：与订单的 (tenant, order_id) 唯一约束相互独立。
-- request_fingerprint 固定为首次受理时的请求摘要，重放不改写。
-- response_snapshot 固定为首次受理的响应订单对象，保证重放返回与首次完全一致。
CREATE TABLE IF NOT EXISTS accepted_requests(
  tenant TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  order_id TEXT NOT NULL,
  response_snapshot TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, idempotency_key)
);
