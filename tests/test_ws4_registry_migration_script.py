import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.offline

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "migrate_ws4_create_kb_registry.py"


def write_storage_marker(directory: Path, filename: str = "kv_store_full_docs.json") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text("{}", encoding="utf-8")


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )


def test_registry_migration_script_dry_run_discovers_root_and_workspace_dirs(tmp_path):
    write_storage_marker(tmp_path)
    write_storage_marker(tmp_path / "sales-ops")

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
    assert summary["discovered_workspaces"] == ["core_team", "sales_ops"]
    assert summary["created_count"] == 2
    assert not (tmp_path / "kb_registry.json").exists()


def test_registry_migration_script_writes_registry_idempotently(tmp_path):
    write_storage_marker(tmp_path)
    write_storage_marker(tmp_path / "team-alpha")

    first = run_script(
        "--working-dir",
        str(tmp_path),
        "--default-workspace-id",
        "main-team",
    )
    second = run_script(
        "--working-dir",
        str(tmp_path),
        "--default-workspace-id",
        "main-team",
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    first_summary = json.loads(first.stdout)
    second_summary = json.loads(second.stdout)
    registry_payload = json.loads((tmp_path / "kb_registry.json").read_text("utf-8"))

    assert first_summary["created_count"] == 2
    assert second_summary["created_count"] == 0
    assert second_summary["existing_count"] == 2
    assert sorted(registry_payload["workspaces"].keys()) == ["main_team", "team_alpha"]
    assert sorted(registry_payload["workspaces"]["main_team"]["kbs"].keys()) == [
        "default"
    ]
    assert sorted(registry_payload["workspaces"]["team_alpha"]["kbs"].keys()) == [
        "default"
    ]


def test_registry_migration_script_recovers_workspace_from_combined_default_dir(tmp_path):
    write_storage_marker(tmp_path / "finance__default")

    result = run_script(
        "--working-dir",
        str(tmp_path),
        "--default-workspace-id",
        "fallback",
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)

    assert summary["discovered_workspaces"] == ["finance"]
    registry_payload = json.loads((tmp_path / "kb_registry.json").read_text("utf-8"))
    assert sorted(registry_payload["workspaces"].keys()) == ["finance"]
