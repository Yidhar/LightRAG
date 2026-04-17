"""
Offline tests for the Phase AUDIT-2 insert path.

Covers ``AuditEventRow`` serialisation and the sqlite3 branch of
``insert_audit_event``. The sqlalchemy branch and the emit-through-
FastAPI-Request path need integration fixtures and are verified in
higher-level tests.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lightrag.api import db as db_module
from lightrag.api.audit_store import (
    AUDIT_LOG_TABLE_NAME,
    AuditEventRow,
    apply_audit_schema_sqlite,
    insert_audit_event,
)

pytestmark = pytest.mark.offline


@pytest.fixture
async def audit_db(tmp_path: Path):
    await db_module.close_db()
    db_path = tmp_path / "audit.db"
    # apply both the identity schema (to mirror production) and the
    # audit schema the emitter writes to
    apply_audit_schema_sqlite(str(db_path))
    # Point db_module at this sqlite file so insert_audit_event finds it.
    db_module._engine = {"backend": "sqlite3", "path": str(db_path)}  # type: ignore[attr-defined]
    db_module._session_factory = True  # type: ignore[attr-defined]
    db_module._engine_backend = "sqlite3"  # type: ignore[attr-defined]
    db_module._sqlite_path = str(db_path)  # type: ignore[attr-defined]

    yield db_path

    await db_module.close_db()


def test_audit_event_row_serialises_metadata_to_json() -> None:
    event = AuditEventRow(
        action="kb:delete_document",
        resource_type="document",
        outcome="success",
        metadata={"doc_ids": ["a", "b"], "reason": "stale"},
    )
    row = event.to_row()
    assert isinstance(row["metadata"], str)
    parsed = json.loads(row["metadata"])
    assert parsed == {"doc_ids": ["a", "b"], "reason": "stale"}


def test_audit_event_row_none_metadata_stays_null() -> None:
    event = AuditEventRow(
        action="kb:view",
        resource_type="kb",
        outcome="success",
    )
    row = event.to_row()
    assert row["metadata"] is None


def test_audit_event_row_id_and_timestamp_autofilled() -> None:
    event = AuditEventRow(
        action="workspace:view",
        resource_type="workspace",
        outcome="success",
    )
    assert event.id
    assert event.occurred_at


@pytest.mark.asyncio
async def test_insert_audit_event_persists_row(audit_db: Path) -> None:
    event = AuditEventRow(
        action="workspace:update",
        resource_type="workspace",
        outcome="success",
        actor_user_id="u1",
        actor_username="alice",
        workspace_id="alpha",
        http_method="PATCH",
        http_path="/workspaces/alpha",
        status_code=200,
        metadata={"name": {"before": "A", "after": "Alpha"}},
    )

    ok = await insert_audit_event(event)
    assert ok is True

    with sqlite3.connect(str(audit_db)) as conn:
        cursor = conn.execute(
            f"SELECT action, outcome, actor_username, http_method, status_code, metadata "
            f"FROM {AUDIT_LOG_TABLE_NAME} WHERE id = ?",
            (event.id,),
        )
        row = cursor.fetchone()

    assert row is not None
    assert row[0] == "workspace:update"
    assert row[1] == "success"
    assert row[2] == "alice"
    assert row[3] == "PATCH"
    assert row[4] == 200
    assert json.loads(row[5]) == {"name": {"before": "A", "after": "Alpha"}}


@pytest.mark.asyncio
async def test_insert_audit_event_without_backend_returns_false(tmp_path: Path) -> None:
    """With no DB backend configured, insert should gracefully return False."""
    await db_module.close_db()
    event = AuditEventRow(
        action="kb:view",
        resource_type="kb",
        outcome="success",
    )
    ok = await insert_audit_event(event)
    assert ok is False
