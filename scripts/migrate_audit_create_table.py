"""
Migration: create the audit_log table (PR-AUDIT-1).

This is the first of three audit-log rollout PRs (see
``docs/platform-v2/audit-log-design.md``). It is intentionally narrow in
scope:

- creates the audit_log table and its four indexes
- idempotent — re-running is safe and reports ``existed``
- SQLite-only path for now; PostgreSQL support lands with PR-AUDIT-2/3
  alongside the emit and query paths that need to write to it
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from lightrag.api.audit_store import (
    AUDIT_LOG_TABLE_NAME,
    apply_audit_schema_sqlite,
    audit_schema_ddl,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the audit_log table in the platform database.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--sqlite-path",
        help="Raw SQLite database path.",
    )
    target.add_argument(
        "--db-url",
        help=(
            "Database URL (sqlite:///... or sqlite+aiosqlite:///... only in "
            "PR-AUDIT-1). PostgreSQL support lands with PR-AUDIT-2/3."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report DDL that would run without writing to the database.",
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
        "error: --db-url only supports sqlite:/// or sqlite+aiosqlite:/// "
        "URLs in PR-AUDIT-1. Pass --sqlite-path for any other layout."
    )


def table_exists(path: str) -> bool:
    if path == ":memory:":
        return False
    target = Path(path)
    if not target.exists():
        return False
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (AUDIT_LOG_TABLE_NAME,),
        )
        return cursor.fetchone() is not None


def main() -> int:
    args = parse_args()
    sqlite_path = resolve_sqlite_path(args)

    existed = table_exists(sqlite_path)
    statements = list(audit_schema_ddl())

    summary: dict[str, object] = {
        "sqlite_path": sqlite_path,
        "dry_run": bool(args.dry_run),
        "table": AUDIT_LOG_TABLE_NAME,
        "existed": existed,
        "statements": statements,
    }

    if not args.dry_run:
        apply_audit_schema_sqlite(sqlite_path)
        summary["applied"] = True
    else:
        summary["applied"] = False

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
