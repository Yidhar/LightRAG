"""
Offline tests for the Phase W1 workspace entity.

Covers:

- the identity_store workspace CRUD helpers against a clean sqlite DB
- the ``migrate_workspaces_create_table`` script (schema creation,
  backfill, idempotency)
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.offline


# The migration script lives under scripts/, which is not on sys.path by
# default in CI. Resolving it manually keeps this test self-contained.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_migration_module():
    # Importlib shim so we can run the CLI script as a library.
    import importlib.util

    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))

    spec = importlib.util.spec_from_file_location(
        "migrate_workspaces_create_table",
        _SCRIPTS_DIR / "migrate_workspaces_create_table.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Migration script
# ---------------------------------------------------------------------------


def test_migration_creates_workspaces_table(tmp_path: Path) -> None:
    migration = _load_migration_module()
    db_path = tmp_path / "platform.db"

    migration.ensure_schema(str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("workspaces",),
        )
        assert cursor.fetchone() is not None

        cursor = conn.execute("PRAGMA table_info(workspaces)")
        columns = {row[1] for row in cursor.fetchall()}
    assert {
        "id",
        "name",
        "description",
        "owner_user_id",
        "created_at",
        "updated_at",
    } <= columns


def test_migration_backfills_default_workspace(tmp_path: Path) -> None:
    migration = _load_migration_module()
    db_path = tmp_path / "platform.db"
    migration.ensure_schema(str(db_path))

    summary = migration.backfill_workspaces(
        str(db_path),
        default_workspace_id="default",
        default_workspace_name="Default workspace",
    )

    assert "default" in summary["inserted_ids"]
    assert summary["default_inserted"] is True

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT id, name FROM workspaces WHERE id = 'default'"
        ).fetchone()
    assert row == ("default", "Default workspace")


def test_migration_backfill_is_idempotent(tmp_path: Path) -> None:
    migration = _load_migration_module()
    db_path = tmp_path / "platform.db"
    migration.ensure_schema(str(db_path))

    first = migration.backfill_workspaces(
        str(db_path),
        default_workspace_id="default",
        default_workspace_name="Default",
    )
    second = migration.backfill_workspaces(
        str(db_path),
        default_workspace_id="default",
        default_workspace_name="Default",
    )
    assert "default" in first["inserted_ids"]
    assert "default" in second["skipped_ids"]
    assert second["default_inserted"] is False


def test_migration_backfills_distinct_workspaces_from_memberships(
    tmp_path: Path,
) -> None:
    migration = _load_migration_module()
    db_path = tmp_path / "platform.db"

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_memberships (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                role TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO workspace_memberships
            (id, user_id, workspace_id, role, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(uuid4()),
                    "u1",
                    workspace,
                    "owner",
                    "seed",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                )
                for workspace in ("alpha", "beta")
            ],
        )
        conn.commit()

    migration.ensure_schema(str(db_path))
    summary = migration.backfill_workspaces(
        str(db_path),
        default_workspace_id="default",
        default_workspace_name="Default",
    )

    assert set(summary["inserted_ids"]) >= {"alpha", "beta", "default"}


# ---------------------------------------------------------------------------
# identity_store workspace CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
async def identity_db(tmp_path: Path):
    # Reset the db module state between tests so each run gets its own DB.
    from lightrag.api import db as db_module

    await db_module.close_db()

    db_path = tmp_path / "identity.db"
    await db_module.init_db(f"sqlite+aiosqlite:///{db_path}")

    # Apply identity schema (includes workspaces via _CREATE_STATEMENTS).
    from lightrag.api import identity_store

    await identity_store.initialize_identity_schema()

    yield db_module

    await db_module.close_db()


async def _seed_user(user_id: str = "user-1", username: str = "alice") -> None:
    from lightrag.api.identity_store import _run_sqlite_statements  # noqa: F401
    from lightrag.api import db as db_module

    sqlite_path = db_module.get_sqlite_path()
    assert sqlite_path is not None
    import sqlite3

    with sqlite3.connect(sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO users (id, username, password_secret, source, is_active, created_at, updated_at)
            VALUES (?, ?, ?, 'seed', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """,
            (user_id, username, "hash"),
        )
        conn.commit()


@pytest.mark.asyncio
async def test_create_and_get_workspace(identity_db) -> None:
    from lightrag.api.identity_store import create_workspace, get_workspace

    record = await create_workspace(
        workspace_id="alpha",
        name="Alpha workspace",
        description="for testing",
        owner_user_id="user-1",
    )

    assert record.id == "alpha"
    assert record.name == "Alpha workspace"
    assert record.description == "for testing"
    assert record.owner_user_id == "user-1"

    fetched = await get_workspace("alpha")
    assert fetched is not None
    assert fetched.id == "alpha"
    assert fetched.name == "Alpha workspace"


@pytest.mark.asyncio
async def test_update_workspace_partial(identity_db) -> None:
    from lightrag.api.identity_store import create_workspace, update_workspace

    await create_workspace(workspace_id="alpha", name="Original")

    updated = await update_workspace("alpha", name="Renamed")
    assert updated is not None
    assert updated.name == "Renamed"
    assert updated.description is None  # original had none

    # Update only description
    partial = await update_workspace("alpha", description="fresh doc")
    assert partial is not None
    assert partial.name == "Renamed"
    assert partial.description == "fresh doc"


@pytest.mark.asyncio
async def test_update_workspace_missing_returns_none(identity_db) -> None:
    from lightrag.api.identity_store import update_workspace

    assert await update_workspace("nope", name="x") is None


@pytest.mark.asyncio
async def test_delete_workspace_cascades_memberships(identity_db) -> None:
    from lightrag.api.identity_store import (
        create_workspace,
        delete_workspace,
        get_workspace,
        upsert_workspace_membership,
    )

    await _seed_user()
    await create_workspace(workspace_id="alpha", name="A")
    await upsert_workspace_membership(
        user_id="user-1",
        workspace_id="alpha",
        role="owner",
        source="seed",
    )

    removed = await delete_workspace("alpha")
    assert removed is True

    assert await get_workspace("alpha") is None

    # Membership row is gone too.
    import sqlite3
    from lightrag.api import db as db_module

    with sqlite3.connect(db_module.get_sqlite_path()) as conn:
        row = conn.execute(
            "SELECT 1 FROM workspace_memberships WHERE workspace_id='alpha'"
        ).fetchone()
    assert row is None


@pytest.mark.asyncio
async def test_delete_missing_workspace_returns_false(identity_db) -> None:
    from lightrag.api.identity_store import delete_workspace

    assert await delete_workspace("ghost") is False


@pytest.mark.asyncio
async def test_list_workspaces_for_user_returns_only_memberships(
    identity_db,
) -> None:
    from lightrag.api.identity_store import (
        create_workspace,
        list_workspaces_for_user,
        upsert_workspace_membership,
    )

    await _seed_user(user_id="user-1")
    await _seed_user(user_id="user-2", username="bob")
    await create_workspace(workspace_id="alpha", name="A")
    await create_workspace(workspace_id="beta", name="B")

    await upsert_workspace_membership(
        user_id="user-1", workspace_id="alpha", role="owner", source="seed"
    )
    await upsert_workspace_membership(
        user_id="user-2", workspace_id="beta", role="owner", source="seed"
    )

    rows = await list_workspaces_for_user("user-1")
    ids = [row.id for row in rows]
    assert ids == ["alpha"]

    rows = await list_workspaces_for_user("user-2")
    ids = [row.id for row in rows]
    assert ids == ["beta"]
