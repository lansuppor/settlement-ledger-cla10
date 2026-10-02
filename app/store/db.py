import sqlite3
from pathlib import Path

from app.config import db_path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))

def _applied_migrations(conn: sqlite3.Connection) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}


def _execute_script_in_transaction(conn: sqlite3.Connection, script: str) -> None:
    # executescript 会隐式提交挂起事务；这里逐条执行以保证整条迁移处于同一事务
    for statement in [s.strip() for s in script.split(";") if s.strip()]:
        conn.execute(statement)


def migrate() -> None:
    conn = connect()
    try:
        applied = _applied_migrations(conn)
        for path in _migration_files():
            if path.name in applied:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                _execute_script_in_transaction(conn, path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations(name) VALUES(?)", (path.name,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()
