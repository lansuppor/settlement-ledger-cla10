-- Refund supplement ("return for additional material") audit trail.
-- A refund in 'accepted' state can be sent back to the customer for a
-- supplement; it then waits in 'awaiting_supplement' and a 'returned' row is
-- written. Once the material arrives, a new amount is submitted; when accepted
-- the refund returns to 'accepted' with its hold recomputed and a
-- 'supplemented' row is written. Replays of the same return (identical reason
-- and note) or of the same accepted supplement do not insert another row.
-- amount_before/after record the hold around the event; on a pure return the
-- hold does not move, so the two are equal.
CREATE TABLE IF NOT EXISTS refund_supplement_events(
  tenant TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('returned','supplemented')),
  reason_code TEXT NOT NULL,
  note TEXT NOT NULL,
  amount_before_cents INTEGER NOT NULL,
  amount_after_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, refund_id, seq),
  FOREIGN KEY(tenant, refund_id) REFERENCES refunds(tenant, refund_id)
);

CREATE INDEX IF NOT EXISTS idx_refund_supplements_refund
  ON refund_supplement_events(tenant, refund_id, seq);
