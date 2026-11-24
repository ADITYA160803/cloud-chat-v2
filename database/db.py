import sqlite3
from pathlib import Path
from typing import Optional, Union
from config import config
from core.exceptions import DatabaseError

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS deployments (
    deployment_id TEXT PRIMARY KEY,
    user_request TEXT NOT NULL,
    service TEXT NOT NULL DEFAULT 'ec2',
    status TEXT NOT NULL DEFAULT 'draft',
    approval_status TEXT NOT NULL DEFAULT 'pending',
    plan_hash TEXT,
    parsed_intent JSON,
    normalized_spec JSON,
    plan_summary JSON,
    aws_resource_ids JSON,
    verification_status TEXT NOT NULL DEFAULT 'NOT_CHECKED',
    verification_details JSON,
    error_message TEXT,
    aws_account_id TEXT,
    aws_identity_arn TEXT,
    aws_region TEXT,
    execution_mode TEXT DEFAULT 'mock',
    credential_source_type TEXT,
    approved_at TIMESTAMP,
    execution_started_at TIMESTAMP,
    execution_completed_at TIMESTAMP,
    terraform_exit_code INTEGER,
    execution_logs TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_deployments_created ON deployments(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deployments_status ON deployments(status);

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    deployment_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_created ON chat_messages(created_at ASC);

CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def get_db_connection(db_path: Optional[Union[Path, str]] = None) -> sqlite3.Connection:
    """Creates a connection to SQLite with WAL mode, foreign keys, and dictionary row factory."""
    target_path = Path(db_path) if db_path else config.db_path
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target_path), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn
    except Exception as e:
        raise DatabaseError(f"Failed to connect to SQLite at {target_path}: {e}") from e


def init_db(db_path: Optional[Path] = None) -> None:
    """Initializes the database schema if tables do not exist and applies migrations."""
    conn = get_db_connection(db_path)
    try:
        with conn:
            conn.executescript(SCHEMA_SQL)
            _apply_migrations(conn)
    except Exception as e:
        raise DatabaseError(f"Failed to initialize database schema: {e}") from e
    finally:
        conn.close()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Safely adds missing columns to existing SQLite databases."""
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(deployments);")
    existing_cols = {row["name"] for row in cursor.fetchall()}

    columns_to_add = [
        ("aws_account_id", "TEXT"),
        ("aws_identity_arn", "TEXT"),
        ("aws_region", "TEXT"),
        ("execution_mode", "TEXT DEFAULT 'mock'"),
        ("credential_source_type", "TEXT"),
        ("approved_at", "TIMESTAMP"),
        ("execution_started_at", "TIMESTAMP"),
        ("execution_completed_at", "TIMESTAMP"),
        ("terraform_exit_code", "INTEGER"),
        ("execution_logs", "TEXT"),
    ]

    for col_name, col_type in columns_to_add:
        if col_name not in existing_cols:
            cursor.execute(f"ALTER TABLE deployments ADD COLUMN {col_name} {col_type};")

    # Ensure chat_messages table exists in migrated databases
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            message_id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            deployment_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_created ON chat_messages(created_at ASC);")

    # Ensure system_settings table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Safety cleanup: Revoke invalid legacy approvals that have empty/null plan_hash
    cursor.execute("""
        UPDATE deployments
        SET approval_status = 'revoked',
            approved_at = NULL,
            plan_hash = NULL,
            error_message = 'Approval revoked: Previous plan hash was empty or invalid. A fresh Terraform plan is required.'
        WHERE approval_status IN ('approved', 'revoked') AND (plan_hash IS NULL OR trim(plan_hash) = '');
    """)
