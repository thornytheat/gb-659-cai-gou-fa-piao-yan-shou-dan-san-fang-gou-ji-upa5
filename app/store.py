import sqlite3
from contextlib import contextmanager
from app.config import DATABASE_PATH

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, accepted INTEGER NOT NULL, quarantined INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS ticket_candidates (
        ticket_key TEXT NOT NULL,
        digest TEXT NOT NULL,
        content_json TEXT NOT NULL,
        sources_json TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        seen_count INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (ticket_key, digest))""",
    """CREATE TABLE IF NOT EXISTS ticket_state (
        ticket_key TEXT PRIMARY KEY,
        version INTEGER NOT NULL,
        status TEXT NOT NULL,
        effective_digest TEXT)""",
    """CREATE TABLE IF NOT EXISTS adjudications (
        adjudication_id TEXT PRIMARY KEY,
        ticket_key TEXT NOT NULL,
        digest TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        reason_text TEXT NOT NULL DEFAULT '',
        base_version INTEGER NOT NULL,
        outcome TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        posted_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS voucher_ledger (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_key TEXT NOT NULL,
        digest TEXT NOT NULL,
        kind TEXT NOT NULL,
        adjudication_id TEXT,
        content_json TEXT NOT NULL,
        posted_at TEXT NOT NULL)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS voucher_ledger_adjudication ON voucher_ledger(adjudication_id) WHERE adjudication_id IS NOT NULL",
)

def _database_file():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return DATABASE_PATH

def connect():
    return sqlite3.connect(_database_file())

@contextmanager
def immediate():
    """BEGIN IMMEDIATE 事务：并发写串行化，检查与写入保持原子。"""
    db = sqlite3.connect(_database_file(), isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

@contextmanager
def reading():
    db = connect()
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()

def init_db():
    with connect() as db:
        for statement in SCHEMA:
            db.execute(statement)

def record_run(manifest: dict) -> bool:
    init_db()
    with connect() as db:
        before = db.total_changes
        db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?, ?)", (manifest["run_id"], manifest["input_sha256"], manifest["accepted"], manifest["quarantined"]))
        return db.total_changes > before

def run_count() -> int:
    init_db()
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
