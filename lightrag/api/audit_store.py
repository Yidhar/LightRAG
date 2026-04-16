"""
Audit-log persistence primitives.

PR-AUDIT-1 lands the schema and idempotent DDL only. Emission and query
paths are introduced in PR-AUDIT-2 and PR-AUDIT-3 respectively, at which
point this module will grow a write helper and a paged reader.

The DDL is written to work against both raw sqlite3 and SQLAlchemy
engines. Every statement uses IF NOT EXISTS so that re-running the
migration is safe.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

AUDIT_LOG_TABLE_NAME = "audit_log"

_AUDIT_LOG_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {AUDIT_LOG_TABLE_NAME} (
    id TEXT NOT NULL PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    actor_user_id TEXT NULL,
    actor_username TEXT NULL,
    actor_role TEXT NULL,
    workspace_id TEXT NULL,
    kb_id TEXT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NULL,
    outcome TEXT NOT NULL,
    http_method TEXT NULL,
    http_path TEXT NULL,
    status_code INTEGER NULL,
    client_ip TEXT NULL,
    user_agent TEXT NULL,
    metadata TEXT NULL
)
""".strip()

_AUDIT_LOG_INDEX_SQLS: tuple[str, ...] = (
    f"CREATE INDEX IF NOT EXISTS idx_{AUDIT_LOG_TABLE_NAME}_workspace_time "
    f"ON {AUDIT_LOG_TABLE_NAME} (workspace_id, occurred_at DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_{AUDIT_LOG_TABLE_NAME}_actor_time "
    f"ON {AUDIT_LOG_TABLE_NAME} (actor_user_id, occurred_at DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_{AUDIT_LOG_TABLE_NAME}_action_time "
    f"ON {AUDIT_LOG_TABLE_NAME} (action, occurred_at DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_{AUDIT_LOG_TABLE_NAME}_resource "
    f"ON {AUDIT_LOG_TABLE_NAME} (resource_type, resource_id, occurred_at DESC)",
)


def audit_schema_ddl() -> tuple[str, ...]:
    """Return the ordered DDL statements that create the audit_log schema."""
    return (_AUDIT_LOG_CREATE_SQL, *_AUDIT_LOG_INDEX_SQLS)


def apply_audit_schema_sqlite(path: str) -> None:
    """Apply the audit_log schema against a raw SQLite database file."""
    with sqlite3.connect(path) as conn:
        for statement in audit_schema_ddl():
            conn.execute(statement)
        conn.commit()


async def apply_audit_schema_sqlalchemy(engine: "AsyncEngine") -> None:
    """Apply the audit_log schema through a SQLAlchemy async engine."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        for statement in audit_schema_ddl():
            await conn.execute(text(statement))


async def apply_audit_schema(engine_or_path: Any) -> None:
    """
    Apply the audit_log schema against whatever backend the caller has.

    Accepts either a SQLAlchemy ``AsyncEngine`` or a plain SQLite path (``str``).
    Keeps PR-AUDIT-2 callers free from knowing which backend was selected by
    ``lightrag.api.db``.
    """
    if isinstance(engine_or_path, str):
        apply_audit_schema_sqlite(engine_or_path)
        return

    await apply_audit_schema_sqlalchemy(engine_or_path)
