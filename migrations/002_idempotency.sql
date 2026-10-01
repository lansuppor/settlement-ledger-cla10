CREATE TABLE IF NOT EXISTS idempotency_keys(
  tenant TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  order_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(tenant, idempotency_key),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);
