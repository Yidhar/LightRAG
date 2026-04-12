import sys
from pathlib import Path

import pytest

sys.argv = sys.argv[:1]

from lightrag.api.routers import document_routes  # noqa: E402
from lightrag.base import DocStatus  # noqa: E402


class DummyRAG:
    def __init__(self):
        self.process_calls = 0

    async def apipeline_process_enqueue_documents(self):
        self.process_calls += 1


def test_document_manager_scan_skips_temp_files_and_accepts_images(tmp_path: Path):
    doc_manager = document_routes.DocumentManager(str(tmp_path))
    (tmp_path / "__tmp__upload.png").write_bytes(b"partial")
    (tmp_path / "diagram.png").write_bytes(b"image-bytes")
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")

    discovered = sorted(path.name for path in doc_manager.scan_directory_for_new_files())

    assert "__tmp__upload.png" not in discovered
    assert "diagram.png" in discovered
    assert "notes.txt" in discovered


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (DocStatus.PENDING, True),
        (DocStatus.PROCESSING, True),
        (DocStatus.PREPROCESSED, True),
        (DocStatus.PROCESSED, True),
        (DocStatus.FAILED, False),
        ("failed", False),
        ("pending", True),
        (None, False),
    ],
)
def test_should_skip_scan_for_status(status, expected):
    assert document_routes._should_skip_scan_for_status(status) is expected


@pytest.mark.asyncio
async def test_pipeline_index_file_skips_when_file_already_claimed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    file_path = tmp_path / "claimed.txt"
    file_path.write_text("claimed", encoding="utf-8")

    events: list[tuple[str, str, str]] = []

    async def fake_claim(rag, filename, owner):
        events.append(("claim", filename, owner))
        return False, "scan-owner"

    async def fake_release(rag, filename, owner=None):
        events.append(("release", filename, owner or ""))

    async def fake_enqueue(*args, **kwargs):
        raise AssertionError("pipeline_enqueue_file should not run when claim fails")

    monkeypatch.setattr(document_routes, "_claim_input_file", fake_claim)
    monkeypatch.setattr(document_routes, "_release_input_file_claim", fake_release)
    monkeypatch.setattr(document_routes, "pipeline_enqueue_file", fake_enqueue)

    success = await document_routes.pipeline_index_file(
        DummyRAG(), file_path, track_id="upload-1"
    )

    assert success is False
    assert events == [("claim", "claimed.txt", "upload-1")]


@pytest.mark.asyncio
async def test_pipeline_index_file_releases_preclaimed_file_after_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    file_path = tmp_path / "image.png"
    file_path.write_bytes(b"image")

    events: list[tuple[str, str, str]] = []

    async def fake_release(rag, filename, owner=None):
        events.append(("release", filename, owner or ""))

    async def fake_enqueue(rag, path, track_id):
        events.append(("enqueue", path.name, track_id))
        return True, track_id

    rag = DummyRAG()

    monkeypatch.setattr(document_routes, "_release_input_file_claim", fake_release)
    monkeypatch.setattr(document_routes, "pipeline_enqueue_file", fake_enqueue)

    success = await document_routes.pipeline_index_file(
        rag, file_path, track_id="upload-2", preclaimed=True
    )

    assert success is True
    assert rag.process_calls == 1
    assert events == [
        ("enqueue", "image.png", "upload-2"),
        ("release", "image.png", "upload-2"),
    ]
