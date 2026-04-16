"""
Minimal database scaffolding for upcoming platform auth/isolation work.

The preferred path uses SQLAlchemy's async engine when available. For lightweight
environments where SQLAlchemy is not installed yet, a sqlite3-based fallback keeps
the PR-1 scaffold usable for the default SQLite bootstrap flow.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

PLATFORM_BOOTSTRAP_TABLE_NAME = "platform_bootstrap_state"
_BOOTSTRAP_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {PLATFORM_BOOTSTRAP_TABLE_NAME} (
    state_key TEXT PRIMARY KEY,
    state_value TEXT NULL
)
""".strip()

_engine: Any = None
_session_factory: Any = None
_engine_backend: str | None = None
_sqlite_path: str | None = None
_Base: type | None = None

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
    from sqlalchemy.orm import DeclarativeBase
else:
    AsyncEngine = Any
    AsyncSession = Any
    async_sessionmaker = Any
    DeclarativeBase = Any


def has_sqlalchemy_support() -> bool:
    try:
        import sqlalchemy  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def get_declarative_base() -> type[DeclarativeBase]:
    """Lazily create the shared SQLAlchemy declarative base when available."""
    global _Base

    if _Base is not None:
        return _Base

    if has_sqlalchemy_support():
        from sqlalchemy.orm import DeclarativeBase as SQLAlchemyDeclarativeBase

        class Base(SQLAlchemyDeclarativeBase):
            pass

    else:

        class Base:
            pass

    _Base = Base
    return _Base


def get_engine() -> AsyncEngine | None:
    return _engine


def get_db_backend() -> str | None:
    return _engine_backend


def get_sqlite_path() -> str | None:
    return _sqlite_path


def is_db_initialized() -> bool:
    return _engine is not None and _session_factory is not None


def _resolve_sqlite_path(db_url: str) -> str:
    prefixes = ("sqlite+aiosqlite:///", "sqlite:///")

    for prefix in prefixes:
        if db_url.startswith(prefix):
            raw_path = unquote(db_url[len(prefix) :])
            if raw_path == ":memory:":
                return raw_path
            return os.path.abspath(raw_path)

    raise RuntimeError(
        "SQLAlchemy is not installed, and the DB fallback only supports "
        "sqlite:/// or sqlite+aiosqlite:/// URLs."
    )


def _ensure_sqlite_bootstrap(path: str) -> None:
    if path != ":memory:":
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    with sqlite3.connect(path) as conn:
        conn.execute(_BOOTSTRAP_CREATE_SQL)
        conn.commit()


async def init_db(db_url: str) -> None:
    """Initialize the DB scaffold and create the bootstrap marker table."""
    global _engine, _session_factory, _engine_backend, _sqlite_path

    if is_db_initialized():
        return

    if has_sqlalchemy_support():
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import (
            AsyncSession as SQLAlchemyAsyncSession,
            async_sessionmaker as sqlalchemy_async_sessionmaker,
            create_async_engine,
        )

        engine = create_async_engine(db_url, future=True)
        session_factory = sqlalchemy_async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=SQLAlchemyAsyncSession,
        )

        async with engine.begin() as conn:
            await conn.execute(text(_BOOTSTRAP_CREATE_SQL))

        _engine = engine
        _session_factory = session_factory
        _engine_backend = "sqlalchemy"
        _sqlite_path = None
        return

    sqlite_path = _resolve_sqlite_path(db_url)
    await asyncio.to_thread(_ensure_sqlite_bootstrap, sqlite_path)

    _engine = {"backend": "sqlite3", "path": sqlite_path}
    _session_factory = True
    _engine_backend = "sqlite3"
    _sqlite_path = sqlite_path


async def close_db() -> None:
    """Dispose the configured engine/session backend and reset markers."""
    global _engine, _session_factory, _engine_backend, _sqlite_path

    if _engine_backend == "sqlalchemy" and _engine is not None:
        await _engine.dispose()

    _engine = None
    _session_factory = None
    _engine_backend = None
    _sqlite_path = None


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a session-like database handle."""
    if _session_factory is None or _engine_backend is None:
        raise RuntimeError(
            "Database session factory is not initialized. "
            "Enable USE_DB_AUTH and run init_db() first."
        )

    if _engine_backend == "sqlalchemy":
        async with _session_factory() as session:
            yield session
        return

    connection = await asyncio.to_thread(sqlite3.connect, _sqlite_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        await asyncio.to_thread(connection.close)
