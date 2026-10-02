-- Payment (installment) plans. A plan may exist only while an order has never
-- received any money; canceling a plan (when no money was received) returns the
-- order to the no-plan state. Payments fill installments in order; each
-- installment accumulates up to its own amount and is then settled.
CREATE TABLE IF NOT EXISTS payment_plans(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, order_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

CREATE TABLE IF NOT EXISTS payment_plan_installments(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  install_seq INTEGER NOT NULL,      -- 1-based position of the period
  install_label TEXT NOT NULL,       -- client-provided period identifier, unique per order
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  paid_cents INTEGER NOT NULL DEFAULT 0 CHECK (paid_cents >= 0 AND paid_cents <= amount_cents),
  PRIMARY KEY(tenant, order_id, install_seq),
  UNIQUE(tenant, order_id, install_label),
  FOREIGN KEY(tenant, order_id) REFERENCES payment_plans(tenant, order_id)
);
