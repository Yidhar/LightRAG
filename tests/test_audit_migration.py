"""
Offline tests for the PR-AUDIT-1 schema migration.

Covers:

- DDL is idempotent (IF NOT EXISTS)
- Schema creates the expected table and indexes
- NOT NULL constraints on required fields are enforced
- A minimal valid row is accepted
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lightrag.api.audit_store import (
    AUDIT_LOG_TABLE_NAME,
    apply_audit_schema_sqlite,
    audit_schema_ddl,
)

pytestmark = pytest.mark.offline


def _index_names(conn: sqlite3.Connection) -> set[str]:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
        (AUDIT_LOG_TABLE_NAME,),
    )
    return {row[0] for row in cursor.fetchall()}


def _column_names(conn: sqlite3.Connection) -> set[str]:
    cursor = conn.execute(f"PRAGMA table_info({AUDIT_LOG_TABLE_NAME})")
    return {row[1] for row in cursor.fetchall()}


def test_schema_ddl_is_idempotent_by_construction() -> None:
    for statement in audit_schema_ddl():
        assert "IF NOT EXISTS" in statement


def test_apply_creates_table_with_expected_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "platform.db"
    apply_audit_schema_sqlite(str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (AUDIT_LOG_TABLE_NAME,),
        )
        assert cursor.fetchone() is not None

        columns = _column_names(conn)

    expected = {
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
    }
    assert expected <= columns


def test_apply_creates_expected_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "platform.db"
    apply_audit_schema_sqlite(str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        indexes = _index_names(conn)

    # Ignore sqlite-internal autoindexes; only match ours.
    managed = {name for name in indexes if name.startswith(f"idx_{AUDIT_LOG_TABLE_NAME}_")}

    assert {
        f"idx_{AUDIT_LOG_TABLE_NAME}_workspace_time",
        f"idx_{AUDIT_LOG_TABLE_NAME}_actor_time",
        f"idx_{AUDIT_LOG_TABLE_NAME}_action_time",
        f"idx_{AUDIT_LOG_TABLE_NAME}_resource",
    } == managed


def test_apply_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "platform.db"
    apply_audit_schema_sqlite(str(db_path))
    apply_audit_schema_sqlite(str(db_path))  # second run must not raise

    with sqlite3.connect(str(db_path)) as conn:
        indexes = _index_names(conn)
    managed = {name for name in indexes if name.startswith(f"idx_{AUDIT_LOG_TABLE_NAME}_")}
    assert len(managed) == 4


def test_required_fields_reject_null(tmp_path: Path) -> None:
    db_path = tmp_path / "platform.db"
    apply_audit_schema_sqlite(str(db_path))

    base_values: dict[str, str | None] = {
        "id": "'evt-fixture'",
        "occurred_at": "'2026-04-17T00:00:00Z'",
        "action": "'kb:view'",
        "resource_type": "'kb'",
        "outcome": "'success'",
    }

    with sqlite3.connect(str(db_path)) as conn:
        for required in ("occurred_at", "action", "resource_type", "outcome"):
            values = dict(base_values)
            values["id"] = f"'evt-null-{required}'"
            values[required] = "NULL"
            columns = ", ".join(values.keys())
            literals = ", ".join(values.values())  # type: ignore[arg-type]
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    f"INSERT INTO {AUDIT_LOG_TABLE_NAME} ({columns}) VALUES ({literals})"
                )
            conn.rollback()


def test_minimal_row_accepted(tmp_path: Path) -> None:
    db_path = tmp_path / "platform.db"
    apply_audit_schema_sqlite(str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            f"INSERT INTO {AUDIT_LOG_TABLE_NAME} "
            "(id, occurred_at, action, resource_type, outcome) "
            "VALUES (?, ?, ?, ?, ?)",
            ("evt-minimal", "2026-04-17T00:00:00Z", "kb:view", "kb", "success"),
        )
        conn.commit()

        cursor = conn.execute(
            f"SELECT id, action, outcome FROM {AUDIT_LOG_TABLE_NAME} WHERE id=?",
            ("evt-minimal",),
        )
        assert cursor.fetchone() == ("evt-minimal", "kb:view", "success")
