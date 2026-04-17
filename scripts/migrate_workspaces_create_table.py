"""
Migration: create the workspaces table (Phase W1).

The new ``workspaces`` table turns workspaces into a first-class entity
so the UI can list, create, rename, and delete them. This script is
idempotent — re-running is safe and will not duplicate rows.

What it does:

1. Creates the ``workspaces`` table (IF NOT EXISTS).
2. Backfills a metadata row for every distinct workspace_id referenced
   by ``workspace_memberships``.
3. Inserts the default workspace record if missing.

For the moment only the sqlite3 backend is supported from the CLI.
PostgreSQL deployments should run the identical DDL through their
migration tooling (the SQL is emitted with ``--dry-run``).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from lightrag.api.models import (
    WORKSPACE_MEMBER_TABLE_NAME,
    WORKSPACE_TABLE_NAME,
)


_CREATE_WORKSPACES_SQL = f"""
CREATE TABLE IF NOT EXISTS {WORKSPACE_TABLE_NAME} (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NULL,
    owner_user_id TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
""".strip()


def workspaces_schema_ddl() -> tuple[str, ...]:
    return (_CREATE_WORKSPACES_SQL,)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the workspaces table and backfill metadata rows.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--sqlite-path", help="Raw SQLite database path.")
    target.add_argument(
        "--db-url",
        help=(
            "Database URL (sqlite:///... or sqlite+aiosqlite:///... only). "
            "PostgreSQL should apply the DDL emitted by --dry-run separately."
        ),
    )
    parser.add_argument(
        "--default-workspace-id",
        default="default",
        help="Workspace id to seed when the table is empty.",
    )
    parser.add_argument(
        "--default-workspace-name",
        default="Default workspace",
        help="Display name used for the seeded default workspace.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report DDL / backfill plan without touching the database.",
    )
    return parser.parse_args()


def resolve_sqlite_path(args: argparse.Namespace) -> str:
    if args.sqlite_path:
        path = Path(args.sqlite_path).expanduser()
        return str(path.resolve()) if str(path) != ":memory:" else ":memory:"

    if args.db_url:
        for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
            if args.db_url.startswith(prefix):
                raw = args.db_url[len(prefix) :]
                if raw == ":memory:":
                    return raw
                return str(Path(raw).expanduser().resolve())

    raise SystemExit(
        "error: --db-url only supports sqlite:/// or sqlite+aiosqlite:/// URLs. "
        "Pass --sqlite-path for any other layout."
    )


def ensure_schema(path: str) -> None:
    with sqlite3.connect(path) as conn:
        for statement in workspaces_schema_ddl():
            conn.execute(statement)
        conn.commit()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def backfill_workspaces(
    path: str,
    *,
    default_workspace_id: str,
    default_workspace_name: str,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "inserted_ids": [],
        "skipped_ids": [],
        "default_inserted": False,
    }

    now = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(path) as conn:
        # Collect distinct workspace_ids from the memberships table (if present).
        distinct_ids: list[str] = []
        if _table_exists(conn, WORKSPACE_MEMBER_TABLE_NAME):
            cursor = conn.execute(
                f"SELECT DISTINCT workspace_id FROM {WORKSPACE_MEMBER_TABLE_NAME}"
            )
            distinct_ids = [row[0] for row in cursor.fetchall() if row[0]]

        if default_workspace_id not in distinct_ids:
            distinct_ids.append(default_workspace_id)

        for workspace_id in distinct_ids:
            cursor = conn.execute(
                f"SELECT id FROM {WORKSPACE_TABLE_NAME} WHERE id = ?",
                (workspace_id,),
            )
            if cursor.fetchone() is not None:
                summary["skipped_ids"].append(workspace_id)
                continue

            # Prefer the requested default metadata for the default id;
            # other backfilled rows inherit a generic name derived from id.
            if workspace_id == default_workspace_id:
                display_name = default_workspace_name
            else:
                display_name = workspace_id

            conn.execute(
                f"""
                INSERT INTO {WORKSPACE_TABLE_NAME}
                (id, name, description, owner_user_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (workspace_id, display_name, None, None, now, now),
            )

            summary["inserted_ids"].append(workspace_id)
            if workspace_id == default_workspace_id:
                summary["default_inserted"] = True

        conn.commit()

    summary["_run_id"] = str(uuid4())
    return summary


def main() -> int:
    args = parse_args()
    sqlite_path = resolve_sqlite_path(args)

    plan: dict[str, object] = {
        "sqlite_path": sqlite_path,
        "dry_run": bool(args.dry_run),
        "table": WORKSPACE_TABLE_NAME,
        "statements": list(workspaces_schema_ddl()),
        "default_workspace_id": args.default_workspace_id,
        "default_workspace_name": args.default_workspace_name,
    }

    if args.dry_run:
        plan["applied"] = False
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    ensure_schema(sqlite_path)
    backfill_summary = backfill_workspaces(
        sqlite_path,
        default_workspace_id=args.default_workspace_id,
        default_workspace_name=args.default_workspace_name,
    )

    plan["applied"] = True
    plan["backfill"] = backfill_summary
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
