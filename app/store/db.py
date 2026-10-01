import sqlite3
from pathlib import Path

from app.config import db_path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
SCHEMA_FILES = ["001_init.sql", "002_idempotency.sql"]

def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发写时排队等待写锁，而不是立刻抛 database is locked
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def migrate() -> None:
    conn = connect()
    try:
        for name in SCHEMA_FILES:
            conn.executescript((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
    finally:
        conn.close()
