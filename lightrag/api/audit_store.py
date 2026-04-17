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

import asyncio
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

from lightrag.api.db import get_db_backend, get_engine, get_sqlite_path


AuditOutcome = Literal["success", "denied", "error"]


@dataclass(slots=True)
class AuditEventRow:
    """A single audit_log row ready to be persisted."""

    action: str
    resource_type: str
    outcome: AuditOutcome
    occurred_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    id: str = field(default_factory=lambda: uuid4().hex)
    actor_user_id: str | None = None
    actor_username: str | None = None
    actor_role: str | None = None
    workspace_id: str | None = None
    kb_id: str | None = None
    resource_id: str | None = None
    http_method: str | None = None
    http_path: str | None = None
    status_code: int | None = None
    client_ip: str | None = None
    user_agent: str | None = None
    metadata: dict[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        metadata = row.pop("metadata", None)
        row["metadata"] = (
            json.dumps(metadata, ensure_ascii=False, default=str)
            if metadata is not None
            else None
        )
        return row

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


# ---------------------------------------------------------------------------
# PR-AUDIT-2: insert path (dual backend, best-effort)
# ---------------------------------------------------------------------------


_INSERT_COLUMNS: tuple[str, ...] = (
    "id",
    "occurred_at",
    "actor_user_id",
    "actor_username",
    "actor_role",
    "workspace_id",
    "kb_id",
    "action",
    "resource_type",
    "resource_id",
    "outcome",
    "http_method",
    "http_path",
    "status_code",
    "client_ip",
    "user_agent",
    "metadata",
)


def _insert_audit_row_sqlite(path: str, row: dict[str, Any]) -> None:
    placeholders = ", ".join(["?"] * len(_INSERT_COLUMNS))
    columns = ", ".join(_INSERT_COLUMNS)
    with sqlite3.connect(path) as conn:
        conn.execute(
            f"INSERT INTO {AUDIT_LOG_TABLE_NAME} ({columns}) VALUES ({placeholders})",
            tuple(row[col] for col in _INSERT_COLUMNS),
        )
        conn.commit()


async def _insert_audit_row_sqlalchemy(row: dict[str, Any]) -> None:
    from sqlalchemy import text

    engine = get_engine()
    if engine is None:
        raise RuntimeError("SQLAlchemy engine is not initialised")

    columns = ", ".join(_INSERT_COLUMNS)
    placeholders = ", ".join(f":{col}" for col in _INSERT_COLUMNS)
    stmt = text(
        f"INSERT INTO {AUDIT_LOG_TABLE_NAME} ({columns}) VALUES ({placeholders})"
    )
    async with engine.begin() as conn:
        await conn.execute(stmt, row)


async def insert_audit_event(event: AuditEventRow) -> bool:
    """
    Best-effort persist of one audit event.

    Returns True when the row was written, False when the backend is
    not ready or the audit_log table does not exist yet (the caller
    should not crash just because audit logging failed).
    """
    backend = get_db_backend()
    if backend is None:
        return False

    row = event.to_row()

    try:
        if backend == "sqlalchemy":
            await _insert_audit_row_sqlalchemy(row)
            return True
        if backend == "sqlite3":
            sqlite_path = get_sqlite_path()
            if sqlite_path is None:
                return False
            await asyncio.to_thread(_insert_audit_row_sqlite, sqlite_path, row)
            return True
    except Exception:
        # Audit writes must not fail the user request. Callers log the
        # failure at warning level; here we just swallow.
        return False

    return False


# ---------------------------------------------------------------------------
# PR-AUDIT-3: read path
# ---------------------------------------------------------------------------


_SELECT_COLUMNS = ", ".join(_INSERT_COLUMNS)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a DB row (sqlite3.Row / SQLAlchemy mapping) to a plain dict."""
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    if isinstance(row, dict):
        return dict(row)
    # Fallback: iterate positional values.
    return {col: value for col, value in zip(_INSERT_COLUMNS, row)}


def _hydrate_metadata(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("metadata")
    if raw is None or raw == "":
        event["metadata"] = None
        return event
    try:
        event["metadata"] = json.loads(raw)
    except (TypeError, ValueError):
        # Preserve the raw string so callers can surface corruption.
        event["metadata"] = {"_raw": raw}
    return event


async def list_audit_events(
    *,
    workspace_id: str | None = None,
    actor_user_id: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """
    Return up to ``limit`` audit events matching the provided filters,
    newest first. ``metadata`` is hydrated from its stored JSON string.
    """
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    filters: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if workspace_id is not None:
        filters.append("workspace_id = :workspace_id")
        params["workspace_id"] = workspace_id
    if actor_user_id is not None:
        filters.append("actor_user_id = :actor_user_id")
        params["actor_user_id"] = actor_user_id
    if action is not None:
        filters.append("action = :action")
        params["action"] = action
    if outcome is not None:
        filters.append("outcome = :outcome")
        params["outcome"] = outcome
    if since is not None:
        filters.append("occurred_at >= :since")
        params["since"] = since
    if until is not None:
        filters.append("occurred_at <= :until")
        params["until"] = until

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    sql = (
        f"SELECT {_SELECT_COLUMNS} FROM {AUDIT_LOG_TABLE_NAME} "
        f"{where_clause} "
        f"ORDER BY occurred_at DESC, id DESC "
        f"LIMIT :limit OFFSET :offset"
    )

    backend = get_db_backend()
    if backend is None:
        return []

    try:
        if backend == "sqlalchemy":
            from sqlalchemy import text

            engine = get_engine()
            if engine is None:
                return []
            async with engine.connect() as conn:
                result = await conn.execute(text(sql), params)
                rows = [_row_to_dict(r) for r in result.mappings().all()]

        elif backend == "sqlite3":
            sqlite_path = get_sqlite_path()
            if sqlite_path is None:
                return []

            # sqlite3 uses qmark/named placeholders; translate :name to ?
            # manually because we want a predictable sync path.
            def _sqlite_read() -> list[dict[str, Any]]:
                placeholders_order: list[str] = []
                sqlite_sql = sql
                for key in [
                    "workspace_id",
                    "actor_user_id",
                    "action",
                    "outcome",
                    "since",
                    "until",
                    "limit",
                    "offset",
                ]:
                    token = f":{key}"
                    if token in sqlite_sql and key in params:
                        sqlite_sql = sqlite_sql.replace(token, "?", 1)
                        placeholders_order.append(key)
                values = tuple(params[key] for key in placeholders_order)
                with sqlite3.connect(sqlite_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.execute(sqlite_sql, values)
                    return [_row_to_dict(r) for r in cursor.fetchall()]

            rows = await asyncio.to_thread(_sqlite_read)
        else:
            return []
    except Exception:
        return []

    return [_hydrate_metadata(event) for event in rows]


async def get_audit_event_by_id(event_id: str) -> dict[str, Any] | None:
    sql = (
        f"SELECT {_SELECT_COLUMNS} FROM {AUDIT_LOG_TABLE_NAME} "
        f"WHERE id = :id LIMIT 1"
    )
    backend = get_db_backend()
    if backend is None:
        return None

    try:
        if backend == "sqlalchemy":
            from sqlalchemy import text

            engine = get_engine()
            if engine is None:
                return None
            async with engine.connect() as conn:
                result = await conn.execute(text(sql), {"id": event_id})
                row = result.mappings().first()
            if row is None:
                return None
            return _hydrate_metadata(_row_to_dict(row))

        if backend == "sqlite3":
            sqlite_path = get_sqlite_path()
            if sqlite_path is None:
                return None

            def _sqlite_read_one() -> dict[str, Any] | None:
                sqlite_sql = sql.replace(":id", "?")
                with sqlite3.connect(sqlite_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.execute(sqlite_sql, (event_id,))
                    row = cursor.fetchone()
                return _row_to_dict(row) if row is not None else None

            row = await asyncio.to_thread(_sqlite_read_one)
            return _hydrate_metadata(row) if row is not None else None
    except Exception:
        return None

    return None


async def delete_audit_events_before(
    workspace_id: str, *, before: str
) -> int:
    """Retention pruning: delete events before the given ISO timestamp."""
    sql = (
        f"DELETE FROM {AUDIT_LOG_TABLE_NAME} "
        f"WHERE workspace_id = :workspace_id AND occurred_at < :before"
    )
    backend = get_db_backend()
    if backend is None:
        return 0

    try:
        if backend == "sqlalchemy":
            from sqlalchemy import text

            engine = get_engine()
            if engine is None:
                return 0
            async with engine.begin() as conn:
                result = await conn.execute(
                    text(sql), {"workspace_id": workspace_id, "before": before}
                )
                return int(result.rowcount or 0)

        if backend == "sqlite3":
            sqlite_path = get_sqlite_path()
            if sqlite_path is None:
                return 0

            def _sqlite_delete() -> int:
                sqlite_sql = sql.replace(":workspace_id", "?").replace(":before", "?")
                with sqlite3.connect(sqlite_path) as conn:
                    cursor = conn.execute(sqlite_sql, (workspace_id, before))
                    conn.commit()
                    return int(cursor.rowcount or 0)

            return await asyncio.to_thread(_sqlite_delete)
    except Exception:
        return 0

    return 0
