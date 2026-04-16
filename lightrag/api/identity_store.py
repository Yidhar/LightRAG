"""
Schema bootstrap and env-account seeding for the staged DB-backed auth rollout.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from lightrag.api.auth_accounts import parse_auth_accounts
from lightrag.api.db import (
    get_db_backend,
    get_engine,
    get_sqlite_path,
    is_db_initialized,
)
from lightrag.api.passwords import hash_password
from lightrag.api.models import (
    KB_MEMBER_TABLE_NAME,
    REFRESH_TOKEN_TABLE_NAME,
    USER_TABLE_NAME,
    WORKSPACE_MEMBER_TABLE_NAME,
)
from lightrag.utils import logger

_CREATE_USERS_SQL = f"""
CREATE TABLE IF NOT EXISTS {USER_TABLE_NAME} (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_secret TEXT NOT NULL,
    source TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
""".strip()

_CREATE_WORKSPACE_MEMBERSHIPS_SQL = f"""
CREATE TABLE IF NOT EXISTS {WORKSPACE_MEMBER_TABLE_NAME} (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    role TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, workspace_id)
)
""".strip()

_CREATE_KB_MEMBERSHIPS_SQL = f"""
CREATE TABLE IF NOT EXISTS {KB_MEMBER_TABLE_NAME} (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    kb_id TEXT NOT NULL,
    role TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, workspace_id, kb_id)
)
""".strip()

_CREATE_REFRESH_TOKENS_SQL = f"""
CREATE TABLE IF NOT EXISTS {REFRESH_TOKEN_TABLE_NAME} (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_jti TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT NULL,
    rotated_from_token_id TEXT NULL,
    created_at TEXT NOT NULL
)
""".strip()

_CREATE_STATEMENTS = (
    _CREATE_USERS_SQL,
    _CREATE_WORKSPACE_MEMBERSHIPS_SQL,
    _CREATE_KB_MEMBERSHIPS_SQL,
    _CREATE_REFRESH_TOKENS_SQL,
)


@dataclass(slots=True)
class EnvAccountSeedSummary:
    parsed_count: int = 0
    inserted_count: int = 0
    updated_count: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(slots=True)
class IdentityUserRecord:
    user_id: str
    username: str
    is_active: bool
    source: str = "env"


@dataclass(slots=True)
class IdentityAuthRecord(IdentityUserRecord):
    password_secret: str = ""


@dataclass(slots=True)
class RefreshTokenIssue:
    token_id: str
    raw_token: str
    token_jti: str
    issued_at: str
    expires_at: str
    rotated_from_token_id: str | None = None


@dataclass(slots=True)
class RefreshTokenRotationResult:
    user: IdentityUserRecord
    memberships: list[dict[str, str | None]]
    previous_token_id: str
    issued_refresh_token: RefreshTokenIssue


class RefreshTokenValidationError(Exception):
    def __init__(self, detail: str, *, status_code: int = 401):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(slots=True)
class MembershipRecord:
    membership_id: str
    user_id: str
    username: str
    workspace_id: str
    kb_id: str | None
    role: str
    source: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class MembershipWriteResult:
    membership: MembershipRecord
    created: bool


_MEMBERSHIP_ROLES = ("owner", "admin", "editor", "viewer")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _build_env_user_id(username: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"lightrag://env-user/{username}"))


def _run_sqlite_statements(path: str, statements: tuple[str, ...]) -> None:
    with sqlite3.connect(path) as conn:
        for statement in statements:
            conn.execute(statement)
        conn.commit()


def _fetch_existing_sqlite_usernames(path: str) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute(f"SELECT username FROM {USER_TABLE_NAME}")}


def _upsert_sqlite_users(path: str, accounts: dict[str, str]) -> EnvAccountSeedSummary:
    existing = _fetch_existing_sqlite_usernames(path)
    summary = EnvAccountSeedSummary(parsed_count=len(accounts))
    now = _utcnow_iso()
    upsert_sql = f"""
    INSERT INTO {USER_TABLE_NAME} (
        id, username, password_secret, source, is_active, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(username) DO UPDATE SET
        password_secret=excluded.password_secret,
        source=excluded.source,
        is_active=excluded.is_active,
        updated_at=excluded.updated_at
    """

    with sqlite3.connect(path) as conn:
        for username, password_secret in accounts.items():
            if username in existing:
                summary.updated_count += 1
            else:
                summary.inserted_count += 1

            conn.execute(
                upsert_sql,
                (
                    _build_env_user_id(username),
                    username,
                    password_secret,
                    "env",
                    1,
                    now,
                    now,
                ),
            )
        conn.commit()

    return summary


async def initialize_identity_schema() -> None:
    """Create staged auth/RBAC tables on top of the PR-1 DB scaffold."""
    if not is_db_initialized():
        raise RuntimeError("Database scaffold must be initialized before identity schema.")

    backend = get_db_backend()
    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.begin() as conn:
            for statement in _CREATE_STATEMENTS:
                await conn.execute(text(statement))
        return

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        await asyncio.to_thread(_run_sqlite_statements, sqlite_path, _CREATE_STATEMENTS)
        return

    raise RuntimeError(f"Unsupported DB backend for identity schema bootstrap: {backend}")


async def seed_env_accounts(auth_accounts: str | None) -> EnvAccountSeedSummary:
    """Upsert legacy AUTH_ACCOUNTS into the staged users table."""
    if not is_db_initialized():
        raise RuntimeError("Database scaffold must be initialized before seeding env users.")

    accounts = parse_auth_accounts(auth_accounts)
    if not accounts:
        return EnvAccountSeedSummary()

    backend = get_db_backend()
    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.begin() as conn:
            existing_rows = await conn.execute(text(f"SELECT username FROM {USER_TABLE_NAME}"))
            existing = {row[0] for row in existing_rows}
            summary = EnvAccountSeedSummary(parsed_count=len(accounts))
            now = _utcnow_iso()
            upsert_sql = text(
                f"""
                INSERT INTO {USER_TABLE_NAME} (
                    id, username, password_secret, source, is_active, created_at, updated_at
                ) VALUES (
                    :id, :username, :password_secret, :source, :is_active, :created_at, :updated_at
                )
                ON CONFLICT(username) DO UPDATE SET
                    password_secret=excluded.password_secret,
                    source=excluded.source,
                    is_active=excluded.is_active,
                    updated_at=excluded.updated_at
                """
            )
            for username, password_secret in accounts.items():
                if username in existing:
                    summary.updated_count += 1
                else:
                    summary.inserted_count += 1
                await conn.execute(
                    upsert_sql,
                    {
                        "id": _build_env_user_id(username),
                        "username": username,
                        "password_secret": password_secret,
                        "source": "env",
                        "is_active": 1,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            return summary

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        return await asyncio.to_thread(_upsert_sqlite_users, sqlite_path, accounts)

    raise RuntimeError(f"Unsupported DB backend for env-account seeding: {backend}")


async def bootstrap_identity_store(auth_accounts: str | None) -> EnvAccountSeedSummary:
    """Create identity tables and upsert env accounts in one safe startup step."""
    await initialize_identity_schema()
    summary = await seed_env_accounts(auth_accounts)
    logger.info(
        "Platform identity bootstrap complete: parsed=%s inserted=%s updated=%s",
        summary.parsed_count,
        summary.inserted_count,
        summary.updated_count,
    )
    return summary


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _hash_refresh_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _generate_refresh_token() -> str:
    return secrets.token_urlsafe(64)


def _build_identity_user_record(row) -> IdentityUserRecord | None:
    if row is None:
        return None

    user_id = row["id"] if isinstance(row, sqlite3.Row) else row["id"]
    username = row["username"] if isinstance(row, sqlite3.Row) else row["username"]
    source = row["source"] if isinstance(row, sqlite3.Row) else row["source"]
    is_active = row["is_active"] if isinstance(row, sqlite3.Row) else row["is_active"]

    return IdentityUserRecord(
        user_id=user_id,
        username=username,
        is_active=bool(is_active),
        source=source,
    )


def _build_identity_auth_record(row) -> IdentityAuthRecord | None:
    if row is None:
        return None

    return IdentityAuthRecord(
        user_id=row["id"] if isinstance(row, sqlite3.Row) else row["id"],
        username=row["username"] if isinstance(row, sqlite3.Row) else row["username"],
        is_active=bool(
            row["is_active"] if isinstance(row, sqlite3.Row) else row["is_active"]
        ),
        source=row["source"] if isinstance(row, sqlite3.Row) else row["source"],
        password_secret=row["password_secret"]
        if isinstance(row, sqlite3.Row)
        else row["password_secret"],
    )


def _normalize_membership_rows(rows) -> list[dict[str, str | None]]:
    normalized = []
    for row in rows:
        normalized.append(
            {
                "workspace_id": row["workspace_id"],
                "kb_id": row["kb_id"],
                "role": row["role"],
            }
        )
    return normalized


def _build_membership_record(row) -> MembershipRecord | None:
    if row is None:
        return None

    return MembershipRecord(
        membership_id=row["membership_id"],
        user_id=row["user_id"],
        username=row["username"],
        workspace_id=row["workspace_id"],
        kb_id=row["kb_id"],
        role=row["role"],
        source=row["source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _build_workspace_membership_id(user_id: str, workspace_id: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"lightrag://workspace-membership/{workspace_id}/{user_id}",
        )
    )


def _build_kb_membership_id(user_id: str, workspace_id: str, kb_id: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"lightrag://kb-membership/{workspace_id}/{kb_id}/{user_id}",
        )
    )


def _validate_membership_role(role: str) -> str:
    normalized = (role or "").strip().lower()
    if normalized not in _MEMBERSHIP_ROLES:
        raise ValueError(
            f"Unsupported membership role '{role}'. Expected one of: {', '.join(_MEMBERSHIP_ROLES)}."
        )
    return normalized


def _require_db_backend() -> str:
    backend = get_db_backend()
    if backend is None:
        raise RuntimeError(
            "Database scaffold must be initialized before using identity queries."
        )
    return backend


def _sqlite_fetch_one(path: str, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchone()


def _sqlite_fetch_all(path: str, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


async def get_user_by_username(username: str) -> IdentityUserRecord | None:
    backend = _require_db_backend()

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"""
                    SELECT id, username, source, is_active
                    FROM {USER_TABLE_NAME}
                    WHERE username = :username
                    LIMIT 1
                    """
                ),
                {"username": username},
            )
            row = result.mappings().first()
        return _build_identity_user_record(row)

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        row = await asyncio.to_thread(
            _sqlite_fetch_one,
            sqlite_path,
            f"""
            SELECT id, username, source, is_active
            FROM {USER_TABLE_NAME}
            WHERE username = ?
            LIMIT 1
            """,
            (username,),
        )
        return _build_identity_user_record(row)

    raise RuntimeError(f"Unsupported DB backend for user lookup: {backend}")


async def get_user_by_id(user_id: str) -> IdentityUserRecord | None:
    backend = _require_db_backend()

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"""
                    SELECT id, username, source, is_active
                    FROM {USER_TABLE_NAME}
                    WHERE id = :user_id
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            )
            row = result.mappings().first()
        return _build_identity_user_record(row)

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        row = await asyncio.to_thread(
            _sqlite_fetch_one,
            sqlite_path,
            f"""
            SELECT id, username, source, is_active
            FROM {USER_TABLE_NAME}
            WHERE id = ?
            LIMIT 1
            """,
            (user_id,),
        )
        return _build_identity_user_record(row)

    raise RuntimeError(f"Unsupported DB backend for user lookup: {backend}")


async def get_user_auth_by_username(username: str) -> IdentityAuthRecord | None:
    """Return the DB-backed auth record for a local account."""
    backend = _require_db_backend()

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"""
                    SELECT id, username, password_secret, source, is_active
                    FROM {USER_TABLE_NAME}
                    WHERE username = :username
                    LIMIT 1
                    """
                ),
                {"username": username},
            )
            row = result.mappings().first()
        return _build_identity_auth_record(row)

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        row = await asyncio.to_thread(
            _sqlite_fetch_one,
            sqlite_path,
            f"""
            SELECT id, username, password_secret, source, is_active
            FROM {USER_TABLE_NAME}
            WHERE username = ?
            LIMIT 1
            """,
            (username,),
        )
        return _build_identity_auth_record(row)

    raise RuntimeError(f"Unsupported DB backend for auth user lookup: {backend}")


async def list_users() -> list[IdentityUserRecord]:
    """List local directory users for account-management surfaces."""
    backend = _require_db_backend()
    sql = f"""
    SELECT id, username, source, is_active
    FROM {USER_TABLE_NAME}
    ORDER BY username
    """

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.connect() as conn:
            rows = (await conn.execute(text(sql))).mappings().all()
        return [
            user
            for user in (_build_identity_user_record(row) for row in rows)
            if user is not None
        ]

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        rows = await asyncio.to_thread(_sqlite_fetch_all, sqlite_path, sql)
        return [
            user
            for user in (_build_identity_user_record(row) for row in rows)
            if user is not None
        ]

    raise RuntimeError(f"Unsupported DB backend for user listing: {backend}")


async def create_user(
    username: str,
    password: str,
    *,
    source: str = "local",
    is_active: bool = True,
) -> IdentityUserRecord:
    """Create a new local directory user with a bcrypt password secret."""
    backend = _require_db_backend()
    now = _utcnow_iso()
    user_id = str(uuid4())
    password_secret = hash_password(password)

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.begin() as conn:
            existing = await conn.execute(
                text(
                    f"""
                    SELECT id
                    FROM {USER_TABLE_NAME}
                    WHERE username = :username
                    LIMIT 1
                    """
                ),
                {"username": username},
            )
            if existing.mappings().first() is not None:
                raise ValueError(f"User '{username}' already exists.")

            await conn.execute(
                text(
                    f"""
                    INSERT INTO {USER_TABLE_NAME} (
                        id, username, password_secret, source, is_active, created_at, updated_at
                    ) VALUES (
                        :id, :username, :password_secret, :source, :is_active, :created_at, :updated_at
                    )
                    """
                ),
                {
                    "id": user_id,
                    "username": username,
                    "password_secret": password_secret,
                    "source": source,
                    "is_active": 1 if is_active else 0,
                    "created_at": now,
                    "updated_at": now,
                },
            )

        return IdentityUserRecord(
            user_id=user_id,
            username=username,
            is_active=is_active,
            source=source,
        )

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()

        def _sqlite_create_user() -> IdentityUserRecord:
            with sqlite3.connect(sqlite_path) as conn:
                existing = conn.execute(
                    f"SELECT id FROM {USER_TABLE_NAME} WHERE username = ? LIMIT 1",
                    (username,),
                ).fetchone()
                if existing is not None:
                    raise ValueError(f"User '{username}' already exists.")

                conn.execute(
                    f"""
                    INSERT INTO {USER_TABLE_NAME} (
                        id, username, password_secret, source, is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        username,
                        password_secret,
                        source,
                        1 if is_active else 0,
                        now,
                        now,
                    ),
                )
                conn.commit()

            return IdentityUserRecord(
                user_id=user_id,
                username=username,
                is_active=is_active,
                source=source,
            )

        return await asyncio.to_thread(_sqlite_create_user)

    raise RuntimeError(f"Unsupported DB backend for user creation: {backend}")


async def update_user(
    user_id: str,
    *,
    username: str | None = None,
    is_active: bool | None = None,
) -> IdentityUserRecord:
    """Update mutable local user attributes while keeping the auth secret intact."""
    backend = _require_db_backend()
    existing = await get_user_auth_by_id(user_id)
    if existing is None:
        raise ValueError(f"User '{user_id}' was not found.")

    next_username = (username or existing.username).strip()
    next_is_active = existing.is_active if is_active is None else bool(is_active)
    now = _utcnow_iso()

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.begin() as conn:
            if next_username != existing.username:
                duplicate = await conn.execute(
                    text(
                        f"""
                        SELECT id
                        FROM {USER_TABLE_NAME}
                        WHERE username = :username AND id <> :user_id
                        LIMIT 1
                        """
                    ),
                    {"username": next_username, "user_id": user_id},
                )
                if duplicate.mappings().first() is not None:
                    raise ValueError(f"User '{next_username}' already exists.")

            await conn.execute(
                text(
                    f"""
                    UPDATE {USER_TABLE_NAME}
                    SET username = :username,
                        is_active = :is_active,
                        updated_at = :updated_at
                    WHERE id = :user_id
                    """
                ),
                {
                    "username": next_username,
                    "is_active": 1 if next_is_active else 0,
                    "updated_at": now,
                    "user_id": user_id,
                },
            )

    elif backend == "sqlite3":
        sqlite_path = get_sqlite_path()

        def _sqlite_update_user() -> None:
            with sqlite3.connect(sqlite_path) as conn:
                if next_username != existing.username:
                    duplicate = conn.execute(
                        f"""
                        SELECT id
                        FROM {USER_TABLE_NAME}
                        WHERE username = ? AND id <> ?
                        LIMIT 1
                        """,
                        (next_username, user_id),
                    ).fetchone()
                    if duplicate is not None:
                        raise ValueError(f"User '{next_username}' already exists.")

                conn.execute(
                    f"""
                    UPDATE {USER_TABLE_NAME}
                    SET username = ?, is_active = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_username, 1 if next_is_active else 0, now, user_id),
                )
                conn.commit()

        await asyncio.to_thread(_sqlite_update_user)
    else:
        raise RuntimeError(f"Unsupported DB backend for user update: {backend}")

    return IdentityUserRecord(
        user_id=user_id,
        username=next_username,
        is_active=next_is_active,
        source=existing.source,
    )


async def set_user_password(user_id: str, password: str) -> IdentityUserRecord:
    """Rotate the stored local password secret for a user."""
    backend = _require_db_backend()
    existing = await get_user_auth_by_id(user_id)
    if existing is None:
        raise ValueError(f"User '{user_id}' was not found.")

    password_secret = hash_password(password)
    now = _utcnow_iso()

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"""
                    UPDATE {USER_TABLE_NAME}
                    SET password_secret = :password_secret,
                        updated_at = :updated_at
                    WHERE id = :user_id
                    """
                ),
                {
                    "password_secret": password_secret,
                    "updated_at": now,
                    "user_id": user_id,
                },
            )
    elif backend == "sqlite3":
        sqlite_path = get_sqlite_path()

        def _sqlite_set_password() -> None:
            with sqlite3.connect(sqlite_path) as conn:
                conn.execute(
                    f"""
                    UPDATE {USER_TABLE_NAME}
                    SET password_secret = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (password_secret, now, user_id),
                )
                conn.commit()

        await asyncio.to_thread(_sqlite_set_password)
    else:
        raise RuntimeError(f"Unsupported DB backend for password rotation: {backend}")

    return IdentityUserRecord(
        user_id=existing.user_id,
        username=existing.username,
        is_active=existing.is_active,
        source=existing.source,
    )


async def get_user_auth_by_id(user_id: str) -> IdentityAuthRecord | None:
    """Return a DB-backed auth record by user id."""
    backend = _require_db_backend()

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"""
                    SELECT id, username, password_secret, source, is_active
                    FROM {USER_TABLE_NAME}
                    WHERE id = :user_id
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            )
            row = result.mappings().first()
        return _build_identity_auth_record(row)

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        row = await asyncio.to_thread(
            _sqlite_fetch_one,
            sqlite_path,
            f"""
            SELECT id, username, password_secret, source, is_active
            FROM {USER_TABLE_NAME}
            WHERE id = ?
            LIMIT 1
            """,
            (user_id,),
        )
        return _build_identity_auth_record(row)

    raise RuntimeError(f"Unsupported DB backend for auth user lookup: {backend}")


async def get_membership_claims(user_id: str) -> list[dict[str, str | None]]:
    backend = _require_db_backend()
    membership_sql = f"""
    SELECT workspace_id, NULL AS kb_id, role
    FROM {WORKSPACE_MEMBER_TABLE_NAME}
    WHERE user_id = :user_id
    UNION ALL
    SELECT workspace_id, kb_id, role
    FROM {KB_MEMBER_TABLE_NAME}
    WHERE user_id = :user_id
    ORDER BY workspace_id, kb_id, role
    """

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(text(membership_sql), {"user_id": user_id})
            rows = result.mappings().all()
        return _normalize_membership_rows(rows)

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        rows = await asyncio.to_thread(
            _sqlite_fetch_all,
            sqlite_path,
            membership_sql.replace(":user_id", "?"),
            (user_id, user_id),
        )
        return _normalize_membership_rows(rows)

    raise RuntimeError(f"Unsupported DB backend for membership lookup: {backend}")


async def list_workspace_memberships(workspace_id: str) -> list[MembershipRecord]:
    backend = _require_db_backend()
    sql = f"""
    SELECT
        m.id AS membership_id,
        m.user_id AS user_id,
        u.username AS username,
        m.workspace_id AS workspace_id,
        NULL AS kb_id,
        m.role AS role,
        m.source AS source,
        m.created_at AS created_at,
        m.updated_at AS updated_at
    FROM {WORKSPACE_MEMBER_TABLE_NAME} AS m
    INNER JOIN {USER_TABLE_NAME} AS u ON u.id = m.user_id
    WHERE m.workspace_id = :workspace_id
    ORDER BY u.username, m.role
    """

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text(sql), {"workspace_id": workspace_id})
            ).mappings().all()
        return [
            membership
            for membership in (_build_membership_record(row) for row in rows)
            if membership is not None
        ]

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        rows = await asyncio.to_thread(
            _sqlite_fetch_all,
            sqlite_path,
            sql.replace(":workspace_id", "?"),
            (workspace_id,),
        )
        return [
            membership
            for membership in (_build_membership_record(row) for row in rows)
            if membership is not None
        ]

    raise RuntimeError(f"Unsupported DB backend for workspace membership listing: {backend}")


async def list_kb_memberships(
    workspace_id: str,
    kb_id: str,
) -> list[MembershipRecord]:
    backend = _require_db_backend()
    sql = f"""
    SELECT
        m.id AS membership_id,
        m.user_id AS user_id,
        u.username AS username,
        m.workspace_id AS workspace_id,
        m.kb_id AS kb_id,
        m.role AS role,
        m.source AS source,
        m.created_at AS created_at,
        m.updated_at AS updated_at
    FROM {KB_MEMBER_TABLE_NAME} AS m
    INNER JOIN {USER_TABLE_NAME} AS u ON u.id = m.user_id
    WHERE m.workspace_id = :workspace_id AND m.kb_id = :kb_id
    ORDER BY u.username, m.role
    """

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(sql),
                    {"workspace_id": workspace_id, "kb_id": kb_id},
                )
            ).mappings().all()
        return [
            membership
            for membership in (_build_membership_record(row) for row in rows)
            if membership is not None
        ]

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        rows = await asyncio.to_thread(
            _sqlite_fetch_all,
            sqlite_path,
            sql.replace(":workspace_id", "?").replace(":kb_id", "?"),
            (workspace_id, kb_id),
        )
        return [
            membership
            for membership in (_build_membership_record(row) for row in rows)
            if membership is not None
        ]

    raise RuntimeError(f"Unsupported DB backend for KB membership listing: {backend}")


async def get_workspace_membership(
    user_id: str,
    workspace_id: str,
) -> MembershipRecord | None:
    backend = _require_db_backend()
    sql = f"""
    SELECT
        m.id AS membership_id,
        m.user_id AS user_id,
        u.username AS username,
        m.workspace_id AS workspace_id,
        NULL AS kb_id,
        m.role AS role,
        m.source AS source,
        m.created_at AS created_at,
        m.updated_at AS updated_at
    FROM {WORKSPACE_MEMBER_TABLE_NAME} AS m
    INNER JOIN {USER_TABLE_NAME} AS u ON u.id = m.user_id
    WHERE m.user_id = :user_id AND m.workspace_id = :workspace_id
    LIMIT 1
    """

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(sql),
                    {"user_id": user_id, "workspace_id": workspace_id},
                )
            ).mappings().first()
        return _build_membership_record(row)

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        row = await asyncio.to_thread(
            _sqlite_fetch_one,
            sqlite_path,
            sql.replace(":user_id", "?").replace(":workspace_id", "?"),
            (user_id, workspace_id),
        )
        return _build_membership_record(row)

    raise RuntimeError(f"Unsupported DB backend for workspace membership lookup: {backend}")


async def get_kb_membership(
    user_id: str,
    workspace_id: str,
    kb_id: str,
) -> MembershipRecord | None:
    backend = _require_db_backend()
    sql = f"""
    SELECT
        m.id AS membership_id,
        m.user_id AS user_id,
        u.username AS username,
        m.workspace_id AS workspace_id,
        m.kb_id AS kb_id,
        m.role AS role,
        m.source AS source,
        m.created_at AS created_at,
        m.updated_at AS updated_at
    FROM {KB_MEMBER_TABLE_NAME} AS m
    INNER JOIN {USER_TABLE_NAME} AS u ON u.id = m.user_id
    WHERE m.user_id = :user_id AND m.workspace_id = :workspace_id AND m.kb_id = :kb_id
    LIMIT 1
    """

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(sql),
                    {
                        "user_id": user_id,
                        "workspace_id": workspace_id,
                        "kb_id": kb_id,
                    },
                )
            ).mappings().first()
        return _build_membership_record(row)

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        row = await asyncio.to_thread(
            _sqlite_fetch_one,
            sqlite_path,
            sql.replace(":user_id", "?")
            .replace(":workspace_id", "?")
            .replace(":kb_id", "?"),
            (user_id, workspace_id, kb_id),
        )
        return _build_membership_record(row)

    raise RuntimeError(f"Unsupported DB backend for KB membership lookup: {backend}")


def _sqlite_execute(path: str, sql: str, params: tuple = ()) -> int:
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(sql, params)
        conn.commit()
        return int(cursor.rowcount or 0)


async def upsert_workspace_membership(
    user_id: str,
    workspace_id: str,
    role: str,
    *,
    source: str = "api",
) -> MembershipWriteResult:
    backend = _require_db_backend()
    normalized_role = _validate_membership_role(role)
    existing = await get_workspace_membership(user_id, workspace_id)
    now = _utcnow_iso()
    membership_id = existing.membership_id if existing else _build_workspace_membership_id(
        user_id,
        workspace_id,
    )

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"""
                    INSERT INTO {WORKSPACE_MEMBER_TABLE_NAME} (
                        id, user_id, workspace_id, role, source, created_at, updated_at
                    ) VALUES (
                        :id, :user_id, :workspace_id, :role, :source, :created_at, :updated_at
                    )
                    ON CONFLICT(user_id, workspace_id) DO UPDATE SET
                        role=excluded.role,
                        source=excluded.source,
                        updated_at=excluded.updated_at
                    """
                ),
                {
                    "id": membership_id,
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                    "role": normalized_role,
                    "source": source,
                    "created_at": existing.created_at if existing else now,
                    "updated_at": now,
                },
            )

    elif backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        await asyncio.to_thread(
            _sqlite_execute,
            sqlite_path,
            f"""
            INSERT INTO {WORKSPACE_MEMBER_TABLE_NAME} (
                id, user_id, workspace_id, role, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, workspace_id) DO UPDATE SET
                role=excluded.role,
                source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (
                membership_id,
                user_id,
                workspace_id,
                normalized_role,
                source,
                existing.created_at if existing else now,
                now,
            ),
        )
    else:
        raise RuntimeError(
            f"Unsupported DB backend for workspace membership upsert: {backend}"
        )

    membership = await get_workspace_membership(user_id, workspace_id)
    if membership is None:
        raise RuntimeError("Workspace membership upsert failed to persist.")
    return MembershipWriteResult(membership=membership, created=existing is None)


async def upsert_kb_membership(
    user_id: str,
    workspace_id: str,
    kb_id: str,
    role: str,
    *,
    source: str = "api",
) -> MembershipWriteResult:
    backend = _require_db_backend()
    normalized_role = _validate_membership_role(role)
    existing = await get_kb_membership(user_id, workspace_id, kb_id)
    now = _utcnow_iso()
    membership_id = existing.membership_id if existing else _build_kb_membership_id(
        user_id,
        workspace_id,
        kb_id,
    )

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"""
                    INSERT INTO {KB_MEMBER_TABLE_NAME} (
                        id, user_id, workspace_id, kb_id, role, source, created_at, updated_at
                    ) VALUES (
                        :id, :user_id, :workspace_id, :kb_id, :role, :source, :created_at, :updated_at
                    )
                    ON CONFLICT(user_id, workspace_id, kb_id) DO UPDATE SET
                        role=excluded.role,
                        source=excluded.source,
                        updated_at=excluded.updated_at
                    """
                ),
                {
                    "id": membership_id,
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                    "kb_id": kb_id,
                    "role": normalized_role,
                    "source": source,
                    "created_at": existing.created_at if existing else now,
                    "updated_at": now,
                },
            )

    elif backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        await asyncio.to_thread(
            _sqlite_execute,
            sqlite_path,
            f"""
            INSERT INTO {KB_MEMBER_TABLE_NAME} (
                id, user_id, workspace_id, kb_id, role, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, workspace_id, kb_id) DO UPDATE SET
                role=excluded.role,
                source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (
                membership_id,
                user_id,
                workspace_id,
                kb_id,
                normalized_role,
                source,
                existing.created_at if existing else now,
                now,
            ),
        )
    else:
        raise RuntimeError(f"Unsupported DB backend for KB membership upsert: {backend}")

    membership = await get_kb_membership(user_id, workspace_id, kb_id)
    if membership is None:
        raise RuntimeError("KB membership upsert failed to persist.")
    return MembershipWriteResult(membership=membership, created=existing is None)


async def delete_workspace_membership(user_id: str, workspace_id: str) -> bool:
    backend = _require_db_backend()

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"""
                    DELETE FROM {KB_MEMBER_TABLE_NAME}
                    WHERE user_id = :user_id AND workspace_id = :workspace_id
                    """
                ),
                {"user_id": user_id, "workspace_id": workspace_id},
            )
            deleted = await conn.execute(
                text(
                    f"""
                    DELETE FROM {WORKSPACE_MEMBER_TABLE_NAME}
                    WHERE user_id = :user_id AND workspace_id = :workspace_id
                    """
                ),
                {"user_id": user_id, "workspace_id": workspace_id},
            )
        return int(deleted.rowcount or 0) > 0

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()

        def _sqlite_delete_workspace_membership() -> bool:
            with sqlite3.connect(sqlite_path) as conn:
                conn.execute(
                    f"""
                    DELETE FROM {KB_MEMBER_TABLE_NAME}
                    WHERE user_id = ? AND workspace_id = ?
                    """,
                    (user_id, workspace_id),
                )
                cursor = conn.execute(
                    f"""
                    DELETE FROM {WORKSPACE_MEMBER_TABLE_NAME}
                    WHERE user_id = ? AND workspace_id = ?
                    """,
                    (user_id, workspace_id),
                )
                conn.commit()
                return int(cursor.rowcount or 0) > 0

        return await asyncio.to_thread(_sqlite_delete_workspace_membership)

    raise RuntimeError(
        f"Unsupported DB backend for workspace membership delete: {backend}"
    )


async def delete_kb_membership(user_id: str, workspace_id: str, kb_id: str) -> bool:
    backend = _require_db_backend()

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.begin() as conn:
            deleted = await conn.execute(
                text(
                    f"""
                    DELETE FROM {KB_MEMBER_TABLE_NAME}
                    WHERE user_id = :user_id AND workspace_id = :workspace_id AND kb_id = :kb_id
                    """
                ),
                {
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                    "kb_id": kb_id,
                },
            )
        return int(deleted.rowcount or 0) > 0

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        deleted = await asyncio.to_thread(
            _sqlite_execute,
            sqlite_path,
            f"""
            DELETE FROM {KB_MEMBER_TABLE_NAME}
            WHERE user_id = ? AND workspace_id = ? AND kb_id = ?
            """,
            (user_id, workspace_id, kb_id),
        )
        return deleted > 0

    raise RuntimeError(f"Unsupported DB backend for KB membership delete: {backend}")


def _build_refresh_issue(
    *,
    token_jti: str,
    expires_in_hours: float,
    rotated_from_token_id: str | None = None,
) -> tuple[RefreshTokenIssue, str]:
    issued_at = _utcnow().isoformat()
    expires_at = (_utcnow() + timedelta(hours=expires_in_hours)).isoformat()
    issue = RefreshTokenIssue(
        token_id=str(uuid4()),
        raw_token=_generate_refresh_token(),
        token_jti=token_jti,
        issued_at=issued_at,
        expires_at=expires_at,
        rotated_from_token_id=rotated_from_token_id,
    )
    return issue, _hash_refresh_token(issue.raw_token)


def _sqlite_insert_refresh_issue(
    path: str,
    issue: RefreshTokenIssue,
    token_hash: str,
    user_id: str,
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            f"""
            INSERT INTO {REFRESH_TOKEN_TABLE_NAME} (
                id, user_id, token_jti, token_hash, issued_at, expires_at,
                revoked_at, rotated_from_token_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue.token_id,
                user_id,
                issue.token_jti,
                token_hash,
                issue.issued_at,
                issue.expires_at,
                None,
                issue.rotated_from_token_id,
                issue.issued_at,
            ),
        )
        conn.commit()


async def issue_refresh_token(
    user_id: str,
    *,
    token_jti: str,
    expires_in_hours: float,
    rotated_from_token_id: str | None = None,
) -> RefreshTokenIssue:
    backend = _require_db_backend()
    issue, token_hash = _build_refresh_issue(
        token_jti=token_jti,
        expires_in_hours=expires_in_hours,
        rotated_from_token_id=rotated_from_token_id,
    )

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"""
                    INSERT INTO {REFRESH_TOKEN_TABLE_NAME} (
                        id, user_id, token_jti, token_hash, issued_at, expires_at,
                        revoked_at, rotated_from_token_id, created_at
                    ) VALUES (
                        :id, :user_id, :token_jti, :token_hash, :issued_at, :expires_at,
                        :revoked_at, :rotated_from_token_id, :created_at
                    )
                    """
                ),
                {
                    "id": issue.token_id,
                    "user_id": user_id,
                    "token_jti": issue.token_jti,
                    "token_hash": token_hash,
                    "issued_at": issue.issued_at,
                    "expires_at": issue.expires_at,
                    "revoked_at": None,
                    "rotated_from_token_id": issue.rotated_from_token_id,
                    "created_at": issue.issued_at,
                },
            )
        return issue

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        await asyncio.to_thread(
            _sqlite_insert_refresh_issue,
            sqlite_path,
            issue,
            token_hash,
            user_id,
        )
        return issue

    raise RuntimeError(f"Unsupported DB backend for refresh token issue: {backend}")


def _validate_refresh_row(row, now: datetime) -> None:
    if row is None:
        raise RefreshTokenValidationError("Invalid refresh token")
    if row["revoked_at"]:
        raise RefreshTokenValidationError("Refresh token has been revoked")
    if _parse_iso_datetime(row["expires_at"]) <= now:
        raise RefreshTokenValidationError("Refresh token expired")


def _validate_identity_user(user: IdentityUserRecord | None) -> IdentityUserRecord:
    if user is None:
        raise RefreshTokenValidationError("Invalid refresh token")
    if not user.is_active:
        raise RefreshTokenValidationError("User account is inactive", status_code=403)
    return user


def _sqlite_rotate_refresh_token(
    path: str,
    old_token_hash: str,
    issue: RefreshTokenIssue,
    new_token_hash: str,
) -> tuple[sqlite3.Row, sqlite3.Row, list[sqlite3.Row]]:
    refresh_sql = f"""
    SELECT id, user_id, token_jti, token_hash, issued_at, expires_at, revoked_at,
           rotated_from_token_id, created_at
    FROM {REFRESH_TOKEN_TABLE_NAME}
    WHERE token_hash = ?
    LIMIT 1
    """
    user_sql = f"""
    SELECT id, username, source, is_active
    FROM {USER_TABLE_NAME}
    WHERE id = ?
    LIMIT 1
    """
    membership_sql = f"""
    SELECT workspace_id, NULL AS kb_id, role
    FROM {WORKSPACE_MEMBER_TABLE_NAME}
    WHERE user_id = ?
    UNION ALL
    SELECT workspace_id, kb_id, role
    FROM {KB_MEMBER_TABLE_NAME}
    WHERE user_id = ?
    ORDER BY workspace_id, kb_id, role
    """

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        old_row = conn.execute(refresh_sql, (old_token_hash,)).fetchone()
        _validate_refresh_row(old_row, _utcnow())

        user_row = conn.execute(user_sql, (old_row["user_id"],)).fetchone()
        user = _validate_identity_user(_build_identity_user_record(user_row))
        membership_rows = conn.execute(
            membership_sql,
            (user.user_id, user.user_id),
        ).fetchall()
        issue.rotated_from_token_id = old_row["id"]

        conn.execute(
            f"UPDATE {REFRESH_TOKEN_TABLE_NAME} SET revoked_at = ? WHERE id = ?",
            (issue.issued_at, old_row["id"]),
        )
        conn.execute(
            f"""
            INSERT INTO {REFRESH_TOKEN_TABLE_NAME} (
                id, user_id, token_jti, token_hash, issued_at, expires_at,
                revoked_at, rotated_from_token_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue.token_id,
                user.user_id,
                issue.token_jti,
                new_token_hash,
                issue.issued_at,
                issue.expires_at,
                None,
                issue.rotated_from_token_id,
                issue.issued_at,
            ),
        )
        conn.commit()

    return old_row, user_row, membership_rows


async def rotate_refresh_token(
    raw_refresh_token: str,
    *,
    new_token_jti: str,
    expires_in_hours: float,
) -> RefreshTokenRotationResult:
    backend = _require_db_backend()
    old_token_hash = _hash_refresh_token(raw_refresh_token)
    issue, new_token_hash = _build_refresh_issue(
        token_jti=new_token_jti,
        expires_in_hours=expires_in_hours,
    )

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.begin() as conn:
            refresh_result = await conn.execute(
                text(
                    f"""
                    SELECT id, user_id, token_jti, token_hash, issued_at, expires_at,
                           revoked_at, rotated_from_token_id, created_at
                    FROM {REFRESH_TOKEN_TABLE_NAME}
                    WHERE token_hash = :token_hash
                    LIMIT 1
                    """
                ),
                {"token_hash": old_token_hash},
            )
            old_row = refresh_result.mappings().first()
            _validate_refresh_row(old_row, _utcnow())

            user_result = await conn.execute(
                text(
                    f"""
                    SELECT id, username, source, is_active
                    FROM {USER_TABLE_NAME}
                    WHERE id = :user_id
                    LIMIT 1
                    """
                ),
                {"user_id": old_row["user_id"]},
            )
            user_row = user_result.mappings().first()
            user = _validate_identity_user(_build_identity_user_record(user_row))

            membership_result = await conn.execute(
                text(
                    f"""
                    SELECT workspace_id, NULL AS kb_id, role
                    FROM {WORKSPACE_MEMBER_TABLE_NAME}
                    WHERE user_id = :user_id
                    UNION ALL
                    SELECT workspace_id, kb_id, role
                    FROM {KB_MEMBER_TABLE_NAME}
                    WHERE user_id = :user_id
                    ORDER BY workspace_id, kb_id, role
                    """
                ),
                {"user_id": user.user_id},
            )
            membership_rows = membership_result.mappings().all()

            issue.rotated_from_token_id = old_row["id"]

            await conn.execute(
                text(
                    f"""
                    UPDATE {REFRESH_TOKEN_TABLE_NAME}
                    SET revoked_at = :revoked_at
                    WHERE id = :token_id
                    """
                ),
                {"revoked_at": issue.issued_at, "token_id": old_row["id"]},
            )
            await conn.execute(
                text(
                    f"""
                    INSERT INTO {REFRESH_TOKEN_TABLE_NAME} (
                        id, user_id, token_jti, token_hash, issued_at, expires_at,
                        revoked_at, rotated_from_token_id, created_at
                    ) VALUES (
                        :id, :user_id, :token_jti, :token_hash, :issued_at, :expires_at,
                        :revoked_at, :rotated_from_token_id, :created_at
                    )
                    """
                ),
                {
                    "id": issue.token_id,
                    "user_id": user.user_id,
                    "token_jti": issue.token_jti,
                    "token_hash": new_token_hash,
                    "issued_at": issue.issued_at,
                    "expires_at": issue.expires_at,
                    "revoked_at": None,
                    "rotated_from_token_id": issue.rotated_from_token_id,
                    "created_at": issue.issued_at,
                },
            )

        return RefreshTokenRotationResult(
            user=user,
            memberships=_normalize_membership_rows(membership_rows),
            previous_token_id=old_row["id"],
            issued_refresh_token=issue,
        )

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        old_row, user_row, membership_rows = await asyncio.to_thread(
            _sqlite_rotate_refresh_token,
            sqlite_path,
            old_token_hash,
            issue,
            new_token_hash,
        )
        return RefreshTokenRotationResult(
            user=_validate_identity_user(_build_identity_user_record(user_row)),
            memberships=_normalize_membership_rows(membership_rows),
            previous_token_id=old_row["id"],
            issued_refresh_token=issue,
        )

    raise RuntimeError(f"Unsupported DB backend for refresh token rotation: {backend}")


async def revoke_refresh_token(raw_refresh_token: str) -> bool:
    """Revoke a single refresh token if it exists."""
    backend = _require_db_backend()
    token_hash = _hash_refresh_token(raw_refresh_token)
    revoked_at = _utcnow_iso()

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    f"""
                    UPDATE {REFRESH_TOKEN_TABLE_NAME}
                    SET revoked_at = COALESCE(revoked_at, :revoked_at)
                    WHERE token_hash = :token_hash
                    """
                ),
                {"revoked_at": revoked_at, "token_hash": token_hash},
            )
        return bool(result.rowcount)

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()

        def _sqlite_revoke_refresh_token() -> bool:
            with sqlite3.connect(sqlite_path) as conn:
                cursor = conn.execute(
                    f"""
                    UPDATE {REFRESH_TOKEN_TABLE_NAME}
                    SET revoked_at = COALESCE(revoked_at, ?)
                    WHERE token_hash = ?
                    """,
                    (revoked_at, token_hash),
                )
                conn.commit()
                return bool(cursor.rowcount)

        return await asyncio.to_thread(_sqlite_revoke_refresh_token)

    raise RuntimeError(f"Unsupported DB backend for refresh-token revocation: {backend}")


async def revoke_refresh_tokens_for_user(user_id: str) -> int:
    """Revoke every refresh token issued to a user."""
    backend = _require_db_backend()
    revoked_at = _utcnow_iso()

    if backend == "sqlalchemy":
        from sqlalchemy import text

        engine = get_engine()
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    f"""
                    UPDATE {REFRESH_TOKEN_TABLE_NAME}
                    SET revoked_at = COALESCE(revoked_at, :revoked_at)
                    WHERE user_id = :user_id
                    """
                ),
                {"revoked_at": revoked_at, "user_id": user_id},
            )
        return int(result.rowcount or 0)

    if backend == "sqlite3":
        sqlite_path = get_sqlite_path()

        def _sqlite_revoke_refresh_tokens_for_user() -> int:
            with sqlite3.connect(sqlite_path) as conn:
                cursor = conn.execute(
                    f"""
                    UPDATE {REFRESH_TOKEN_TABLE_NAME}
                    SET revoked_at = COALESCE(revoked_at, ?)
                    WHERE user_id = ?
                    """,
                    (revoked_at, user_id),
                )
                conn.commit()
                return int(cursor.rowcount or 0)

        return await asyncio.to_thread(_sqlite_revoke_refresh_tokens_for_user)

    raise RuntimeError(f"Unsupported DB backend for refresh-token revocation: {backend}")
