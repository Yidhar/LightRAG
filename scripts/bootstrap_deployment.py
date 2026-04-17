"""
Zero-friction first-time setup for a Platform V2 deployment.

Run once on a fresh checkout:

    uv run python scripts/bootstrap_deployment.py

What it does, in order:

1. If ``.env`` is missing, copy ``env.example`` to ``.env`` so the user
   starts from the shipped defaults.
2. Scan ``.env`` for ``TOKEN_SECRET``; if missing / blank / still set to
   the documented placeholder, generate a cryptographically-random 48-byte
   urlsafe value and write it back in place.
3. Ensure the four Platform V2 switches are on: ``USE_DB_AUTH``,
   ``DB_URL``, ``ENABLE_KB_ISOLATION``, ``LIGHTRAG_ALLOW_SELF_REGISTRATION``.
   Existing user values are preserved — we only add missing lines.
4. Call ``init_db`` + ``bootstrap_identity_store`` + ``apply_audit_schema``
   to create every platform table (users, workspaces, memberships, refresh
   tokens, audit log, bootstrap marker). All DDL is ``CREATE TABLE IF NOT
   EXISTS`` so re-running is safe.
5. Print the next step — start the server and create the first admin via
   the login page's 首次使用 banner.

Idempotent. Re-running never destroys user data or overwrites existing
non-placeholder env values.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make the lightrag package importable when the script is run from a
# checkout that hasn't been `pip install`-ed yet.
sys.path.insert(0, str(REPO_ROOT))


PLACEHOLDER_TOKEN_SECRET = "REPLACE_ME_WITH_RANDOM_48_BYTE_TOKEN"
DEFAULT_TOKEN_SECRET = "lightrag-jwt-default-secret-key!"  # matches config.py

# Switches we guarantee in the .env after the script runs. Values are the
# defaults; existing user values win if a key is already present.
REQUIRED_SWITCHES: list[tuple[str, str]] = [
    ("USE_DB_AUTH", "true"),
    ("DB_URL", "sqlite+aiosqlite:///./lightrag_auth.db"),
    ("ENABLE_KB_ISOLATION", "true"),
    ("LIGHTRAG_ALLOW_SELF_REGISTRATION", "true"),
]


def _log(message: str) -> None:
    print(f"[bootstrap] {message}")


def _parse_env(path: Path) -> dict[str, str]:
    """Return active (uncommented) key=value pairs from a .env file."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _write_key(path: Path, key: str, value: str) -> None:
    """Replace ``key=...`` in place, or append if missing.

    Preserves every comment and blank line around the key.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replaced = False

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        head, _, _ = stripped.partition("=")
        if head.strip() == key:
            lines[index] = f"{key}={value}"
            replaced = True
            break

    if not replaced:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"{key}={value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ensure_env_file(env_path: Path, example_path: Path) -> bool:
    """Return True when .env was just copied from env.example."""
    if env_path.exists():
        return False
    if not example_path.exists():
        raise FileNotFoundError(
            f"{example_path} not found. Expected the shipped env.example at "
            "the repo root so bootstrap can copy it to .env."
        )
    shutil.copy(example_path, env_path)
    _log(f"Copied {example_path.name} -> {env_path}")
    return True


def _ensure_token_secret(env_path: Path, current: str | None) -> str:
    """Generate + persist a TOKEN_SECRET when the current value is unsafe."""
    if current and current not in {"", PLACEHOLDER_TOKEN_SECRET, DEFAULT_TOKEN_SECRET}:
        _log("TOKEN_SECRET already set — keeping the existing value.")
        return current
    generated = secrets.token_urlsafe(48)
    _write_key(env_path, "TOKEN_SECRET", generated)
    _log("Generated a new TOKEN_SECRET and wrote it into .env.")
    return generated


def _ensure_switches(env_path: Path, existing: dict[str, str]) -> list[str]:
    """Add every REQUIRED_SWITCHES key that's missing. Returns keys touched."""
    touched: list[str] = []
    for key, default in REQUIRED_SWITCHES:
        if key in existing and existing[key] != "":
            continue
        _write_key(env_path, key, default)
        touched.append(key)
    if touched:
        _log(f"Added missing platform switches: {', '.join(touched)}")
    else:
        _log("All platform switches already present — skipping.")
    return touched


async def _apply_schema(db_url: str) -> None:
    """Run the three schema bootstraps the server would otherwise do on first boot.

    Running it here gives the user early feedback — wrong DB_URL or
    missing async driver errors show up before they start the server.
    """
    from lightrag.api.audit_store import apply_audit_schema
    from lightrag.api.db import get_db_backend, get_engine, get_sqlite_path, init_db
    from lightrag.api.identity_store import bootstrap_identity_store

    await init_db(db_url)
    _log("init_db OK (bootstrap marker table ready).")

    summary = await bootstrap_identity_store(None)
    _log(
        "Identity tables ready "
        f"(env-seed parsed={summary.parsed_count}, "
        f"inserted={summary.inserted_count}, updated={summary.updated_count})."
    )

    # ``apply_audit_schema`` accepts either a SQLAlchemy AsyncEngine or a
    # raw sqlite path (str). Route based on what ``init_db`` actually
    # selected — on hosts without SQLAlchemy installed it falls back to
    # the sqlite3 driver and stores a dict sentinel instead of an engine.
    backend = get_db_backend()
    if backend == "sqlalchemy":
        await apply_audit_schema(get_engine())
    elif backend == "sqlite3":
        sqlite_path = get_sqlite_path()
        if not sqlite_path:
            raise RuntimeError(
                "init_db reported the sqlite3 backend but no on-disk path "
                "was resolved — check DB_URL."
            )
        await apply_audit_schema(sqlite_path)
    else:
        raise RuntimeError(
            f"Unexpected DB backend {backend!r} — cannot apply audit_log schema."
        )
    _log("audit_log schema applied.")


def _print_next_steps(generated_token: bool) -> None:
    banner = "=" * 62
    print("")
    print(banner)
    print("Bootstrap complete.")
    print(banner)
    if generated_token:
        print(
            "NOTE: a fresh TOKEN_SECRET was written into .env. Rotate it\n"
            "      before shipping to production and keep .env out of git."
        )
    print("")
    print("Next steps:")
    print("  1. Review .env (LLM / embedding provider, HOST / PORT).")
    print("  2. Start the API server:")
    print("       uv run lightrag-server")
    print("  3. Open the WebUI and create the first admin account via the")
    print("     \"首次使用：创建管理员账号\" banner on the login page.")
    print("  4. With self-registration on, additional users can then sign")
    print("     up via the 注册 tab — each gets a private workspace.")
    print("")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare a fresh LightRAG checkout for first-time deployment "
            "(Platform V2 defaults)."
        )
    )
    parser.add_argument(
        "--env-file",
        default=str(REPO_ROOT / ".env"),
        help="Path to the .env file to populate. Default: repo-root/.env",
    )
    parser.add_argument(
        "--env-example",
        default=str(REPO_ROOT / "env.example"),
        help="Template copied when .env does not yet exist.",
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help=(
            "Skip the DB schema step (useful if the target DB is not reachable "
            "from the bootstrap host — the server will apply the schema on "
            "its next startup)."
        ),
    )
    args = parser.parse_args()

    env_path = Path(args.env_file)
    example_path = Path(args.env_example)

    copied_from_example = _ensure_env_file(env_path, example_path)

    existing = _parse_env(env_path)
    token_value = existing.get("TOKEN_SECRET")
    generated_token = (
        not token_value
        or token_value in {PLACEHOLDER_TOKEN_SECRET, DEFAULT_TOKEN_SECRET}
    )
    _ensure_token_secret(env_path, token_value)

    # Re-parse after the TOKEN_SECRET write so _ensure_switches sees the
    # canonical state of the file.
    existing = _parse_env(env_path)
    _ensure_switches(env_path, existing)

    # Re-parse a final time so we hand a consistent DB_URL to _apply_schema.
    existing = _parse_env(env_path)
    db_url = existing.get("DB_URL", "sqlite+aiosqlite:///./lightrag_auth.db")

    if args.skip_db:
        _log("Skipping DB schema step (--skip-db).")
    else:
        # Export the env values we just wrote so downstream config reads
        # them. Use setdefault so we never clobber the parent shell.
        for key, value in existing.items():
            os.environ.setdefault(key, value)
        try:
            asyncio.run(_apply_schema(db_url))
        except Exception as exc:  # pragma: no cover — surfacing is the point
            _log(f"DB schema step failed: {exc}")
            _log(
                "The .env file is still in place. Fix DB_URL (or install the "
                "async driver) and re-run this script, or pass --skip-db to "
                "defer schema creation to the server's next startup."
            )
            return 1

    _print_next_steps(generated_token or copied_from_example)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
