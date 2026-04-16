import asyncio
import base64
import importlib
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from starlette.datastructures import UploadFile as StarletteUploadFile

sys.argv = sys.argv[:1]

from lightrag.base import DocStatus  # noqa: E402
from lightrag.lightrag import (  # noqa: E402
    LightRAG,
    _build_multimodal_rebuild_status_record,
    _caption_json_has_meaningful_summary,
    _dedupe_extracted_images_for_multimodal_ingest,
    _image_annotation_to_text,
)


def _reload_document_routes():
    from lightrag.api.routers import document_routes

    return importlib.reload(document_routes)


def _get_rebuild_endpoint(mod, rag, doc_manager):
    router = mod.create_document_routes(rag, doc_manager, api_key=None)
    for route in router.routes:
        if getattr(route, "path", "").endswith("/{doc_id}/rebuild_multimodal"):
            return route.endpoint
    raise AssertionError("rebuild_multimodal endpoint not found")


def _get_upload_endpoint(mod, rag, doc_manager):
    router = mod.create_document_routes(rag, doc_manager, api_key=None)
    for route in router.routes:
        if getattr(route, "path", "").endswith("/upload"):
            return route.endpoint
    raise AssertionError("upload endpoint not found")


class DummyRAG:
    def __init__(self, doc_record):
        self.image_embedding_func = object()
        self.images_vdb = object()
        self.image_blob_store = object()
        self.image_metadata = object()
        self.workspace = "test"
        self.doc_status = AsyncMock()
        self.doc_status.get_by_id = AsyncMock(return_value=doc_record)
        self.doc_status.get_doc_by_file_path = AsyncMock(return_value=doc_record)
        self.doc_status.get_docs_by_statuses = AsyncMock(return_value={})
        self.doc_status.upsert = AsyncMock()
        self.doc_status.index_done_callback = AsyncMock()
        self.full_docs = AsyncMock()
        self.full_docs.get_by_id = AsyncMock(
            return_value={"content": "existing", "file_path": doc_record.get("file_path")}
        )
        self.full_docs.upsert = AsyncMock()
        self.full_docs.index_done_callback = AsyncMock()
        self.arebuild_document_multimodal = AsyncMock()
        self._get_existing_image_ids_for_doc = AsyncMock(return_value=[])
        self.areconstruct_document_multimodal_payload = AsyncMock()


@pytest.mark.offline
@pytest.mark.asyncio
async def test_rebuild_multimodal_endpoint_rejects_non_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mod = _reload_document_routes()
    rag = DummyRAG({"file_path": "notes.txt"})
    endpoint = _get_rebuild_endpoint(mod, rag, mod.DocumentManager(str(tmp_path)))

    with pytest.raises(HTTPException) as exc:
        await endpoint(
            "doc-1", mod.RebuildMultimodalRequest(), BackgroundTasks()
        )

    assert exc.value.status_code == 400
    assert "Only PDF documents" in str(exc.value.detail)


@pytest.mark.offline
@pytest.mark.asyncio
async def test_rebuild_multimodal_endpoint_rejects_busy_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mod = _reload_document_routes()
    source_pdf = tmp_path / "report.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\n")
    rag = DummyRAG({"file_path": "report.pdf"})
    endpoint = _get_rebuild_endpoint(mod, rag, mod.DocumentManager(str(tmp_path)))

    monkeypatch.setattr(
        "lightrag.kg.shared_storage.get_namespace_data",
        AsyncMock(return_value={"busy": True}),
    )
    monkeypatch.setattr(
        "lightrag.kg.shared_storage.get_namespace_lock",
        lambda *args, **kwargs: asyncio.Lock(),
    )

    with pytest.raises(HTTPException) as exc:
        await endpoint(
            "doc-1", mod.RebuildMultimodalRequest(), BackgroundTasks()
        )

    assert exc.value.status_code == 409
    assert "Pipeline is busy" in str(exc.value.detail)


@pytest.mark.offline
@pytest.mark.asyncio
async def test_rebuild_multimodal_endpoint_allows_existing_asset_fallback_when_pdf_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mod = _reload_document_routes()
    rag = DummyRAG({"file_path": "missing.pdf"})
    rag._get_existing_image_ids_for_doc = AsyncMock(return_value=["img-1"])
    endpoint = _get_rebuild_endpoint(mod, rag, mod.DocumentManager(str(tmp_path)))

    monkeypatch.setattr(mod, "generate_track_id", lambda prefix: f"{prefix}-track")
    monkeypatch.setattr(
        "lightrag.kg.shared_storage.get_namespace_data",
        AsyncMock(return_value={"busy": False}),
    )
    monkeypatch.setattr(
        "lightrag.kg.shared_storage.get_namespace_lock",
        lambda *args, **kwargs: asyncio.Lock(),
    )

    background_tasks = BackgroundTasks()
    response = await endpoint(
        "doc-1",
        mod.RebuildMultimodalRequest(reuse_cache=True),
        background_tasks,
    )

    assert response.status == "rebuild_started"
    assert len(background_tasks.tasks) == 1


@pytest.mark.offline
@pytest.mark.asyncio
async def test_rebuild_multimodal_endpoint_schedules_background_task_and_reuses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mod = _reload_document_routes()
    enqueued_dir = tmp_path / "__enqueued__"
    enqueued_dir.mkdir()
    queued_pdf = enqueued_dir / "floorplan_001.pdf"
    queued_pdf.write_bytes(b"%PDF-1.4\n")

    rag = DummyRAG({"file_path": "uploads/floorplan.pdf"})
    endpoint = _get_rebuild_endpoint(mod, rag, mod.DocumentManager(str(tmp_path)))

    monkeypatch.setattr(mod, "generate_track_id", lambda prefix: f"{prefix}-track")
    monkeypatch.setattr(
        "lightrag.kg.shared_storage.get_namespace_data",
        AsyncMock(return_value={"busy": False}),
    )
    monkeypatch.setattr(
        "lightrag.kg.shared_storage.get_namespace_lock",
        lambda *args, **kwargs: asyncio.Lock(),
    )

    background_tasks = BackgroundTasks()
    response = await endpoint(
        "doc-1",
        mod.RebuildMultimodalRequest(reuse_cache=True),
        background_tasks,
    )

    assert response.status == "rebuild_started"
    assert response.track_id == "rebuild_multimodal-track"
    assert response.doc_id == "doc-1"
    assert len(background_tasks.tasks) == 1
    scheduled = background_tasks.tasks[0]
    assert scheduled.func is mod.background_rebuild_document_multimodal
    assert scheduled.args[2] == "doc-1"
    assert scheduled.args[3] == "rebuild_multimodal-track"
    assert scheduled.args[4] is True


@pytest.mark.offline
@pytest.mark.asyncio
async def test_background_rebuild_document_multimodal_passes_original_logical_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mod = _reload_document_routes()
    enqueued_dir = tmp_path / "__enqueued__"
    enqueued_dir.mkdir()
    queued_pdf = enqueued_dir / "drawing_002.pdf"
    queued_pdf.write_bytes(b"%PDF-1.4\n")

    rag = DummyRAG({"file_path": "nested/drawing.pdf"})
    monkeypatch.setattr(
        mod,
        "_convert_with_docling_multimodal",
        lambda file_path: (
            "drawing text",
            [{"bytes": b"img", "mime_type": "image/png", "page_no": 1}],
        ),
    )

    await mod.background_rebuild_document_multimodal(
        rag,
        mod.DocumentManager(str(tmp_path)),
        "doc-1",
        "track-1",
        reuse_cache=False,
    )

    rag.arebuild_document_multimodal.assert_awaited_once()
    kwargs = rag.arebuild_document_multimodal.await_args.kwargs
    assert kwargs["doc_id"] == "doc-1"
    assert kwargs["file_path"] == "drawing.pdf"
    assert kwargs["reuse_existing_images"] is False
    assert kwargs["track_id"] == "track-1"


@pytest.mark.offline
@pytest.mark.asyncio
async def test_background_rebuild_document_multimodal_falls_back_to_existing_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mod = _reload_document_routes()
    rag = DummyRAG({"file_path": "missing.pdf"})
    rag.areconstruct_document_multimodal_payload = AsyncMock(
        return_value=(
            "stored text",
            [{"bytes": b"img", "mime_type": "image/png", "page_no": 2}],
        )
    )

    await mod.background_rebuild_document_multimodal(
        rag,
        mod.DocumentManager(str(tmp_path)),
        "doc-1",
        "track-1",
        reuse_cache=True,
    )

    rag.areconstruct_document_multimodal_payload.assert_awaited_once_with("doc-1")
    rag.arebuild_document_multimodal.assert_awaited_once()
    kwargs = rag.arebuild_document_multimodal.await_args.kwargs
    assert kwargs["text_content"] == "stored text"
    assert kwargs["file_path"] == "missing.pdf"


@pytest.mark.offline
def test_build_multimodal_rebuild_status_record_marks_processing():
    record = _build_multimodal_rebuild_status_record(
        {
            "status": DocStatus.PROCESSED,
            "content_summary": "summary",
            "content_length": 12,
            "created_at": "2026-04-12T00:00:00+00:00",
            "updated_at": "2026-04-12T00:00:00+00:00",
            "file_path": "report.pdf",
            "track_id": "upload-old",
            "metadata": {"image_ids": ["img-1"]},
        },
        track_id="rebuild-1",
        file_path="report.pdf",
        stage="extracting_source_pdf",
    )

    assert record["status"] == DocStatus.PROCESSING
    assert record["track_id"] == "rebuild-1"
    assert record["metadata"]["multimodal_rebuild_in_progress"] is True
    assert record["metadata"]["multimodal_rebuild_stage"] == "extracting_source_pdf"


@pytest.mark.offline
def test_caption_json_has_meaningful_summary_requires_actual_description():
    assert (
        _caption_json_has_meaningful_summary(
            {
                "image_category": "Engineering Drawing",
                "caption": "Basement floor plan with room labels.",
                "detailed_description": "",
                "detected_entities": [],
            }
        )
        is True
    )
    assert (
        _caption_json_has_meaningful_summary(
            {
                "image_category": "Engineering Drawing",
                "caption": "",
                "detailed_description": "",
                "detected_entities": [],
                "key_attributes": {},
            }
        )
        is False
    )


@pytest.mark.offline
@pytest.mark.asyncio
async def test_background_rebuild_document_multimodal_marks_processing_before_extract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mod = _reload_document_routes()
    source_pdf = tmp_path / "report.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\n")
    rag = DummyRAG({"file_path": "report.pdf", "status": "processed", "metadata": {}})
    monkeypatch.setattr(
        mod,
        "_convert_with_docling_multimodal",
        lambda file_path: (
            "report text",
            [{"bytes": b"img", "mime_type": "image/png", "page_no": 1}],
        ),
    )

    await mod.background_rebuild_document_multimodal(
        rag,
        mod.DocumentManager(str(tmp_path)),
        "doc-1",
        "track-1",
        reuse_cache=True,
    )

    first_upsert = rag.doc_status.upsert.await_args_list[0].args[0]["doc-1"]
    assert first_upsert["status"] == DocStatus.PROCESSING
    assert first_upsert["metadata"]["multimodal_rebuild_in_progress"] is True
    assert first_upsert["metadata"]["multimodal_rebuild_stage"] == "extracting_source_pdf"


@pytest.mark.offline
@pytest.mark.asyncio
async def test_background_rebuild_document_multimodal_releases_claimed_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mod = _reload_document_routes()
    source_pdf = tmp_path / "replacement.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\nreplacement\n")
    rag = DummyRAG({"file_path": "replacement.pdf"})
    release_calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        mod,
        "_convert_with_docling_multimodal",
        lambda file_path: (
            "replacement text",
            [{"bytes": b"img", "mime_type": "image/png", "page_no": 1}],
        ),
    )

    async def fake_release(_rag, filename, owner=None):
        release_calls.append((filename, owner))

    monkeypatch.setattr(mod, "_release_input_file_claim", fake_release)

    await mod.background_rebuild_document_multimodal(
        rag,
        mod.DocumentManager(str(tmp_path)),
        "doc-1",
        "track-1",
        reuse_cache=True,
        release_input_claim_filename="replacement.pdf",
    )

    assert release_calls == [("replacement.pdf", "track-1")]


@pytest.mark.offline
@pytest.mark.asyncio
async def test_arebuild_document_multimodal_reinserts_processing_placeholder():
    existing_status = {
        "status": DocStatus.PROCESSED,
        "content_summary": "summary",
        "content_length": 24,
        "created_at": "2026-04-12T00:00:00+00:00",
        "updated_at": "2026-04-12T00:00:00+00:00",
        "file_path": "report.pdf",
        "track_id": "upload-old",
        "chunks_count": 2,
        "chunks_list": ["chunk-1", "chunk-2"],
        "metadata": {"image_ids": ["img-old"]},
    }
    fake_rag = SimpleNamespace(
        workspace="test",
        doc_status=SimpleNamespace(
            get_by_id=AsyncMock(return_value=existing_status),
            upsert=AsyncMock(),
            index_done_callback=AsyncMock(),
        ),
        full_docs=SimpleNamespace(
            get_by_id=AsyncMock(return_value={"content": "old text", "file_path": "report.pdf"}),
            upsert=AsyncMock(),
            index_done_callback=AsyncMock(),
        ),
        adelete_by_doc_id=AsyncMock(
            return_value=SimpleNamespace(status="success", message="ok")
        ),
        ainsert_document_with_images=AsyncMock(return_value="rebuild-track"),
        _delete_image_side_resources=AsyncMock(),
    )

    result = await LightRAG.arebuild_document_multimodal(
        fake_rag,
        doc_id="doc-1",
        text_content="new text",
        extracted_images=[{"bytes": b"img", "mime_type": "image/png"}],
        file_path="report.pdf",
        track_id="rebuild-track",
        reuse_existing_images=True,
    )

    assert result == "rebuild-track"
    placeholder = fake_rag.doc_status.upsert.await_args_list[0].args[0]["doc-1"]
    assert placeholder["status"] == DocStatus.PROCESSING
    assert placeholder["metadata"]["multimodal_rebuild_in_progress"] is True
    kwargs = fake_rag.ainsert_document_with_images.await_args.kwargs
    assert kwargs["allow_existing_doc_id"] is True
    assert kwargs["initial_doc_status"] == DocStatus.PROCESSING
    assert kwargs["initial_doc_metadata"]["multimodal_rebuild_in_progress"] is True


@pytest.mark.offline
@pytest.mark.asyncio
async def test_attach_context_chunks_to_extracted_images_adds_nearby_chunks():
    fake_rag = SimpleNamespace(
        workspace="test",
        tokenizer=None,
        chunk_overlap_token_size=100,
        chunk_token_size=1200,
        chunking_func=lambda tokenizer, content, *_args: [
            {
                "content": "Section intro and overview for the drawing package.",
                "chunk_order_index": 0,
            },
            {
                "content": (
                    "Pump pressure figure shows the discharge curve, valve status, "
                    "and maintenance notes for the basement system."
                ),
                "chunk_order_index": 1,
            },
            {
                "content": "Following section documents inspection steps and safety notes.",
                "chunk_order_index": 2,
            },
        ],
    )
    extracted_images = [
        {
            "page_no": 4,
            "page_text_excerpt": (
                "Pump pressure figure shows the discharge curve, valve status, "
                "and maintenance notes."
            ),
            "context_text": (
                "[当前页 p.4]\nPump pressure figure shows the discharge curve, valve "
                "status, and maintenance notes."
            ),
        }
    ]

    await LightRAG._attach_context_chunks_to_extracted_images(
        fake_rag, "ignored text body", extracted_images
    )

    assert extracted_images[0]["context_chunk_ids"]
    assert len(extracted_images[0]["context_chunks"]) >= 2
    assert any(
        "Pump pressure figure shows the discharge curve"
        in chunk["content"]
        for chunk in extracted_images[0]["context_chunks"]
    )


@pytest.mark.offline
def test_image_annotation_to_text_includes_context_chunks():
    annotation = _image_annotation_to_text(
        {
            "image_category": "Engineering Drawing",
            "caption": "Basement system figure.",
            "detailed_description": "A detailed engineering drawing.",
        },
        blob_id="img-123",
        file_path="drawing.pdf",
        extra_metadata={
            "source_page": 3,
            "context_chunks": [
                {
                    "chunk_id": "chunk-1",
                    "chunk_order_index": 0,
                    "content": "The basement system figure describes valve routing.",
                }
            ],
            "context_text": "[当前页 p.3]\nValve routing and equipment labels.",
        },
    )

    assert "【图像邻近上下文块】" in annotation
    assert "Chunk #1" in annotation
    assert "valve routing" in annotation.lower()


@pytest.mark.offline
def test_image_annotation_to_text_can_exclude_context_for_doc_append():
    annotation = _image_annotation_to_text(
        {
            "image_category": "Engineering Drawing",
            "caption": "Kitchen plan diagram.",
            "detailed_description": "A detailed kitchen floor plan.",
        },
        blob_id="img-ctxless",
        file_path="drawing.pdf",
        extra_metadata={
            "source_page": 66,
            "source_printed_page": 61,
            "source_page_label": "61",
            "native_xref": 654,
            "merged_extraction_modes": [
                "pymupdf_native_image",
                "pymupdf_image_block",
            ],
            "context_chunks": [
                {
                    "chunk_id": "chunk-1",
                    "chunk_order_index": 0,
                    "content": "Overall kitchen system notes and nearby text.",
                }
            ],
            "context_text": "[当前页 p.66/页码61]\nOverall kitchen system notes.",
        },
        include_context=False,
    )

    assert "PDF物理第 66 页" in annotation
    assert "文档页码 61" in annotation
    assert "【PDF原生图像】 xref=654" in annotation
    assert "【合并提取来源】" in annotation
    assert "【图像邻近上下文】" not in annotation
    assert "【图像邻近上下文块】" not in annotation


@pytest.mark.offline
def test_dedupe_extracted_images_for_multimodal_ingest_merges_duplicate_routes():
    deduped = _dedupe_extracted_images_for_multimodal_ingest(
        [
            {
                "bytes": b"same-image",
                "mime_type": "image/png",
                "page_no": 66,
                "source_printed_page": 61,
                "source_page_label": "61",
                "bbox": {"x0": 100.2, "y0": 120.1, "x1": 260.0, "y1": 300.0},
                "extraction_mode": "pymupdf_native_image",
                "native_xref": 654,
                "merged_extraction_modes": ["pymupdf_native_image"],
            },
            {
                "bytes": b"same-image",
                "mime_type": "image/png",
                "page_no": 66,
                "source_printed_page": 61,
                "bbox": {"x0": 100.2, "y0": 120.1, "x1": 260.0, "y1": 300.0},
                "extraction_mode": "pymupdf_image_block",
                "merged_extraction_modes": ["pymupdf_image_block"],
                "context_chunk_ids": ["chunk-1"],
            },
        ]
    )

    assert len(deduped) == 1
    assert deduped[0]["source_printed_page"] == 61
    assert deduped[0]["source_page_label"] == "61"
    assert deduped[0]["native_xref"] == 654
    assert set(deduped[0]["merged_extraction_modes"]) == {
        "pymupdf_native_image",
        "pymupdf_image_block",
    }


@pytest.mark.offline
@pytest.mark.asyncio
async def test_is_file_path_still_referenced_detects_other_docs(tmp_path: Path):
    mod = _reload_document_routes()
    rag = DummyRAG({"file_path": "shared.pdf"})
    rag.doc_status.get_docs_by_statuses = AsyncMock(
        return_value={
            "doc-keep": SimpleNamespace(file_path="shared.pdf"),
            "doc-delete": SimpleNamespace(file_path="shared.pdf"),
        }
    )

    assert (
        await mod._is_file_path_still_referenced(
            rag, "shared.pdf", excluding_doc_ids={"doc-delete"}
        )
        is True
    )
    assert (
        await mod._is_file_path_still_referenced(
            rag, "shared.pdf", excluding_doc_ids={"doc-delete", "doc-keep"}
        )
        is False
    )


@pytest.mark.offline
def test_should_add_page_raster_fallback_when_exact_crops_are_too_small():
    mod = _reload_document_routes()

    assert (
        mod._should_add_page_raster_fallback(
            exact_picture_count=0,
            exact_picture_coverage_ratio=0.0,
            max_exact_picture_ratio=0.0,
        )
        is True
    )
    assert (
        mod._should_add_page_raster_fallback(
            exact_picture_count=1,
            exact_picture_coverage_ratio=0.18,
            max_exact_picture_ratio=0.18,
        )
        is True
    )
    assert (
        mod._should_add_page_raster_fallback(
            exact_picture_count=1,
            exact_picture_coverage_ratio=0.88,
            max_exact_picture_ratio=0.88,
        )
        is False
    )


@pytest.mark.offline
def test_resolve_multimodal_max_images_auto_expands_for_large_documents():
    mod = _reload_document_routes()

    assert mod._resolve_multimodal_max_images(12, None) == 256
    assert mod._resolve_multimodal_max_images(150, None) == 512
    assert mod._resolve_multimodal_max_images(300, None) == 768
    assert mod._resolve_multimodal_max_images(462, None) == 1024
    assert mod._resolve_multimodal_max_images(462, 320) == 320


@pytest.mark.offline
def test_convert_with_docling_multimodal_extracts_pymupdf_native_images(tmp_path: Path):
    mod = _reload_document_routes()
    pdf_path = tmp_path / "native-image.pdf"
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcCAr7LZ/kAAAAASUVORK5CYII="
    )

    import fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 72), "Section drawing")
    page.insert_text((72, 810), "61")
    page.insert_image(fitz.Rect(120, 150, 320, 300), stream=png_bytes)
    doc.save(pdf_path)
    doc.close()

    text_content, extracted_images = mod._convert_with_docling_multimodal(
        pdf_path, page_range=(1, 1), max_images=32
    )

    assert "Section drawing" in text_content
    native_images = [
        img for img in extracted_images if img.get("extraction_mode") == "pymupdf_native_image"
    ]
    assert native_images, "Expected PyMuPDF native-image extraction to produce candidates"
    assert native_images[0]["page_no"] == 1
    assert native_images[0]["source_printed_page"] == 61
    assert native_images[0]["source_page_label"] == "61"
    assert native_images[0]["native_xref"] is not None


@pytest.mark.offline
@pytest.mark.asyncio
async def test_get_existing_image_ids_for_doc_filters_stale_doc_status_ids():
    fake_rag = SimpleNamespace(
        workspace="test",
        doc_status=SimpleNamespace(
            get_by_id=AsyncMock(
                return_value={
                    "metadata": {"image_ids": ["img-stale", "img-live"]},
                }
            )
        ),
        image_metadata=SimpleNamespace(
            get_by_ids=AsyncMock(return_value=[None, {"source_doc_id": "doc-1"}])
        ),
    )

    image_ids = await LightRAG._get_existing_image_ids_for_doc(fake_rag, "doc-1")

    assert image_ids == ["img-live"]


@pytest.mark.offline
@pytest.mark.asyncio
async def test_upload_endpoint_reuses_existing_pdf_doc_for_multimodal_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mod = _reload_document_routes()
    doc_manager = mod.DocumentManager(str(tmp_path))
    existing_doc = {
        "file_path": "report.pdf",
        "status": "processed",
        "track_id": "upload-old",
        "updated_at": "2026-04-12T19:00:00+00:00",
    }
    rag = DummyRAG(existing_doc)
    rag.doc_status.get_docs_by_statuses = AsyncMock(return_value={"doc-1": existing_doc})
    endpoint = _get_upload_endpoint(mod, rag, doc_manager)

    monkeypatch.setattr(mod, "generate_track_id", lambda prefix: f"{prefix}-track")

    async def fake_claim(_rag, filename, owner):
        return True, None

    async def fake_release(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mod, "_claim_input_file", fake_claim)
    monkeypatch.setattr(mod, "_release_input_file_claim", fake_release)
    monkeypatch.setattr(mod, "_ensure_pipeline_not_busy", AsyncMock())

    upload = StarletteUploadFile(
        filename="report.pdf",
        file=BytesIO(b"%PDF-1.4\nreplacement source\n"),
    )
    background_tasks = BackgroundTasks()

    response = await endpoint(background_tasks, upload)

    assert response.status == "success"
    assert response.track_id == "rebuild_multimodal-track"
    assert response.doc_id == "doc-1"
    assert response.operation_metadata == {
        "operation": "multimodal_takeover_rebuild",
        "target_doc_id": "doc-1",
        "file_name": "report.pdf",
        "previous_status": "processed",
        "target_status": "processing",
        "multimodal_rebuild_stage": "queued_for_rebuild",
    }
    assert "instead of creating a duplicate" in response.message
    assert "Processing list" in response.message
    assert (tmp_path / "report.pdf").read_bytes() == b"%PDF-1.4\nreplacement source\n"
    assert (tmp_path / "report.pdf") in doc_manager.indexed_files
    assert len(background_tasks.tasks) == 1
    scheduled = background_tasks.tasks[0]
    assert scheduled.func is mod.background_rebuild_document_multimodal
    assert scheduled.args[2] == "doc-1"
    assert scheduled.args[3] == "rebuild_multimodal-track"
    assert scheduled.args[4] is True
    assert scheduled.args[5] == "report.pdf"


@pytest.mark.offline
@pytest.mark.asyncio
async def test_upload_endpoint_reuses_preprocessed_pdf_doc_for_multimodal_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mod = _reload_document_routes()
    doc_manager = mod.DocumentManager(str(tmp_path))
    existing_doc = {
        "file_path": "report.pdf",
        "status": "preprocessed",
        "track_id": "upload-old",
        "updated_at": "2026-04-12T19:00:00+00:00",
    }
    rag = DummyRAG(existing_doc)
    rag.doc_status.get_docs_by_statuses = AsyncMock(return_value={"doc-1": existing_doc})
    endpoint = _get_upload_endpoint(mod, rag, doc_manager)

    monkeypatch.setattr(mod, "generate_track_id", lambda prefix: f"{prefix}-track")
    monkeypatch.setattr(mod, "_claim_input_file", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(mod, "_release_input_file_claim", AsyncMock())
    monkeypatch.setattr(mod, "_ensure_pipeline_not_busy", AsyncMock())

    upload = StarletteUploadFile(
        filename="report.pdf",
        file=BytesIO(b"%PDF-1.4\nreplacement source\n"),
    )
    background_tasks = BackgroundTasks()

    response = await endpoint(background_tasks, upload)

    assert response.status == "success"
    assert response.track_id == "rebuild_multimodal-track"
    assert response.doc_id == "doc-1"
    assert response.operation_metadata == {
        "operation": "multimodal_takeover_rebuild",
        "target_doc_id": "doc-1",
        "file_name": "report.pdf",
        "previous_status": "preprocessed",
        "target_status": "processing",
        "multimodal_rebuild_stage": "queued_for_rebuild",
    }
    assert len(background_tasks.tasks) == 1
    scheduled = background_tasks.tasks[0]
    assert scheduled.func is mod.background_rebuild_document_multimodal
    assert scheduled.args[2] == "doc-1"


@pytest.mark.offline
@pytest.mark.asyncio
async def test_upload_endpoint_keeps_duplicate_behavior_for_busy_existing_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mod = _reload_document_routes()
    doc_manager = mod.DocumentManager(str(tmp_path))
    existing_doc = {
        "file_path": "report.pdf",
        "status": "processing",
        "track_id": "upload-old",
    }
    rag = DummyRAG(existing_doc)
    endpoint = _get_upload_endpoint(mod, rag, doc_manager)
    upload = StarletteUploadFile(
        filename="report.pdf",
        file=BytesIO(b"%PDF-1.4\nreplacement source\n"),
    )
    background_tasks = BackgroundTasks()

    response = await endpoint(background_tasks, upload)

    assert response.status == "duplicated"
    assert response.track_id == "upload-old"
    assert not background_tasks.tasks
    assert not (tmp_path / "report.pdf").exists()
