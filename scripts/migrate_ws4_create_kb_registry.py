"""
WS4 migration helper: create default KB registry entries for discovered workspaces.

This script is intentionally conservative:

- it discovers likely legacy workspaces from file-backed storage artifacts
- it creates/ensures only the default KB record per workspace
- it does not rename any data directories or mutate existing storage payloads
- it supports a dry-run mode for operator review
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lightrag.api.config import sanitize_platform_identifier
from lightrag.api.kb_registry import KnowledgeBaseRegistry

LEGACY_STORAGE_FILE_PATTERNS = (
    "kv_store_*.json",
    "graph_*.graphml",
    "vdb_*.json",
)
LEGACY_STORAGE_DIR_NAMES = ("blobs",)
IGNORED_DIRECTORY_NAMES = {
    "__pycache__",
    ".git",
    ".github",
    ".venv",
    "node_modules",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create default knowledge-base registry entries for legacy workspaces."
    )
    parser.add_argument(
        "--working-dir",
        required=True,
        help="LightRAG working directory that contains storage artifacts.",
    )
    parser.add_argument(
        "--default-workspace-id",
        default="default",
        help="Workspace id to use when legacy data exists at the root of the working dir.",
    )
    parser.add_argument(
        "--default-kb-id",
        default="default",
        help="Default KB id to ensure for every discovered workspace.",
    )
    parser.add_argument(
        "--kb-separator",
        default="__",
        help="Combined workspace/KB separator used by WS4 compatibility mode.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing kb_registry.json.",
    )
    return parser.parse_args()


def has_storage_artifacts(directory: Path) -> bool:
    for pattern in LEGACY_STORAGE_FILE_PATTERNS:
        if any(directory.glob(pattern)):
            return True
    for name in LEGACY_STORAGE_DIR_NAMES:
        if (directory / name).exists():
            return True
    return False


def sanitize_identifier(value: str, *, label: str) -> str:
    sanitized = sanitize_platform_identifier(value, label=label)
    if not sanitized:
        raise ValueError(f"{label} must not be empty.")
    return sanitized


def discover_legacy_workspaces(
    working_dir: Path,
    *,
    default_workspace_id: str,
    default_kb_id: str,
    kb_separator: str,
) -> list[str]:
    discovered: set[str] = set()

    if has_storage_artifacts(working_dir):
        discovered.add(default_workspace_id)

    combined_default_suffix = (
        f"{kb_separator}{default_kb_id}" if kb_separator else None
    )

    for child in working_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name in IGNORED_DIRECTORY_NAMES:
            continue
        if child.name in LEGACY_STORAGE_DIR_NAMES:
            # Root-level blob/image store belongs to the default workspace.
            continue
        if not has_storage_artifacts(child):
            continue

        workspace_id = sanitize_identifier(child.name, label="workspace directory")

        if combined_default_suffix and workspace_id.endswith(combined_default_suffix):
            workspace_id = workspace_id[: -len(combined_default_suffix)] or workspace_id

        discovered.add(workspace_id)

    return sorted(discovered)


def apply_registry_migration(
    working_dir: Path,
    *,
    default_workspace_id: str,
    default_kb_id: str,
    kb_separator: str,
    dry_run: bool,
) -> dict[str, object]:
    discovered_workspaces = discover_legacy_workspaces(
        working_dir,
        default_workspace_id=default_workspace_id,
        default_kb_id=default_kb_id,
        kb_separator=kb_separator,
    )

    registry_path = working_dir / KnowledgeBaseRegistry.REGISTRY_FILENAME
    summary: dict[str, object] = {
        "working_dir": str(working_dir),
        "registry_path": str(registry_path),
        "dry_run": dry_run,
        "default_workspace_id": default_workspace_id,
        "default_kb_id": default_kb_id,
        "kb_separator": kb_separator,
        "discovered_workspaces": discovered_workspaces,
        "created_workspaces": [],
        "existing_workspaces": [],
        "created_count": 0,
        "existing_count": 0,
    }

    if dry_run:
        existing_workspaces: list[str] = []
        created_workspaces: list[str] = []
        if registry_path.exists():
            registry = KnowledgeBaseRegistry(working_dir)
            for workspace_id in discovered_workspaces:
                if registry.exists(workspace_id, default_kb_id):
                    existing_workspaces.append(workspace_id)
                else:
                    created_workspaces.append(workspace_id)
        else:
            created_workspaces = list(discovered_workspaces)

        summary["created_workspaces"] = created_workspaces
        summary["existing_workspaces"] = existing_workspaces
        summary["created_count"] = len(created_workspaces)
        summary["existing_count"] = len(existing_workspaces)
        return summary

    registry = KnowledgeBaseRegistry(working_dir)
    created_workspaces: list[str] = []
    existing_workspaces: list[str] = []
    for workspace_id in discovered_workspaces:
        if registry.exists(workspace_id, default_kb_id):
            existing_workspaces.append(workspace_id)
            continue
        registry.ensure_default_kb(workspace_id, default_kb_id)
        created_workspaces.append(workspace_id)

    summary["created_workspaces"] = created_workspaces
    summary["existing_workspaces"] = existing_workspaces
    summary["created_count"] = len(created_workspaces)
    summary["existing_count"] = len(existing_workspaces)
    return summary


def main() -> int:
    args = parse_args()
    working_dir = Path(args.working_dir).resolve()
    default_workspace_id = sanitize_identifier(
        args.default_workspace_id,
        label="default_workspace_id",
    )
    default_kb_id = sanitize_identifier(
        args.default_kb_id,
        label="default_kb_id",
    )
    kb_separator = str(args.kb_separator or "__")

    summary = apply_registry_migration(
        working_dir,
        default_workspace_id=default_workspace_id,
        default_kb_id=default_kb_id,
        kb_separator=kb_separator,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
