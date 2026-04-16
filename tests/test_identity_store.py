import importlib
import sqlite3
import sys

import pytest

pytestmark = pytest.mark.offline


def import_real_api_module(module_name: str):
    sys.modules.pop(module_name, None)

    package_name, _, child_name = module_name.rpartition(".")
    package = sys.modules.get(package_name)
    if package is not None and hasattr(package, child_name):
        delattr(package, child_name)

    return importlib.import_module(module_name)


@pytest.fixture(autouse=True)
def clear_identity_modules():
    module_names = (
        "lightrag.api.db",
        "lightrag.api.models",
        "lightrag.api.models.user",
        "lightrag.api.models.membership",
        "lightrag.api.identity_store",
        "lightrag.api.auth_accounts",
    )
    for module_name in module_names:
        sys.modules.pop(module_name, None)

    yield

    for module_name in module_names:
        sys.modules.pop(module_name, None)


def _list_tables(db_path):
    with sqlite3.connect(db_path) as conn:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def _fetch_users(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT username, password_secret, source, is_active FROM users ORDER BY username"
        ).fetchall()
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_bootstrap_identity_store_creates_phase_b1_tables(tmp_path):
    db_module = import_real_api_module("lightrag.api.db")
    identity_store = import_real_api_module("lightrag.api.identity_store")

    db_path = tmp_path / "identity_bootstrap.db"
    db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    await db_module.init_db(db_url)
    try:
        summary = await identity_store.bootstrap_identity_store("")
    finally:
        await db_module.close_db()

    assert summary.to_dict() == {
        "parsed_count": 0,
        "inserted_count": 0,
        "updated_count": 0,
    }

    assert _list_tables(db_path) >= {
        "platform_bootstrap_state",
        "users",
        "workspace_memberships",
        "kb_memberships",
        "refresh_tokens",
    }


@pytest.mark.asyncio
async def test_seed_env_accounts_upserts_users(tmp_path):
    db_module = import_real_api_module("lightrag.api.db")
    identity_store = import_real_api_module("lightrag.api.identity_store")

    db_path = tmp_path / "identity_seed.db"
    db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    await db_module.init_db(db_url)
    try:
        await identity_store.initialize_identity_schema()
        first_summary = await identity_store.seed_env_accounts(
            "alice:secret,bob:{{bcrypt}}hashvalue"
        )
        second_summary = await identity_store.seed_env_accounts(
            "alice:new_secret,bob:{{bcrypt}}hashvalue"
        )
    finally:
        await db_module.close_db()

    assert first_summary.to_dict() == {
        "parsed_count": 2,
        "inserted_count": 2,
        "updated_count": 0,
    }
    assert second_summary.to_dict() == {
        "parsed_count": 2,
        "inserted_count": 0,
        "updated_count": 2,
    }

    assert _fetch_users(db_path) == [
        {
            "username": "alice",
            "password_secret": "new_secret",
            "source": "env",
            "is_active": 1,
        },
        {
            "username": "bob",
            "password_secret": "{{bcrypt}}hashvalue",
            "source": "env",
            "is_active": 1,
        },
    ]


@pytest.mark.asyncio
async def test_invalid_auth_accounts_fail_without_partial_write(tmp_path):
    db_module = import_real_api_module("lightrag.api.db")
    identity_store = import_real_api_module("lightrag.api.identity_store")

    db_path = tmp_path / "identity_invalid.db"
    db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    await db_module.init_db(db_url)
    try:
        await identity_store.initialize_identity_schema()
        with pytest.raises(ValueError, match="AUTH_ACCOUNTS must use"):
            await identity_store.seed_env_accounts("alice:secret,")
    finally:
        await db_module.close_db()

    assert _fetch_users(db_path) == []


def test_models_package_exports_phase_b1_identity_types():
    models = import_real_api_module("lightrag.api.models")

    assert "UserRow" in models.__all__
    assert "WorkspaceMemberRow" in models.__all__
    assert "KBMemberRow" in models.__all__
    assert "RefreshTokenRow" in models.__all__
