ALTER TABLE orders ADD COLUMN refunded_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE orders ADD COLUMN pending_refund_cents INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS refunds(
  tenant TEXT NOT NULL,
  refund_request_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  currency TEXT NOT NULL,
  status TEXT NOT NULL,
  rejection_reason TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT,
  rejected_at TEXT,
  PRIMARY KEY(tenant, refund_request_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(tenant, order_id);
