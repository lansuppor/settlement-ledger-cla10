-- Installment payment plans. A plan is a set of rows per (tenant, order_id);
-- each row is one installment identified by term_id within the order, applied
-- in seq order. A plan exists only before any payment is registered, and the
-- sum of amount_cents over a plan always equals the order amount.
CREATE TABLE IF NOT EXISTS payment_plan_items(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  term_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  paid_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, order_id, term_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_plan_items_order ON payment_plan_items(tenant, order_id, seq);
