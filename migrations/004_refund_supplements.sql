-- Supplement ledger for refunds returned for additional material.
-- A 'returned' row records the return-for-supplement request (amount unchanged);
-- a 'supplemented' row records the accepted supplement (amount_before -> amount_after).
CREATE TABLE IF NOT EXISTS refund_supplements(
  tenant TEXT NOT NULL,
  supplement_id TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  note TEXT NOT NULL,
  amount_before_cents INTEGER NOT NULL,
  amount_after_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, supplement_id),
  FOREIGN KEY(tenant, refund_id) REFERENCES refunds(tenant, refund_id)
);

CREATE INDEX IF NOT EXISTS idx_supplements_refund ON refund_supplements(tenant, refund_id);
