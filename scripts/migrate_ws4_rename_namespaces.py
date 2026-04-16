"""
WS4 migration helper: copy legacy file-backend storage into workspace__default layout.

This script intentionally uses a copy-first strategy:

- detect legacy root/workspace file-backed storage layouts
- copy artifacts into the combined workspace + default-KB namespace
- verify copied bytes via SHA-256 before optionally removing sources
- remain idempotent when rerun
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from lightrag.api.config import sanitize_platform_identifier

LEGACY_STORAGE_FILE_PATTERNS = (
    "kv_store_*.json",
    "graph_*.graphml",
    "vdb_*.json",
)
ROOT_SPECIAL_DIRS = ("blobs",)
IGNORED_DIRECTORY_NAMES = {
    "__pycache__",
    ".git",
    ".github",
    ".venv",
    "node_modules",
}
LEGACY_BLOB_DEFAULT_DIRNAME = "_default"


@dataclass(slots=True)
class FileOperation:
    source: Path
    destination: Path
    category: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy legacy LightRAG file-backed storage into the workspace__default "
            "layout required by WS4 KB isolation."
        )
    )
    parser.add_argument(
        "--working-dir",
        required=True,
        help="LightRAG working directory containing legacy storage files.",
    )
    parser.add_argument(
        "--default-workspace-id",
        default="default",
        help="Workspace id to use for root-level legacy artifacts.",
    )
    parser.add_argument(
        "--default-kb-id",
        default="default",
        help="Default KB id to append during migration.",
    )
    parser.add_argument(
        "--kb-separator",
        default="__",
        help="Combined workspace/KB separator used by WS4 compatibility mode.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Describe planned file copies without writing anything.",
    )
    parser.add_argument(
        "--remove-source",
        action="store_true",
        help="Delete source files/directories after verified copy.",
    )
    return parser.parse_args()


def sanitize_identifier(value: str, *, label: str) -> str:
    sanitized = sanitize_platform_identifier(value, label=label)
    if not sanitized:
        raise ValueError(f"{label} must not be empty.")
    return sanitized


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def collect_legacy_file_operations(
    working_dir: Path,
    *,
    default_workspace_id: str,
    default_kb_id: str,
    kb_separator: str,
) -> list[FileOperation]:
    operations: list[FileOperation] = []
    default_combined_workspace = (
        f"{default_workspace_id}{kb_separator}{default_kb_id}"
    )
    combined_default_suffix = f"{kb_separator}{default_kb_id}" if kb_separator else ""

    # Root-level legacy files (workspace="").
    for pattern in LEGACY_STORAGE_FILE_PATTERNS:
        for source in sorted(working_dir.glob(pattern)):
            destination = working_dir / default_combined_workspace / source.name
            operations.append(
                FileOperation(
                    source=source,
                    destination=destination,
                    category="root_file",
                )
            )

    # Legacy workspace directories.
    for child in sorted(working_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name in IGNORED_DIRECTORY_NAMES or child.name in ROOT_SPECIAL_DIRS:
            continue

        if combined_default_suffix and child.name.endswith(combined_default_suffix):
            # Already in the combined workspace format; do not remigrate.
            continue

        workspace_id = sanitize_identifier(child.name, label="workspace directory")
        destination_workspace = f"{workspace_id}{kb_separator}{default_kb_id}"
        for pattern in LEGACY_STORAGE_FILE_PATTERNS:
            for source in sorted(child.glob(pattern)):
                destination = working_dir / destination_workspace / source.name
                operations.append(
                    FileOperation(
                        source=source,
                        destination=destination,
                        category="workspace_file",
                    )
                )

    blobs_root = working_dir / "blobs"
    if not blobs_root.exists():
        return operations

    for child in sorted(blobs_root.iterdir()):
        if not child.is_dir():
            continue

        if child.name == LEGACY_BLOB_DEFAULT_DIRNAME:
            destination_workspace = default_combined_workspace
        elif combined_default_suffix and child.name.endswith(combined_default_suffix):
            continue
        else:
            workspace_id = sanitize_identifier(child.name, label="blob workspace")
            destination_workspace = f"{workspace_id}{kb_separator}{default_kb_id}"

        for source in sorted(path for path in child.rglob("*") if path.is_file()):
            destination = blobs_root / destination_workspace / source.relative_to(child)
            operations.append(
                FileOperation(
                    source=source,
                    destination=destination,
                    category="blob_file",
                )
            )

    return operations


def remove_empty_ancestors(path: Path, *, stop_at: Path) -> None:
    current = path
    while current != stop_at and stop_at in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def get_source_workspace_id(
    operation: FileOperation,
    *,
    working_dir: Path,
    default_workspace_id: str,
) -> str:
    if operation.category == "root_file":
        return default_workspace_id
    if operation.category == "workspace_file":
        workspace_dir = operation.source.parent.name
        return sanitize_identifier(workspace_dir, label="workspace directory")
    if operation.category == "blob_file":
        relative_parts = operation.source.relative_to(working_dir / "blobs").parts
        workspace_dir = relative_parts[0]
        if workspace_dir == LEGACY_BLOB_DEFAULT_DIRNAME:
            return default_workspace_id
        return sanitize_identifier(workspace_dir, label="blob workspace")
    return default_workspace_id


def apply_file_operations(
    operations: list[FileOperation],
    *,
    dry_run: bool,
    remove_source: bool,
    working_dir: Path,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "dry_run": dry_run,
        "remove_source": remove_source,
        "planned_count": len(operations),
        "copied": [],
        "skipped_identical": [],
        "removed_sources": [],
        "conflicts": [],
    }

    for operation in operations:
        source = operation.source
        destination = operation.destination
        relative_source = str(source.relative_to(working_dir))
        relative_destination = str(destination.relative_to(working_dir))

        if dry_run:
            cast_list(summary["copied"]).append(
                {
                    "source": relative_source,
                    "destination": relative_destination,
                    "category": operation.category,
                    "status": "planned",
                }
            )
            continue

        if destination.exists():
            source_hash = hash_file(source)
            destination_hash = hash_file(destination)
            if source_hash == destination_hash:
                cast_list(summary["skipped_identical"]).append(
                    {
                        "source": relative_source,
                        "destination": relative_destination,
                        "category": operation.category,
                    }
                )
                if remove_source and source.exists():
                    source.unlink()
                    cast_list(summary["removed_sources"]).append(relative_source)
                    stop_at = (
                        working_dir / "blobs"
                        if operation.category == "blob_file"
                        else working_dir
                    )
                    remove_empty_ancestors(source.parent, stop_at=stop_at)
                continue

            cast_list(summary["conflicts"]).append(
                {
                    "source": relative_source,
                    "destination": relative_destination,
                    "category": operation.category,
                }
            )
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        source_hash = hash_file(source)
        destination_hash = hash_file(destination)
        if source_hash != destination_hash:
            raise RuntimeError(
                f"Hash verification failed for {relative_source} -> {relative_destination}"
            )

        cast_list(summary["copied"]).append(
            {
                "source": relative_source,
                "destination": relative_destination,
                "category": operation.category,
                "status": "copied",
            }
        )
        if remove_source:
            source.unlink()
            cast_list(summary["removed_sources"]).append(relative_source)
            stop_at = (
                working_dir / "blobs"
                if operation.category == "blob_file"
                else working_dir
            )
            remove_empty_ancestors(source.parent, stop_at=stop_at)

    summary["copied_count"] = len(summary["copied"])
    summary["skipped_count"] = len(summary["skipped_identical"])
    summary["removed_count"] = len(summary["removed_sources"])
    summary["conflict_count"] = len(summary["conflicts"])
    return summary


def cast_list(value: object) -> list:
    return value if isinstance(value, list) else []


def build_summary(
    working_dir: Path,
    *,
    default_workspace_id: str,
    default_kb_id: str,
    kb_separator: str,
    dry_run: bool,
    remove_source: bool,
    operations: list[FileOperation],
    execution_summary: dict[str, object],
) -> dict[str, object]:
    source_workspaces = sorted(
        {
            get_source_workspace_id(
                operation,
                working_dir=working_dir,
                default_workspace_id=default_workspace_id,
            )
            for operation in operations
        }
    )
    return {
        "working_dir": str(working_dir),
        "default_workspace_id": default_workspace_id,
        "default_kb_id": default_kb_id,
        "kb_separator": kb_separator,
        "dry_run": dry_run,
        "remove_source": remove_source,
        "source_workspaces": source_workspaces,
        **execution_summary,
    }


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

    operations = collect_legacy_file_operations(
        working_dir,
        default_workspace_id=default_workspace_id,
        default_kb_id=default_kb_id,
        kb_separator=kb_separator,
    )
    execution_summary = apply_file_operations(
        operations,
        dry_run=bool(args.dry_run),
        remove_source=bool(args.remove_source),
        working_dir=working_dir,
    )
    summary = build_summary(
        working_dir,
        default_workspace_id=default_workspace_id,
        default_kb_id=default_kb_id,
        kb_separator=kb_separator,
        dry_run=bool(args.dry_run),
        remove_source=bool(args.remove_source),
        operations=operations,
        execution_summary=execution_summary,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))

    if summary["conflict_count"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
