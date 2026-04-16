import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.offline

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "migrate_ws4_rename_namespaces.py"


def write_text_file(path: Path, content: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_binary_file(path: Path, content: bytes = b"blob") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )


def test_namespace_rename_script_dry_run_reports_root_files_and_default_blobs(tmp_path):
    write_text_file(tmp_path / "kv_store_full_docs.json")
    write_text_file(
        tmp_path / "blobs" / "_default" / "image_blobs" / "ab" / "img-1.meta.json",
        '{"blob_id":"img-1"}',
    )
    write_binary_file(
        tmp_path / "blobs" / "_default" / "image_blobs" / "ab" / "img-1.png",
        b"pngdata",
    )

    result = run_script(
        "--working-dir",
        str(tmp_path),
        "--default-workspace-id",
        "core-team",
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)

    assert summary["dry_run"] is True
    assert summary["source_workspaces"] == ["core_team"]
    assert summary["planned_count"] == 3
    assert not (tmp_path / "core_team__default").exists()
    assert not (tmp_path / "blobs" / "core_team__default").exists()


def test_namespace_rename_script_copies_workspace_files_idempotently(tmp_path):
    write_text_file(tmp_path / "team-alpha" / "kv_store_doc_status.json", '{"a":1}')
    write_text_file(tmp_path / "team-alpha" / "vdb_entities.json", '{"b":2}')

    first = run_script(
        "--working-dir",
        str(tmp_path),
    )
    second = run_script(
        "--working-dir",
        str(tmp_path),
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    first_summary = json.loads(first.stdout)
    second_summary = json.loads(second.stdout)

    assert first_summary["copied_count"] == 2
    assert first_summary["skipped_count"] == 0
    assert second_summary["copied_count"] == 0
    assert second_summary["skipped_count"] == 2
    assert (tmp_path / "team_alpha__default" / "kv_store_doc_status.json").exists()
    assert (tmp_path / "team_alpha__default" / "vdb_entities.json").exists()
    assert (tmp_path / "team-alpha" / "kv_store_doc_status.json").exists()


def test_namespace_rename_script_can_remove_verified_sources(tmp_path):
    write_text_file(tmp_path / "kv_store_full_docs.json", '{"root":true}')
    write_text_file(
        tmp_path / "blobs" / "_default" / "image_blobs" / "cd" / "img-2.meta.json",
        '{"blob_id":"img-2"}',
    )
    write_binary_file(
        tmp_path / "blobs" / "_default" / "image_blobs" / "cd" / "img-2.webp",
        b"webpdata",
    )

    result = run_script(
        "--working-dir",
        str(tmp_path),
        "--remove-source",
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)

    assert summary["removed_count"] == 3
    assert not (tmp_path / "kv_store_full_docs.json").exists()
    assert not (
        tmp_path / "blobs" / "_default" / "image_blobs" / "cd" / "img-2.meta.json"
    ).exists()
    assert (tmp_path / "default__default" / "kv_store_full_docs.json").exists()
    assert (
        tmp_path
        / "blobs"
        / "default__default"
        / "image_blobs"
        / "cd"
        / "img-2.webp"
    ).exists()
