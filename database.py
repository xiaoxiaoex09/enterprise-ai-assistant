
"""SQLite storage for the existing API and the incremental knowledge-base MVP."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATABASE_NAME = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "tickets.db")))


@contextmanager
def get_connection():
    """Return a transaction-scoped connection with foreign keys enabled."""
    conn = sqlite3.connect(str(DATABASE_NAME), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _init_legacy_schema(conn: sqlite3.Connection) -> None:
    """Keep existing tickets and PDF endpoints compatible during the refactor."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            device_name TEXT NOT NULL,
            problem TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '待处理',
            ai_analysis TEXT,
            priority TEXT,
            category TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            page INTEGER NOT NULL,
            content TEXT NOT NULL,
            embedding TEXT NOT NULL,
            model TEXT NOT NULL,
            doc_type TEXT
        )
    """)

    ticket_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(tickets)")
    }
    for name in ("ai_analysis", "priority", "category"):
        if name not in ticket_columns:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} TEXT")

    chunk_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(knowledge_chunks)")
    }
    if "doc_type" not in chunk_columns:
        conn.execute("ALTER TABLE knowledge_chunks ADD COLUMN doc_type TEXT")


def _init_product_schema(conn: sqlite3.Connection) -> None:
    """Create isolated, versioned document storage for the pilot workflow."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS organizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pilot_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER NOT NULL REFERENCES organizations(id),
            username TEXT NOT NULL COLLATE NOCASE,
            role TEXT NOT NULL CHECK (role IN ('admin', 'engineer')),
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (organization_id, username)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pilot_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES pilot_users(id) ON DELETE CASCADE,
            expires_at INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS pilot_sessions_expiry ON pilot_sessions(expires_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pilot_login_failures (
            organization_name TEXT NOT NULL,
            username TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            locked_until INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (organization_name, username)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS device_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER NOT NULL REFERENCES organizations(id),
            model_code TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (organization_id, model_code),
            UNIQUE (id, organization_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER NOT NULL REFERENCES organizations(id),
            family_key TEXT NOT NULL,
            title TEXT NOT NULL,
            document_type TEXT NOT NULL CHECK (document_type IN ('manual', 'case')),
            version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'processing'
                CHECK (status IN ('processing', 'review', 'published', 'superseded', 'failed')),
            storage_key TEXT,
            sha256 TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            published_at TEXT,
            UNIQUE (organization_id, family_key, version),
            UNIQUE (id, organization_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS document_models (
            organization_id INTEGER NOT NULL REFERENCES organizations(id),
            document_id INTEGER NOT NULL,
            device_model_id INTEGER NOT NULL,
            PRIMARY KEY (document_id, device_model_id),
            FOREIGN KEY (document_id, organization_id)
                REFERENCES documents(id, organization_id),
            FOREIGN KEY (device_model_id, organization_id)
                REFERENCES device_models(id, organization_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS document_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
            page INTEGER NOT NULL CHECK (page > 0),
            section TEXT,
            content TEXT NOT NULL,
            embedding TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            UNIQUE (document_id, chunk_index)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingestion_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
            error_message TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            finished_at TEXT,
            worker_id TEXT,
            lease_token TEXT,
            completed_lease_token TEXT,
            lease_expires_at INTEGER,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0)
        )
    """)
    job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(ingestion_jobs)")}
    for name, definition in (
        ("worker_id", "TEXT"),
        ("lease_token", "TEXT"),
        ("completed_lease_token", "TEXT"),
        ("lease_expires_at", "INTEGER"),
        ("attempt_count", "INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0)"),
    ):
        if name not in job_columns:
            conn.execute(f"ALTER TABLE ingestion_jobs ADD COLUMN {name} {definition}")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS one_published_version_per_family
        ON documents(organization_id, family_key)
        WHERE status = 'published'
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS document_models_by_model
        ON document_models(organization_id, device_model_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS ingestion_jobs_by_status
        ON ingestion_jobs(status, created_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS ingestion_jobs_by_lease
        ON ingestion_jobs(status, lease_expires_at)
    """)


def init_db() -> None:
    with get_connection() as conn:
        # The API and ingestion worker share one local SQLite file.
        journal_mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if journal_mode.lower() != "wal":
            raise RuntimeError(f"SQLite WAL mode could not be enabled: {journal_mode}")
        _init_legacy_schema(conn)
        _init_product_schema(conn)


def migrate_database() -> None:
    init_db()
