"""
Wipe the platform identity + workspace + audit tables so a deployment
can re-bootstrap cleanly. Does NOT touch the RAG storage (documents,
chunks, vectors, graph) — that lives under ``working_dir`` and should
be cleaned separately (usually ``rm -rf working_dir/rag_storage`` or
``--clear`` from the Documents UI).

Typical use case: a dev deployment accumulated guest-mode data, and
you want to start from an empty auth surface before running
``POST /auth/bootstrap-admin``.

Usage:

    python scripts/reset_platform_db.py --sqlite-path ./rag_storage/platform.db --confirm

The ``--confirm`` flag is mandatory. Running without it prints what
would be wiped and exits without touching the DB.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


# Tables cleared in dependency order. Table names mirror the schema
# constants declared under ``lightrag/api/models/``; we keep a local
# literal list here so the script stays runnable without importing
# LightRAG's server modules.
_TABLES_TO_CLEAR: tuple[str, ...] = (
    "audit_log",
    "refresh_tokens",
    "kb_memberships",
    "workspace_memberships",
    "users",
    "workspaces",
    "platform_bootstrap_state",
    "kb_registry",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Wipe the platform identity / workspace / audit tables. "
            "RAG data (documents, chunks, vectors, graph) is NOT touched."
        ),
    )
    parser.add_argument(
        "--sqlite-path",
        required=True,
        help="SQLite database path, e.g. ./rag_storage/platform.db",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required to actually run. Without it, prints the plan and exits.",
    )
    return parser.parse_args()


def inspect_row_counts(path: str) -> dict[str, int | str]:
    counts: dict[str, int | str] = {}
    if not Path(path).exists():
        return {table: "(table missing — DB file not found)" for table in _TABLES_TO_CLEAR}

    with sqlite3.connect(path) as conn:
        for table in _TABLES_TO_CLEAR:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                (table,),
            )
            if cursor.fetchone() is None:
                counts[table] = "(absent)"
                continue
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = int(cursor.fetchone()[0])
    return counts


def wipe_tables(path: str) -> dict[str, int]:
    deleted: dict[str, int] = {}
    with sqlite3.connect(path) as conn:
        for table in _TABLES_TO_CLEAR:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                (table,),
            )
            if cursor.fetchone() is None:
                deleted[table] = 0
                continue
            cursor = conn.execute(f"DELETE FROM {table}")
            deleted[table] = int(cursor.rowcount or 0)
        conn.commit()
    return deleted


def main() -> int:
    args = parse_args()
    resolved_path = str(Path(args.sqlite_path).expanduser().resolve())

    pre = inspect_row_counts(resolved_path)

    if not args.confirm:
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "sqlite_path": resolved_path,
                    "row_counts": pre,
                    "note": "Re-run with --confirm to actually wipe these tables.",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if not Path(resolved_path).exists():
        print(
            json.dumps(
                {
                    "status": "noop",
                    "sqlite_path": resolved_path,
                    "note": "DB file does not exist; nothing to wipe.",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    deleted = wipe_tables(resolved_path)
    print(
        json.dumps(
            {
                "status": "wiped",
                "sqlite_path": resolved_path,
                "before": pre,
                "deleted_rows": deleted,
                "note": (
                    "Platform identity/workspace/audit state cleared. Start the server, "
                    "then POST /auth/bootstrap-admin to create the first administrator."
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
