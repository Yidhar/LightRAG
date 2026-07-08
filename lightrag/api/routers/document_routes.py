"""
This module contains all document-related routes for the LightRAG API.
"""

import asyncio
import mimetypes
import time
from collections import defaultdict
from uuid import uuid4
from functools import lru_cache
from lightrag.utils import logger, get_pinyin_sort_key, performance_timing_log
import aiofiles
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Literal
from io import BytesIO
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.params import Depends as DependsParameter
from fastapi.responses import FileResponse, Response
from urllib.parse import quote as url_quote
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lightrag import LightRAG
from lightrag.api.audit import emit_audit_event
from lightrag.api.dependencies import (
    compose_runtime_workspace,
    get_current_rag,
    get_request_context,
)
from lightrag.base import DeletionResult, DocProcessingStatus, DocStatus
from lightrag.lightrag import _build_multimodal_rebuild_status_record
from lightrag.utils import (
    generate_track_id,
    compute_mdhash_id,
    sanitize_text_for_encoding,
)
from lightrag.api.permissions import Action, require_permission
from ..config import global_args


@lru_cache(maxsize=1)
def _is_docling_available() -> bool:
    """Check if docling is available (cached check).

    This function uses lru_cache to avoid repeated import attempts.
    The result is cached after the first call.

    Returns:
        bool: True if docling is available, False otherwise
    """
    try:
        import docling  # noqa: F401  # type: ignore[import-not-found]

        return True
    except ImportError:
        return False


# Function to format datetime to ISO format string with timezone information
def format_datetime(dt: Any) -> Optional[str]:
    """Format datetime to ISO format string with timezone information

    Args:
        dt: Datetime object, string, or None

    Returns:
        ISO format string with timezone information, or None if input is None
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt

    # Check if datetime object has timezone information
    if isinstance(dt, datetime):
        # If datetime object has no timezone info (naive datetime), add UTC timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

    # Return ISO format string with timezone information
    return dt.isoformat()


router = APIRouter(
    prefix="/documents",
    tags=["documents"],
)

# Temporary file prefix
temp_prefix = "__tmp__"
UNKNOWN_FILE_SOURCE = "unknown_source"
LEGACY_EMPTY_FILE_PATH_SENTINELS = {"", "no-file-path"}
DOCUMENT_SCAN_STATE_NAMESPACE = "document_scan_state"
DIRECT_IMAGE_EXTENSIONS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
DOCUMENT_SCAN_SKIP_STATUSES = frozenset(
    {
        DocStatus.PENDING.value,
        DocStatus.PROCESSING.value,
        DocStatus.PREPROCESSED.value,
        DocStatus.PROCESSED.value,
    }
)
MULTIMODAL_UPLOAD_TAKEOVER_STATUSES = frozenset(
    {
        DocStatus.PREPROCESSED.value,
        DocStatus.PROCESSED.value,
        DocStatus.FAILED.value,
    }
)


def normalize_file_path(file_path: str | None) -> str:
    """Normalize missing document sources to a single non-null sentinel."""
    if file_path is None:
        return UNKNOWN_FILE_SOURCE

    normalized = file_path.strip()
    if normalized in LEGACY_EMPTY_FILE_PATH_SENTINELS:
        return UNKNOWN_FILE_SOURCE

    return normalized


def sanitize_filename(filename: str, input_dir: Path) -> str:
    """
    Sanitize uploaded filename to prevent Path Traversal attacks.

    Args:
        filename: The original filename from the upload
        input_dir: The target input directory

    Returns:
        str: Sanitized filename that is safe to use

    Raises:
        HTTPException: If the filename is unsafe or invalid
    """
    # Basic validation
    if not filename or not filename.strip():
        raise HTTPException(status_code=400, detail="Filename cannot be empty")

    # Remove path separators and traversal sequences
    clean_name = filename.replace("/", "").replace("\\", "")
    clean_name = clean_name.replace("..", "")

    # Remove control characters and null bytes
    clean_name = "".join(c for c in clean_name if ord(c) >= 32 and c != "\x7f")

    # Remove leading/trailing whitespace and dots
    clean_name = clean_name.strip().strip(".")

    # Check if anything is left after sanitization
    if not clean_name:
        raise HTTPException(
            status_code=400, detail="Invalid filename after sanitization"
        )

    # Verify the final path stays within the input directory
    try:
        final_path = (input_dir / clean_name).resolve()
        if not final_path.is_relative_to(input_dir.resolve()):
            raise HTTPException(status_code=400, detail="Unsafe filename detected")
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid filename")

    return clean_name


def _coerce_doc_status_value(status: Any) -> str | None:
    """Normalize enum/string status values to lowercase strings."""
    if isinstance(status, DocStatus):
        return status.value
    if isinstance(status, str):
        return status.replace("DocStatus.", "").strip().lower()
    return None


def _should_skip_scan_for_status(status: Any) -> bool:
    """Return True when a file is already in-flight or completed."""
    normalized_status = _coerce_doc_status_value(status)
    return normalized_status in DOCUMENT_SCAN_SKIP_STATUSES


def _is_multimodal_pipeline_enabled(rag: LightRAG) -> bool:
    return (
        rag.image_embedding_func is not None
        and rag.images_vdb is not None
        and rag.image_blob_store is not None
        and rag.image_metadata is not None
    )


def _get_status_doc_field(status_doc: Any, field_name: str, default: Any = None) -> Any:
    """Read a field from either a doc-status dict or dataclass-like object."""
    if status_doc is None:
        return default
    if isinstance(status_doc, dict):
        return status_doc.get(field_name, default)
    return getattr(status_doc, field_name, default)


async def _find_tracked_document_by_file_path(
    rag: LightRAG, file_path: str
) -> tuple[str | None, dict[str, Any] | None]:
    """Return the best tracked document matching a logical file path.

    ``get_doc_by_file_path`` does not expose the document id, so for workflows
    like in-place multimodal rebuild we need to scan the tracked documents and
    pick the most suitable candidate. Prefer processed / failed documents, then
    the most recently updated record.
    """

    normalized_target = normalize_file_path(file_path)
    all_statuses = [
        DocStatus.PROCESSED,
        DocStatus.FAILED,
        DocStatus.PREPROCESSED,
        DocStatus.PROCESSING,
        DocStatus.PENDING,
    ]
    docs = await rag.doc_status.get_docs_by_statuses(all_statuses)
    if not docs:
        fallback = await rag.doc_status.get_doc_by_file_path(file_path)
        return None, fallback

    status_rank = {
        DocStatus.PROCESSED.value: 50,
        DocStatus.FAILED.value: 40,
        DocStatus.PREPROCESSED.value: 30,
        DocStatus.PROCESSING.value: 20,
        DocStatus.PENDING.value: 10,
    }
    candidates: list[tuple[int, str, str, dict[str, Any]]] = []
    for doc_id, status_doc in docs.items():
        candidate_file_path = normalize_file_path(_extract_doc_status_file_path(status_doc))
        if candidate_file_path != normalized_target:
            continue

        candidate_dict = (
            dict(status_doc)
            if isinstance(status_doc, dict)
            else {
                "file_path": _extract_doc_status_file_path(status_doc),
                "status": _get_status_doc_field(status_doc, "status"),
                "track_id": _get_status_doc_field(status_doc, "track_id"),
                "updated_at": _get_status_doc_field(status_doc, "updated_at"),
                "created_at": _get_status_doc_field(status_doc, "created_at"),
                "metadata": _get_status_doc_field(status_doc, "metadata", {}),
                "error_msg": _get_status_doc_field(status_doc, "error_msg"),
            }
        )
        normalized_status = _coerce_doc_status_value(candidate_dict.get("status"))
        candidates.append(
            (
                status_rank.get(normalized_status or "", 0),
                str(candidate_dict.get("updated_at") or candidate_dict.get("created_at") or ""),
                doc_id,
                candidate_dict,
            )
        )

    if not candidates:
        fallback = await rag.doc_status.get_doc_by_file_path(file_path)
        return None, fallback

    candidates.sort(reverse=True)
    _, _, doc_id, candidate_dict = candidates[0]
    return doc_id, candidate_dict


async def _reconstruct_text_for_file_path(
    rag: LightRAG,
    file_path: str,
    *,
    kb_label: str,
    reason_sink: list[str],
) -> tuple[str | None, str | None, str | None]:
    """Locate the full-text content for ``file_path`` on one RAG instance.

    Returns ``(content, doc_id, kb_label)`` on hit, ``(None, None, None)``
    on miss. Appends a human-readable miss reason to ``reason_sink`` so
    the calling endpoint can emit a single diagnostic log entry after it
    exhausts every KB it tried — instead of spamming one warning per KB.

    ``_find_tracked_document_by_file_path`` is intentionally NOT used:
    its ``get_doc_by_file_path`` fallback branch returns ``(None, data)``
    and loses the doc_id that ``full_docs.get_by_id`` requires. This
    helper does the scan inline and preserves the key.
    """
    normalized_target = normalize_file_path(file_path)
    all_statuses = [
        DocStatus.PROCESSED,
        DocStatus.FAILED,
        DocStatus.PREPROCESSED,
        DocStatus.PROCESSING,
        DocStatus.PENDING,
    ]
    try:
        tracked_docs = await rag.doc_status.get_docs_by_statuses(all_statuses)
    except Exception as exc:
        reason_sink.append(f"kb={kb_label}: get_docs_by_statuses raised {exc!r}")
        return None, None, None

    status_rank_map = {
        DocStatus.PROCESSED.value: 50,
        DocStatus.FAILED.value: 40,
        DocStatus.PREPROCESSED.value: 30,
        DocStatus.PROCESSING.value: 20,
        DocStatus.PENDING.value: 10,
    }
    matching_doc_id: str | None = None
    matching_rank = -1
    for doc_id, status_doc in (tracked_docs or {}).items():
        candidate_file_path = normalize_file_path(
            _extract_doc_status_file_path(status_doc)
        )
        if candidate_file_path != normalized_target:
            continue
        rank = status_rank_map.get(
            _coerce_doc_status_value(
                _get_status_doc_field(status_doc, "status")
            )
            or "",
            0,
        )
        if rank > matching_rank:
            matching_rank = rank
            matching_doc_id = doc_id

    if matching_doc_id is None:
        # Direct index lookup may know about rows dropped from the
        # statuses iteration (legacy / retired status values).
        try:
            direct = await rag.doc_status.get_doc_by_file_path(file_path)
        except Exception as exc:
            reason_sink.append(
                f"kb={kb_label}: get_doc_by_file_path raised {exc!r}"
            )
            direct = None
        if direct is not None:
            direct_fp = direct.get("file_path")
            for doc_id, status_doc in (tracked_docs or {}).items():
                if _extract_doc_status_file_path(status_doc) == direct_fp:
                    matching_doc_id = doc_id
                    break

    if matching_doc_id is None:
        reason_sink.append(
            f"kb={kb_label}: no doc_status row with file_path matching "
            f"'{normalized_target}' (scanned {len(tracked_docs or {})} rows)"
        )
        return None, None, None

    try:
        full_doc = await rag.full_docs.get_by_id(matching_doc_id)
    except Exception as exc:
        reason_sink.append(
            f"kb={kb_label}: full_docs.get_by_id({matching_doc_id}) raised {exc!r}"
        )
        return None, None, None
    content = (
        (full_doc or {}).get("content") if isinstance(full_doc, dict) else None
    )
    if not content:
        reason_sink.append(
            f"kb={kb_label}: doc_id={matching_doc_id} matched but full_docs "
            f"has no content (full_doc={full_doc!r})"
        )
        return None, None, None

    return str(content), matching_doc_id, kb_label


async def _ensure_pipeline_not_busy(
    rag: LightRAG,
    *,
    detail: str = (
        "Pipeline is busy. Please wait for the current task to finish before "
        "rebuilding multimodal data."
    ),
) -> None:
    from lightrag.kg.shared_storage import get_namespace_data, get_namespace_lock

    pipeline_status = await get_namespace_data("pipeline_status", workspace=rag.workspace)
    pipeline_status_lock = get_namespace_lock("pipeline_status", workspace=rag.workspace)
    async with pipeline_status_lock:
        if pipeline_status.get("busy", False):
            raise HTTPException(status_code=409, detail=detail)


def _is_direct_image_extension(ext: str) -> bool:
    return ext.lower() in DIRECT_IMAGE_EXTENSIONS


def _guess_image_mime_type(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    guessed = DIRECT_IMAGE_EXTENSIONS.get(ext)
    if guessed:
        return guessed
    return mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"


async def _get_document_scan_state(rag: LightRAG):
    from lightrag.kg.shared_storage import get_namespace_data, get_namespace_lock

    scan_state = await get_namespace_data(
        DOCUMENT_SCAN_STATE_NAMESPACE, workspace=rag.workspace
    )
    scan_state_lock = get_namespace_lock(
        DOCUMENT_SCAN_STATE_NAMESPACE, workspace=rag.workspace
    )
    return scan_state, scan_state_lock


async def _claim_input_file(
    rag: LightRAG, filename: str, owner: str
) -> tuple[bool, str | None]:
    """Claim an input filename so scan/upload workers cannot process it twice."""
    scan_state, scan_state_lock = await _get_document_scan_state(rag)
    async with scan_state_lock:
        claimed_files = dict(scan_state.get("claimed_files") or {})
        existing_owner = claimed_files.get(filename)
        if existing_owner and existing_owner != owner:
            return False, existing_owner

        claimed_files[filename] = owner
        scan_state["claimed_files"] = claimed_files
        return True, existing_owner


async def _release_input_file_claim(
    rag: LightRAG, filename: str, owner: str | None = None
) -> None:
    """Release a previously claimed input filename."""
    scan_state, scan_state_lock = await _get_document_scan_state(rag)
    async with scan_state_lock:
        claimed_files = dict(scan_state.get("claimed_files") or {})
        existing_owner = claimed_files.get(filename)
        if existing_owner is None:
            return
        if owner is not None and existing_owner != owner:
            return

        claimed_files.pop(filename, None)
        scan_state["claimed_files"] = claimed_files


class ScanResponse(BaseModel):
    """Response model for document scanning operation

    Attributes:
        status: Status of the scanning operation
        message: Optional message with additional details
        track_id: Tracking ID for monitoring scanning progress
    """

    status: Literal["scanning_started"] = Field(
        description="Status of the scanning operation"
    )
    message: Optional[str] = Field(
        default=None, description="Additional details about the scanning operation"
    )
    track_id: str = Field(description="Tracking ID for monitoring scanning progress")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "scanning_started",
                "message": "Scanning process has been initiated in the background",
                "track_id": "scan_20250729_170612_abc123",
            }
        }
    )


class ReprocessResponse(BaseModel):
    """Response model for reprocessing failed documents operation

    Attributes:
        status: Status of the reprocessing operation
        message: Message describing the operation result
        track_id: Always empty string. Reprocessed documents retain their original track_id.
    """

    status: Literal["reprocessing_started"] = Field(
        description="Status of the reprocessing operation"
    )
    message: str = Field(description="Human-readable message describing the operation")
    track_id: str = Field(
        default="",
        description="Always empty string. Reprocessed documents retain their original track_id from initial upload.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "reprocessing_started",
                "message": "Reprocessing of failed documents has been initiated in background",
                "track_id": "",
            }
        }
    )


class RebuildMultimodalRequest(BaseModel):
    """Request model for rebuilding multimodal data for one PDF document."""

    reuse_cache: bool = Field(
        default=True,
        description=(
            "Reuse cached image captions / embeddings whenever the extracted "
            "image blob hash matches an existing record."
        ),
    )


class RebuildMultimodalResponse(BaseModel):
    """Response model for multimodal rebuild operations."""

    status: Literal["rebuild_started"] = Field(
        description="Status of the rebuild operation"
    )
    message: str = Field(description="Human-readable message describing the rebuild")
    track_id: str = Field(description="Tracking ID for the rebuild job")
    doc_id: str = Field(description="Document identifier being rebuilt")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "rebuild_started",
                "message": "Multimodal rebuild has been initiated in the background",
                "track_id": "rebuild_multimodal_20260412_190000_ab12cd",
                "doc_id": "doc-123456",
            }
        }
    )


class CancelPipelineResponse(BaseModel):
    """Response model for pipeline cancellation operation

    Attributes:
        status: Status of the cancellation request
        message: Message describing the operation result
    """

    status: Literal["cancellation_requested", "not_busy"] = Field(
        description="Status of the cancellation request"
    )
    message: str = Field(description="Human-readable message describing the operation")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "cancellation_requested",
                "message": "Pipeline cancellation has been requested. Documents will be marked as FAILED.",
            }
        }
    )


class InsertTextRequest(BaseModel):
    """Request model for inserting a single text document

    Attributes:
        text: The text content to be inserted into the RAG system
        file_source: Source of the text (optional)
    """

    text: str = Field(
        min_length=1,
        description="The text to insert",
    )
    file_source: Optional[str] = Field(
        default=None, min_length=0, description="File Source"
    )

    @field_validator("text", mode="after")
    @classmethod
    def strip_text_after(cls, text: str) -> str:
        return text.strip()

    @field_validator("file_source", mode="before")
    @classmethod
    def normalize_source_before(cls, file_source: Optional[str]) -> str:
        return normalize_file_path(file_source)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "text": "This is a sample text to be inserted into the RAG system.",
                "file_source": "Source of the text (optional)",
            }
        }
    )


class InsertTextsRequest(BaseModel):
    """Request model for inserting multiple text documents

    Attributes:
        texts: List of text contents to be inserted into the RAG system
        file_sources: Sources of the texts (optional)
    """

    texts: list[str] = Field(
        min_length=1,
        description="The texts to insert",
    )
    file_sources: Optional[list[str]] = Field(
        default=None, min_length=0, description="Sources of the texts"
    )

    @field_validator("texts", mode="after")
    @classmethod
    def strip_texts_after(cls, texts: list[str]) -> list[str]:
        return [text.strip() for text in texts]

    @field_validator("file_sources", mode="before")
    @classmethod
    def normalize_sources_before(
        cls, file_sources: Optional[list[str]]
    ) -> Optional[list[str]]:
        if file_sources is None:
            return None

        return [normalize_file_path(file_source) for file_source in file_sources]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "texts": [
                    "This is the first text to be inserted.",
                    "This is the second text to be inserted.",
                ],
                "file_sources": [
                    "First file source (optional)",
                ],
            }
        }
    )


class InsertResponse(BaseModel):
    """Response model for document insertion operations

    Attributes:
        status: Status of the operation (success, duplicated, partial_success, failure)
        message: Detailed message describing the operation result
        track_id: Tracking ID for monitoring processing status
    """

    status: Literal["success", "duplicated", "partial_success", "failure"] = Field(
        description="Status of the operation"
    )
    message: str = Field(description="Message describing the operation result")
    track_id: str = Field(description="Tracking ID for monitoring processing status")
    doc_id: Optional[str] = Field(
        default=None,
        description=(
            "Document identifier associated with the operation. Present for "
            "in-place replacement / takeover flows."
        ),
    )
    operation_metadata: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional structured metadata describing how the upload was "
            "handled (for example, whether it reused an existing document)."
        ),
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "success",
                "message": "File 'document.pdf' uploaded successfully. Processing will continue in background.",
                "track_id": "upload_20250729_170612_abc123",
                "doc_id": None,
                "operation_metadata": None,
            }
        }
    )


class ClearDocumentsResponse(BaseModel):
    """Response model for document clearing operation

    Attributes:
        status: Status of the clear operation
        message: Detailed message describing the operation result
    """

    status: Literal["success", "partial_success", "busy", "fail"] = Field(
        description="Status of the clear operation"
    )
    message: str = Field(description="Message describing the operation result")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "success",
                "message": "All documents cleared successfully. Deleted 15 files.",
            }
        }
    )


class ClearCacheRequest(BaseModel):
    """Request model for clearing cache

    This model is kept for API compatibility but no longer accepts any parameters.
    All cache will be cleared regardless of the request content.
    """

    model_config = ConfigDict(json_schema_extra={"example": {}})


class ClearCacheResponse(BaseModel):
    """Response model for cache clearing operation

    Attributes:
        status: Status of the clear operation
        message: Detailed message describing the operation result
    """

    status: Literal["success", "fail"] = Field(
        description="Status of the clear operation"
    )
    message: str = Field(description="Message describing the operation result")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "success",
                "message": "Successfully cleared cache for modes: ['default', 'naive']",
            }
        }
    )


"""Response model for document status

Attributes:
    id: Document identifier
    content_summary: Summary of document content
    content_length: Length of document content
    status: Current processing status
    created_at: Creation timestamp (ISO format string)
    updated_at: Last update timestamp (ISO format string)
    chunks_count: Number of chunks (optional)
    error: Error message if any (optional)
    metadata: Additional metadata (optional)
    file_path: Path to the document file
"""


class DeleteDocRequest(BaseModel):
    doc_ids: List[str] = Field(..., description="The IDs of the documents to delete.")
    delete_file: bool = Field(
        default=False,
        description="Whether to delete the corresponding file in the upload directory.",
    )
    delete_llm_cache: bool = Field(
        default=False,
        description="Whether to delete cached LLM extraction results for the documents.",
    )

    @field_validator("doc_ids", mode="after")
    @classmethod
    def validate_doc_ids(cls, doc_ids: List[str]) -> List[str]:
        if not doc_ids:
            raise ValueError("Document IDs list cannot be empty")

        validated_ids = []
        for doc_id in doc_ids:
            if not doc_id or not doc_id.strip():
                raise ValueError("Document ID cannot be empty")
            validated_ids.append(doc_id.strip())

        # Check for duplicates
        if len(validated_ids) != len(set(validated_ids)):
            raise ValueError("Document IDs must be unique")

        return validated_ids


class DeleteEntityRequest(BaseModel):
    entity_name: str = Field(..., description="The name of the entity to delete.")

    @field_validator("entity_name", mode="after")
    @classmethod
    def validate_entity_name(cls, entity_name: str) -> str:
        if not entity_name or not entity_name.strip():
            raise ValueError("Entity name cannot be empty")
        return entity_name.strip()


class DeleteRelationRequest(BaseModel):
    source_entity: str = Field(..., description="The name of the source entity.")
    target_entity: str = Field(..., description="The name of the target entity.")

    @field_validator("source_entity", "target_entity", mode="after")
    @classmethod
    def validate_entity_names(cls, entity_name: str) -> str:
        if not entity_name or not entity_name.strip():
            raise ValueError("Entity name cannot be empty")
        return entity_name.strip()


class DocStatusResponse(BaseModel):
    id: str = Field(description="Document identifier")
    content_summary: str = Field(description="Summary of document content")
    content_length: int = Field(description="Length of document content in characters")
    status: DocStatus = Field(description="Current processing status")
    created_at: str = Field(description="Creation timestamp (ISO format string)")
    updated_at: str = Field(description="Last update timestamp (ISO format string)")
    track_id: Optional[str] = Field(
        default=None, description="Tracking ID for monitoring progress"
    )
    chunks_count: Optional[int] = Field(
        default=None, description="Number of chunks the document was split into"
    )
    error_msg: Optional[str] = Field(
        default=None, description="Error message if processing failed"
    )
    metadata: Optional[dict[str, Any]] = Field(
        default=None, description="Additional metadata about the document"
    )
    file_path: str = Field(description="Path to the document file")
    knowledge_base_id: Optional[str] = Field(
        default=None,
        description="KB id that owns this row (populated only on cross-KB aggregated responses)",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "doc_123456",
                "content_summary": "Research paper on machine learning",
                "content_length": 15240,
                "status": "processed",
                "created_at": "2025-03-31T12:34:56",
                "updated_at": "2025-03-31T12:35:30",
                "track_id": "upload_20250729_170612_abc123",
                "chunks_count": 12,
                "error": None,
                "metadata": {"author": "John Doe", "year": 2025},
                "file_path": "research_paper.pdf",
            }
        }
    )


class DocsStatusesResponse(BaseModel):
    """Response model for document statuses

    Attributes:
        statuses: Dictionary mapping document status to lists of document status responses
    """

    statuses: Dict[DocStatus, List[DocStatusResponse]] = Field(
        default_factory=dict,
        description="Dictionary mapping document status to lists of document status responses",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "statuses": {
                    "PENDING": [
                        {
                            "id": "doc_123",
                            "content_summary": "Pending document",
                            "content_length": 5000,
                            "status": "pending",
                            "created_at": "2025-03-31T10:00:00",
                            "updated_at": "2025-03-31T10:00:00",
                            "track_id": "upload_20250331_100000_abc123",
                            "chunks_count": None,
                            "error": None,
                            "metadata": None,
                            "file_path": "pending_doc.pdf",
                        }
                    ],
                    "PREPROCESSED": [
                        {
                            "id": "doc_789",
                            "content_summary": "Document pending final indexing",
                            "content_length": 7200,
                            "status": "preprocessed",
                            "created_at": "2025-03-31T09:30:00",
                            "updated_at": "2025-03-31T09:35:00",
                            "track_id": "upload_20250331_093000_xyz789",
                            "chunks_count": 10,
                            "error": None,
                            "metadata": None,
                            "file_path": "preprocessed_doc.pdf",
                        }
                    ],
                    "PROCESSED": [
                        {
                            "id": "doc_456",
                            "content_summary": "Processed document",
                            "content_length": 8000,
                            "status": "processed",
                            "created_at": "2025-03-31T09:00:00",
                            "updated_at": "2025-03-31T09:05:00",
                            "track_id": "insert_20250331_090000_def456",
                            "chunks_count": 8,
                            "error": None,
                            "metadata": {"author": "John Doe"},
                            "file_path": "processed_doc.pdf",
                        }
                    ],
                }
            }
        }
    )


class TrackStatusResponse(BaseModel):
    """Response model for tracking document processing status by track_id

    Attributes:
        track_id: The tracking ID
        documents: List of documents associated with this track_id
        total_count: Total number of documents for this track_id
        status_summary: Count of documents by status
    """

    track_id: str = Field(description="The tracking ID")
    documents: List[DocStatusResponse] = Field(
        description="List of documents associated with this track_id"
    )
    total_count: int = Field(description="Total number of documents for this track_id")
    status_summary: Dict[str, int] = Field(description="Count of documents by status")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "track_id": "upload_20250729_170612_abc123",
                "documents": [
                    {
                        "id": "doc_123456",
                        "content_summary": "Research paper on machine learning",
                        "content_length": 15240,
                        "status": "PROCESSED",
                        "created_at": "2025-03-31T12:34:56",
                        "updated_at": "2025-03-31T12:35:30",
                        "track_id": "upload_20250729_170612_abc123",
                        "chunks_count": 12,
                        "error": None,
                        "metadata": {"author": "John Doe", "year": 2025},
                        "file_path": "research_paper.pdf",
                    }
                ],
                "total_count": 1,
                "status_summary": {"PROCESSED": 1},
            }
        }
    )


class DocumentsRequest(BaseModel):
    """Request model for paginated document queries

    Attributes:
        status_filter: Filter by document status, None for all statuses
        page: Page number (1-based)
        page_size: Number of documents per page (10-200)
        sort_field: Field to sort by ('created_at', 'updated_at', 'id', 'file_path')
        sort_direction: Sort direction ('asc' or 'desc')
    """

    status_filter: Optional[DocStatus] = Field(
        default=None, description="Filter by document status, None for all statuses"
    )
    page: int = Field(default=1, ge=1, description="Page number (1-based)")
    page_size: int = Field(
        default=50, ge=10, le=200, description="Number of documents per page (10-200)"
    )
    sort_field: Literal["created_at", "updated_at", "id", "file_path"] = Field(
        default="updated_at", description="Field to sort by"
    )
    sort_direction: Literal["asc", "desc"] = Field(
        default="desc", description="Sort direction"
    )
    all_kbs: bool = Field(
        default=False,
        description="Aggregate docs across every KB linked to the current workspace",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status_filter": "PROCESSED",
                "page": 1,
                "page_size": 50,
                "sort_field": "updated_at",
                "sort_direction": "desc",
            }
        }
    )


class PaginationInfo(BaseModel):
    """Pagination information

    Attributes:
        page: Current page number
        page_size: Number of items per page
        total_count: Total number of items
        total_pages: Total number of pages
        has_next: Whether there is a next page
        has_prev: Whether there is a previous page
    """

    page: int = Field(description="Current page number")
    page_size: int = Field(description="Number of items per page")
    total_count: int = Field(description="Total number of items")
    total_pages: int = Field(description="Total number of pages")
    has_next: bool = Field(description="Whether there is a next page")
    has_prev: bool = Field(description="Whether there is a previous page")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "page": 1,
                "page_size": 50,
                "total_count": 150,
                "total_pages": 3,
                "has_next": True,
                "has_prev": False,
            }
        }
    )


class PaginatedDocsResponse(BaseModel):
    """Response model for paginated document queries

    Attributes:
        documents: List of documents for the current page
        pagination: Pagination information
        status_counts: Count of documents by status for all documents
    """

    documents: List[DocStatusResponse] = Field(
        description="List of documents for the current page"
    )
    pagination: PaginationInfo = Field(description="Pagination information")
    status_counts: Dict[str, int] = Field(
        description="Count of documents by status for all documents"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "documents": [
                    {
                        "id": "doc_123456",
                        "content_summary": "Research paper on machine learning",
                        "content_length": 15240,
                        "status": "PROCESSED",
                        "created_at": "2025-03-31T12:34:56",
                        "updated_at": "2025-03-31T12:35:30",
                        "track_id": "upload_20250729_170612_abc123",
                        "chunks_count": 12,
                        "error_msg": None,
                        "metadata": {"author": "John Doe", "year": 2025},
                        "file_path": "research_paper.pdf",
                    }
                ],
                "pagination": {
                    "page": 1,
                    "page_size": 50,
                    "total_count": 150,
                    "total_pages": 3,
                    "has_next": True,
                    "has_prev": False,
                },
                "status_counts": {
                    "PENDING": 10,
                    "PROCESSING": 5,
                    "PREPROCESSED": 5,
                    "PROCESSED": 130,
                    "FAILED": 5,
                },
            }
        }
    )


class StatusCountsResponse(BaseModel):
    """Response model for document status counts

    Attributes:
        status_counts: Count of documents by status
    """

    status_counts: Dict[str, int] = Field(description="Count of documents by status")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status_counts": {
                    "PENDING": 10,
                    "PROCESSING": 5,
                    "PREPROCESSED": 5,
                    "PROCESSED": 130,
                    "FAILED": 5,
                }
            }
        }
    )


class PipelineStatusResponse(BaseModel):
    """Response model for pipeline status

    Attributes:
        autoscanned: Whether auto-scan has started
        busy: Whether the pipeline is currently busy
        job_name: Current job name (e.g., indexing files/indexing texts)
        job_start: Job start time as ISO format string with timezone (optional)
        docs: Total number of documents to be indexed
        batchs: Number of batches for processing documents
        cur_batch: Current processing batch
        request_pending: Flag for pending request for processing
        latest_message: Latest message from pipeline processing
        history_messages: List of history messages
        update_status: Status of update flags for all namespaces
    """

    autoscanned: bool = False
    busy: bool = False
    job_name: str = "Default Job"
    job_start: Optional[str] = None
    docs: int = 0
    batchs: int = 0
    cur_batch: int = 0
    total_chunks: int = 0
    processed_chunks: int = 0
    current_stage: str = ""
    current_stage_label: str = ""
    stage_unit: str = ""
    stage_total: int = 0
    stage_processed: int = 0
    stage_remaining: int = 0
    stage_elapsed_seconds: int = 0
    stage_eta_seconds: int | None = None
    request_pending: bool = False
    latest_message: str = ""
    history_messages: Optional[List[str]] = None
    update_status: Optional[dict] = None

    @field_validator("job_start", mode="before")
    @classmethod
    def parse_job_start(cls, value):
        """Process datetime and return as ISO format string with timezone"""
        return format_datetime(value)

    model_config = ConfigDict(extra="allow")


class DocumentManager:
    def __init__(
        self,
        input_dir: str,
        workspace: str = "",  # New parameter for workspace isolation
        supported_extensions: tuple = (
            ".txt",
            ".md",
            ".mdx",  # MDX (Markdown + JSX)
            ".pdf",
            ".docx",
            ".pptx",
            ".xlsx",
            ".rtf",  # Rich Text Format
            ".odt",  # OpenDocument Text
            ".tex",  # LaTeX
            ".epub",  # Electronic Publication
            ".html",  # HyperText Markup Language
            ".htm",  # HyperText Markup Language
            ".csv",  # Comma-Separated Values
            ".json",  # JavaScript Object Notation
            ".xml",  # eXtensible Markup Language
            ".yaml",  # YAML Ain't Markup Language
            ".yml",  # YAML
            ".log",  # Log files
            ".conf",  # Configuration files
            ".ini",  # Initialization files
            ".properties",  # Java properties files
            ".sql",  # SQL scripts
            ".bat",  # Batch files
            ".sh",  # Shell scripts
            ".c",  # C source code
            ".h",  # C header
            ".cpp",  # C++ source code
            ".hpp",  # C++ header
            ".py",  # Python source code
            ".java",  # Java source code
            ".js",  # JavaScript source code
            ".ts",  # TypeScript source code
            ".swift",  # Swift source code
            ".go",  # Go source code
            ".rb",  # Ruby source code
            ".php",  # PHP source code
            ".css",  # Cascading Style Sheets
            ".scss",  # Sassy CSS
            ".less",  # LESS CSS
            *DIRECT_IMAGE_EXTENSIONS.keys(),
        ),
    ):
        # Store the base input directory and workspace
        self.base_input_dir = Path(input_dir)
        self.workspace = workspace
        self.supported_extensions = supported_extensions
        self.indexed_files = set()

        # Create workspace-specific input directory
        # If workspace is provided, create a subdirectory for data isolation
        if workspace:
            self.input_dir = self.base_input_dir / workspace
        else:
            self.input_dir = self.base_input_dir

        # Create input directory if it doesn't exist
        self.input_dir.mkdir(parents=True, exist_ok=True)

    def scan_directory_for_new_files(self) -> List[Path]:
        """Scan input directory for new files"""
        new_files = []
        for ext in self.supported_extensions:
            logger.debug(f"Scanning for {ext} files in {self.input_dir}")
            for file_path in self.input_dir.glob(f"*{ext}"):
                if file_path.name.startswith(temp_prefix) or not file_path.is_file():
                    continue
                if file_path not in self.indexed_files:
                    new_files.append(file_path)
        return new_files

    def mark_as_indexed(self, file_path: Path):
        self.indexed_files.add(file_path)

    def is_supported_file(self, filename: str) -> bool:
        return any(filename.lower().endswith(ext) for ext in self.supported_extensions)


def validate_file_path_security(file_path_str: str, base_dir: Path) -> Optional[Path]:
    """
    Validate file path security to prevent Path Traversal attacks.

    Args:
        file_path_str: The file path string to validate
        base_dir: The base directory that the file must be within

    Returns:
        Path: Safe file path if valid, None if unsafe or invalid
    """
    if not file_path_str or not file_path_str.strip():
        return None

    try:
        # Clean the file path string
        clean_path_str = file_path_str.strip()

        # Check for obvious path traversal patterns before processing
        # This catches both Unix (..) and Windows (..\) style traversals
        if ".." in clean_path_str:
            # Additional check for Windows-style backslash traversal
            if (
                "\\..\\" in clean_path_str
                or clean_path_str.startswith("..\\")
                or clean_path_str.endswith("\\..")
            ):
                # logger.warning(
                #     f"Security violation: Windows path traversal attempt detected - {file_path_str}"
                # )
                return None

        # Normalize path separators (convert backslashes to forward slashes)
        # This helps handle Windows-style paths on Unix systems
        normalized_path = clean_path_str.replace("\\", "/")

        # Create path object and resolve it (handles symlinks and relative paths)
        candidate_path = (base_dir / normalized_path).resolve()
        base_dir_resolved = base_dir.resolve()

        # Check if the resolved path is within the base directory
        if not candidate_path.is_relative_to(base_dir_resolved):
            # logger.warning(
            #     f"Security violation: Path traversal attempt detected - {file_path_str}"
            # )
            return None

        return candidate_path

    except (OSError, ValueError, Exception) as e:
        logger.warning(f"Invalid file path detected: {file_path_str} - {str(e)}")
        return None


def get_unique_filename_in_enqueued(target_dir: Path, original_name: str) -> str:
    """Generate a unique filename in the target directory by adding numeric suffixes if needed

    Args:
        target_dir: Target directory path
        original_name: Original filename

    Returns:
        str: Unique filename (may have numeric suffix added)
    """
    import time

    original_path = Path(original_name)
    base_name = original_path.stem
    extension = original_path.suffix

    # Try original name first
    if not (target_dir / original_name).exists():
        return original_name

    # Try with numeric suffixes 001-999
    for i in range(1, 1000):
        suffix = f"{i:03d}"
        new_name = f"{base_name}_{suffix}{extension}"
        if not (target_dir / new_name).exists():
            return new_name

    # Fallback with timestamp if all 999 slots are taken
    timestamp = int(time.time())
    return f"{base_name}_{timestamp}{extension}"


async def _move_file_to_enqueued_directory(file_path: Path) -> None:
    """Move a successfully enqueued file out of the hot input directory."""
    enqueued_dir = file_path.parent / "__enqueued__"
    await asyncio.to_thread(enqueued_dir.mkdir, exist_ok=True)

    unique_filename = get_unique_filename_in_enqueued(enqueued_dir, file_path.name)
    target_path = enqueued_dir / unique_filename
    await asyncio.to_thread(file_path.rename, target_path)
    logger.debug(
        f"Moved file to enqueued directory: {file_path.name} -> {unique_filename}"
    )


def _resolve_document_source_file(
    doc_manager: DocumentManager, logical_file_path: str | None
) -> Path | None:
    """Resolve the actual on-disk source file for a tracked document."""
    normalized = normalize_file_path(logical_file_path)
    if normalized == UNKNOWN_FILE_SOURCE:
        return None

    logical_name = Path(normalized).name
    search_dirs = [doc_manager.input_dir, doc_manager.input_dir / "__enqueued__"]

    for base_dir in search_dirs:
        candidate = base_dir / logical_name
        if candidate.is_file():
            return candidate

    stem = Path(logical_name).stem
    suffix = Path(logical_name).suffix
    wildcard = f"{stem}*{suffix}"
    fallback_candidates: list[Path] = []
    for base_dir in search_dirs:
        if not base_dir.exists():
            continue
        fallback_candidates.extend(
            candidate for candidate in base_dir.glob(wildcard) if candidate.is_file()
        )

    if not fallback_candidates:
        return None

    fallback_candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return fallback_candidates[0]


def _extract_doc_status_file_path(status_doc: Any) -> str | None:
    """Read ``file_path`` from either a dataclass-like object or dict."""
    if status_doc is None:
        return None
    if isinstance(status_doc, dict):
        value = status_doc.get("file_path")
    else:
        value = getattr(status_doc, "file_path", None)
    if isinstance(value, str):
        value = value.strip()
    return value or None


async def _is_file_path_still_referenced(
    rag: LightRAG,
    file_path: str,
    *,
    excluding_doc_ids: set[str] | None = None,
) -> bool:
    """Return True when another tracked document still points at this file."""
    normalized_target = normalize_file_path(file_path)
    if normalized_target == UNKNOWN_FILE_SOURCE:
        return False

    excluding_doc_ids = excluding_doc_ids or set()
    all_statuses = [
        DocStatus.PENDING,
        DocStatus.PROCESSING,
        DocStatus.PREPROCESSED,
        DocStatus.PROCESSED,
        DocStatus.FAILED,
    ]
    docs = await rag.doc_status.get_docs_by_statuses(all_statuses)
    for other_doc_id, status_doc in docs.items():
        if other_doc_id in excluding_doc_ids:
            continue
        other_file_path = normalize_file_path(_extract_doc_status_file_path(status_doc))
        if other_file_path == normalized_target:
            return True
    return False


# Document processing helper functions (synchronous)
# These functions run in thread pool via asyncio.to_thread() to avoid blocking the event loop


def _convert_with_docling(file_path: Path) -> str:
    """Convert document using docling (synchronous).

    Args:
        file_path: Path to the document file

    Returns:
        str: Extracted markdown content
    """
    from docling.document_converter import DocumentConverter  # type: ignore

    converter = DocumentConverter()
    result = converter.convert(file_path)
    return result.document.export_to_markdown()


def _normalize_pdf_bbox(
    raw_bbox: Any,
    *,
    page_width: float | None = None,
    page_height: float | None = None,
) -> dict[str, Any] | None:
    """Normalize docling / pymupdf bbox payloads to a top-left origin dict."""
    if raw_bbox is None:
        return None

    def _get_number(*names: str) -> float | None:
        for name in names:
            value = getattr(raw_bbox, name, None)
            if value is None and isinstance(raw_bbox, dict):
                value = raw_bbox.get(name)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value.strip())
                except ValueError:
                    continue
        return None

    left = _get_number("x0", "l", "left")
    top = _get_number("y0", "t", "top")
    right = _get_number("x1", "r", "right")
    bottom = _get_number("y1", "b", "bottom")
    coord_origin = getattr(raw_bbox, "coord_origin", None)
    if coord_origin is None and isinstance(raw_bbox, dict):
        coord_origin = raw_bbox.get("coord_origin")
    coord_origin_text = str(coord_origin or "TOPLEFT").upper()

    if (
        coord_origin_text.endswith("BOTTOMLEFT")
        and page_height is not None
        and top is not None
        and bottom is not None
    ):
        top, bottom = page_height - top, page_height - bottom

    if left is None or top is None or right is None or bottom is None:
        return None

    y0 = min(top, bottom)
    y1 = max(top, bottom)
    x0 = min(left, right)
    x1 = max(left, right)
    return {
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "width": max(0.0, x1 - x0),
        "height": max(0.0, y1 - y0),
        "page_width": page_width,
        "page_height": page_height,
        "coord_origin": "TOPLEFT",
    }


def _bbox_area_ratio(normalized_bbox: Any) -> float:
    """Best-effort area ratio of one bbox against its source PDF page."""
    if not isinstance(normalized_bbox, dict):
        return 0.0

    try:
        width = float(normalized_bbox.get("width") or 0.0)
        height = float(normalized_bbox.get("height") or 0.0)
        page_width = float(normalized_bbox.get("page_width") or 0.0)
        page_height = float(normalized_bbox.get("page_height") or 0.0)
    except (TypeError, ValueError):
        return 0.0

    if width <= 0 or height <= 0 or page_width <= 0 or page_height <= 0:
        return 0.0

    return max(0.0, min(1.0, (width * height) / (page_width * page_height)))


def _should_add_page_raster_fallback(
    *,
    exact_picture_count: int,
    exact_picture_coverage_ratio: float,
    max_exact_picture_ratio: float,
) -> bool:
    """Decide whether a figure-heavy page still needs a full-page raster.

    Docling exact-picture crops are great when they already capture the main
    figure. For engineering PDFs, however, Docling may emit one or more small
    crops from a page whose *real* answerable artifact is the whole page (for
    example a full floor plan or vector-heavy drawing). In that case we keep a
    page-level raster fallback as a recall safety net.
    """
    if exact_picture_count <= 0:
        return True

    if max_exact_picture_ratio >= 0.72 or exact_picture_coverage_ratio >= 0.75:
        return False

    if exact_picture_count >= 3 and exact_picture_coverage_ratio >= 0.15:
        return False

    if exact_picture_count >= 2 and exact_picture_coverage_ratio >= 0.18:
        return False

    if exact_picture_count == 1 and max_exact_picture_ratio >= 0.38:
        return False

    return True


def _normalize_page_text_line(line: str) -> str:
    import re

    return re.sub(r"\s+", " ", str(line or "")).strip()


def _extract_page_label_info(page_text: str) -> tuple[int | None, str | None]:
    """Best-effort extraction of the printed / footer page label from page text."""
    import re

    if not page_text:
        return None, None

    lines = [
        _normalize_page_text_line(line)
        for line in str(page_text).splitlines()
        if _normalize_page_text_line(line)
    ]
    if not lines:
        return None, None

    patterns: list[tuple[re.Pattern[str], int]] = [
        (re.compile(r"^第\s*(\d{1,4})\s*页$"), 50),
        (re.compile(r"^[Pp](?:age|\.)?\s*(\d{1,4})$"), 40),
        (re.compile(r"^(\d{1,4})$"), 35),
    ]
    candidates: list[tuple[int, int, str]] = []
    windowed_lines: list[tuple[int, str, bool]] = []
    for index, line in enumerate(lines[:8]):
        windowed_lines.append((index, line, False))
    tail_start = max(0, len(lines) - 8)
    for index in range(tail_start, len(lines)):
        windowed_lines.append((index, lines[index], True))

    seen_keys: set[tuple[int, str]] = set()
    for index, line, is_tail in windowed_lines:
        dedupe_key = (index, line)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        for pattern, base_score in patterns:
            match = pattern.match(line)
            if not match:
                continue
            try:
                page_number = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if page_number <= 0:
                continue
            position_bonus = 18 if index <= 5 else 12
            edge_bonus = 8 if is_tail else 6
            short_line_bonus = 8 if len(line) <= 4 else 0
            candidates.append(
                (
                    base_score + position_bonus + edge_bonus + short_line_bonus,
                    page_number,
                    line,
                )
            )

    if not candidates:
        return None, None

    candidates.sort(reverse=True)
    _score, printed_page, raw_label = candidates[0]
    page_label = raw_label if raw_label else str(printed_page)
    return printed_page, page_label


def _format_page_reference_marker(page_ref: dict[str, Any] | None) -> str:
    if not isinstance(page_ref, dict):
        return "unknown"

    physical_page = page_ref.get("source_page")
    printed_page = page_ref.get("source_printed_page")
    page_label = str(page_ref.get("source_page_label") or "").strip()

    if physical_page is not None:
        try:
            physical_page_text = f"p.{int(physical_page)}"
        except (TypeError, ValueError):
            physical_page_text = f"p.{physical_page}"
    else:
        physical_page_text = "unknown"

    if printed_page is not None:
        try:
            printed_page_text = str(int(printed_page))
        except (TypeError, ValueError):
            printed_page_text = str(printed_page)
        if printed_page_text and printed_page_text != str(physical_page):
            return f"{physical_page_text}/页码{printed_page_text}"

    if page_label and page_label != str(physical_page):
        return f"{physical_page_text}/页标{page_label}"

    return physical_page_text


def _bbox_area(normalized_bbox: Any) -> float:
    if not isinstance(normalized_bbox, dict):
        return 0.0
    try:
        width = float(normalized_bbox.get("width") or 0.0)
        height = float(normalized_bbox.get("height") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if width <= 0 or height <= 0:
        return 0.0
    return width * height


def _bbox_overlap_ratio_on_smaller(a: Any, b: Any) -> float:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return 0.0

    try:
        ax0 = float(a.get("x0"))
        ay0 = float(a.get("y0"))
        ax1 = float(a.get("x1"))
        ay1 = float(a.get("y1"))
        bx0 = float(b.get("x0"))
        by0 = float(b.get("y0"))
        bx1 = float(b.get("x1"))
        by1 = float(b.get("y1"))
    except (TypeError, ValueError):
        return 0.0

    inter_w = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    inter_h = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter_area = inter_w * inter_h
    if inter_area <= 0:
        return 0.0

    smaller_area = min(_bbox_area(a), _bbox_area(b))
    if smaller_area <= 0:
        return 0.0

    return max(0.0, min(1.0, inter_area / smaller_area))


def _rounded_bbox_signature(
    normalized_bbox: Any,
    *,
    quantum: float = 8.0,
) -> tuple[int, int, int, int] | None:
    if not isinstance(normalized_bbox, dict):
        return None
    try:
        return (
            int(round(float(normalized_bbox.get("x0")) / quantum)),
            int(round(float(normalized_bbox.get("y0")) / quantum)),
            int(round(float(normalized_bbox.get("x1")) / quantum)),
            int(round(float(normalized_bbox.get("y1")) / quantum)),
        )
    except (TypeError, ValueError):
        return None


def _candidate_pixel_count(item: dict[str, Any]) -> int:
    pil_size = item.get("pil_size")
    if (
        isinstance(pil_size, (tuple, list))
        and len(pil_size) >= 2
        and isinstance(pil_size[0], (int, float))
        and isinstance(pil_size[1], (int, float))
    ):
        return max(0, int(pil_size[0])) * max(0, int(pil_size[1]))
    return 0


def _multimodal_extraction_priority(item: dict[str, Any]) -> int:
    mode = str(item.get("extraction_mode") or "").strip()
    return {
        "pymupdf_native_image": 500,
        "pymupdf_image_block": 450,
        "docling_picture": 400,
        "page_raster_recall_fallback": 150,
        "page_raster_fallback": 100,
    }.get(mode, 0)


def _prefer_richer_text(current: Any, candidate: Any) -> Any:
    current_text = str(current or "").strip()
    candidate_text = str(candidate or "").strip()
    if candidate_text and len(candidate_text) > len(current_text):
        return candidate
    return current


def _merge_context_chunks(
    current: Any, candidate: Any
) -> list[dict[str, Any]] | None:
    merged: list[dict[str, Any]] = []
    seen_chunk_ids: set[str] = set()
    for source in (current, candidate):
        if not isinstance(source, list):
            continue
        for chunk in source:
            if not isinstance(chunk, dict):
                continue
            chunk_id = str(chunk.get("chunk_id") or "").strip()
            key = chunk_id or str(chunk)
            if key in seen_chunk_ids:
                continue
            seen_chunk_ids.add(key)
            merged.append(chunk)
    return merged or None


def _should_keep_precise_candidate(candidate: dict[str, Any]) -> bool:
    bbox = candidate.get("bbox")
    bbox_ratio = _bbox_area_ratio(bbox)
    pixel_count = _candidate_pixel_count(candidate)
    display_width = 0.0
    display_height = 0.0
    if isinstance(bbox, dict):
        try:
            display_width = float(bbox.get("width") or 0.0)
            display_height = float(bbox.get("height") or 0.0)
        except (TypeError, ValueError):
            display_width = 0.0
            display_height = 0.0

    if pixel_count <= 0:
        return False
    if display_width < 48 and display_height < 48:
        return False
    if bbox_ratio < 0.002 and pixel_count < 12_000:
        return False
    if bbox_ratio < 0.005 and pixel_count < 24_000:
        return False
    return True


def _multimodal_candidates_look_duplicate(
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    try:
        if int(existing.get("page_no")) != int(candidate.get("page_no")):
            return False
    except (TypeError, ValueError):
        if existing.get("page_no") != candidate.get("page_no"):
            return False

    existing_bbox = existing.get("bbox")
    candidate_bbox = candidate.get("bbox")
    overlap_ratio = _bbox_overlap_ratio_on_smaller(existing_bbox, candidate_bbox)
    bbox_signatures_match = (
        _rounded_bbox_signature(existing_bbox) is not None
        and _rounded_bbox_signature(existing_bbox)
        == _rounded_bbox_signature(candidate_bbox)
    )

    existing_hash = existing.get("_content_hash")
    candidate_hash = candidate.get("_content_hash")
    if existing_hash and candidate_hash and existing_hash == candidate_hash:
        if bbox_signatures_match or overlap_ratio >= 0.88:
            return True
        if existing_bbox is None and candidate_bbox is None:
            return True

    existing_xref = existing.get("native_xref")
    candidate_xref = candidate.get("native_xref")
    if (
        existing_xref is not None
        and candidate_xref is not None
        and str(existing_xref) == str(candidate_xref)
        and (bbox_signatures_match or overlap_ratio >= 0.72)
    ):
        return True

    if bbox_signatures_match and overlap_ratio >= 0.90:
        return True

    return overlap_ratio >= 0.97


def _merge_multimodal_candidate(
    preferred: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    preferred["caption_hint"] = _prefer_richer_text(
        preferred.get("caption_hint"), candidate.get("caption_hint")
    )
    preferred["page_text_excerpt"] = _prefer_richer_text(
        preferred.get("page_text_excerpt"), candidate.get("page_text_excerpt")
    )
    preferred["context_text"] = _prefer_richer_text(
        preferred.get("context_text"), candidate.get("context_text")
    )
    if preferred.get("bbox") is None and candidate.get("bbox") is not None:
        preferred["bbox"] = candidate.get("bbox")
    if _candidate_pixel_count(candidate) > _candidate_pixel_count(preferred):
        preferred["pil_size"] = candidate.get("pil_size")
    if preferred.get("page_size") is None and candidate.get("page_size") is not None:
        preferred["page_size"] = candidate.get("page_size")
    if preferred.get("source_printed_page") is None and candidate.get(
        "source_printed_page"
    ) is not None:
        preferred["source_printed_page"] = candidate.get("source_printed_page")
    if not preferred.get("source_page_label") and candidate.get("source_page_label"):
        preferred["source_page_label"] = candidate.get("source_page_label")
    if preferred.get("native_xref") is None and candidate.get("native_xref") is not None:
        preferred["native_xref"] = candidate.get("native_xref")
    if not preferred.get("native_ext") and candidate.get("native_ext"):
        preferred["native_ext"] = candidate.get("native_ext")

    merged_context_chunks = _merge_context_chunks(
        preferred.get("context_chunks"), candidate.get("context_chunks")
    )
    if merged_context_chunks is not None:
        preferred["context_chunks"] = merged_context_chunks
        preferred["context_chunk_ids"] = [
            str(chunk.get("chunk_id"))
            for chunk in merged_context_chunks
            if isinstance(chunk, dict) and chunk.get("chunk_id")
        ]
    elif not preferred.get("context_chunk_ids") and candidate.get("context_chunk_ids"):
        preferred["context_chunk_ids"] = candidate.get("context_chunk_ids")

    merged_modes = list(
        dict.fromkeys(
            [
                *(
                    preferred.get("merged_extraction_modes")
                    if isinstance(preferred.get("merged_extraction_modes"), list)
                    else []
                ),
                str(preferred.get("extraction_mode") or "").strip(),
                *(
                    candidate.get("merged_extraction_modes")
                    if isinstance(candidate.get("merged_extraction_modes"), list)
                    else []
                ),
                str(candidate.get("extraction_mode") or "").strip(),
            ]
        )
    )
    preferred["merged_extraction_modes"] = [mode for mode in merged_modes if mode]
    return preferred


def _dedupe_precise_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not candidates:
        return []

    sortable = [dict(candidate) for candidate in candidates if isinstance(candidate, dict)]
    sortable.sort(
        key=lambda item: (
            -_multimodal_extraction_priority(item),
            -_bbox_area_ratio(item.get("bbox")),
            -_candidate_pixel_count(item),
        )
    )

    deduped: list[dict[str, Any]] = []
    for candidate in sortable:
        img_bytes = candidate.get("bytes")
        if img_bytes and not candidate.get("_content_hash"):
            candidate["_content_hash"] = compute_mdhash_id(img_bytes, prefix="img-")

        merged = False
        for existing in deduped:
            if _multimodal_candidates_look_duplicate(existing, candidate):
                _merge_multimodal_candidate(existing, candidate)
                merged = True
                break
        if not merged:
            deduped.append(candidate)

    deduped.sort(
        key=lambda item: (
            item.get("page_no") if item.get("page_no") is not None else 10**9,
            -_bbox_area_ratio(item.get("bbox")),
            -_candidate_pixel_count(item),
            -_multimodal_extraction_priority(item),
        )
    )
    return deduped


def _cap_precise_candidates_per_page(
    candidates: list[dict[str, Any]],
    *,
    max_per_page: int = 8,
) -> list[dict[str, Any]]:
    if max_per_page <= 0:
        return []

    grouped: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.get("page_no")].append(candidate)

    limited: list[dict[str, Any]] = []
    for page_candidates in grouped.values():
        page_candidates.sort(
            key=lambda item: (
                -_bbox_area_ratio(item.get("bbox")),
                -_candidate_pixel_count(item),
                -_multimodal_extraction_priority(item),
            )
        )
        limited.extend(page_candidates[:max_per_page])

    limited.sort(
        key=lambda item: (
            item.get("page_no") if item.get("page_no") is not None else 10**9,
            item.get("page_picture_index")
            if item.get("page_picture_index") is not None
            else item.get("picture_index", 0),
        )
    )
    return limited


def _collect_precise_picture_page_stats(
    candidates: list[dict[str, Any]],
) -> tuple[set[int], dict[int, int], dict[int, float], dict[int, float]]:
    precise_pages: set[int] = set()
    counts_by_page: dict[int, int] = {}
    coverage_by_page: dict[int, float] = {}
    max_ratio_by_page: dict[int, float] = {}

    for candidate in candidates:
        page_no = candidate.get("page_no")
        try:
            page_no_int = int(page_no)
        except (TypeError, ValueError):
            continue
        precise_pages.add(page_no_int)
        counts_by_page[page_no_int] = counts_by_page.get(page_no_int, 0) + 1
        bbox_ratio = _bbox_area_ratio(candidate.get("bbox"))
        coverage_by_page[page_no_int] = coverage_by_page.get(page_no_int, 0.0) + bbox_ratio
        max_ratio_by_page[page_no_int] = max(
            max_ratio_by_page.get(page_no_int, 0.0),
            bbox_ratio,
        )

    return precise_pages, counts_by_page, coverage_by_page, max_ratio_by_page


def _image_mime_type_from_ext(ext: str | None) -> str:
    ext_text = str(ext or "").strip().lower().lstrip(".")
    if not ext_text:
        return "image/png"
    if ext_text in {"jpg", "jpeg"}:
        return "image/jpeg"
    if ext_text in {"jp2", "jpx"}:
        return "image/jp2"
    if ext_text in {"tif", "tiff"}:
        return "image/tiff"
    if ext_text == "bmp":
        return "image/bmp"
    if ext_text == "gif":
        return "image/gif"
    if ext_text == "webp":
        return "image/webp"
    guessed = mimetypes.guess_type(f"image.{ext_text}")[0]
    return guessed or f"image/{ext_text}"


def _count_candidates_by_mode(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        mode = str(candidate.get("extraction_mode") or "unknown").strip() or "unknown"
        counts[mode] = counts.get(mode, 0) + 1
    return counts


def _format_mode_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{mode}={count}" for mode, count in sorted(counts.items()))


def _resolve_multimodal_max_images(
    scanned_page_count: int,
    requested_max_images: int | None,
) -> int:
    """Resolve the effective multimodal image cap for one conversion run.

    Small/medium documents should keep a conservative cap so captioning and
    embedding cost remain bounded. Large engineering PDFs, however, routinely
    contain hundreds of genuine embedded images. For those, a fixed 256-image
    cap truncates too aggressively and causes recall failures on later pages.
    """
    if requested_max_images is not None:
        try:
            normalized = int(requested_max_images)
        except (TypeError, ValueError):
            normalized = 0
        if normalized > 0:
            return normalized

    if scanned_page_count >= 400:
        return 1024
    if scanned_page_count >= 240:
        return 768
    if scanned_page_count >= 120:
        return 512
    return 256

def _build_page_context_excerpt(
    pages_text: list[str],
    page_index: int,
    *,
    page_refs: list[dict[str, Any]] | None = None,
    current_limit: int = 1200,
    neighbor_limit: int = 300,
) -> tuple[str, str]:
    """Build a current-page excerpt plus a surrounding context window."""
    if not (0 <= page_index < len(pages_text)):
        return "", ""

    current_text = (pages_text[page_index] or "").strip()
    current_excerpt = current_text[:current_limit]

    context_sections: list[str] = []
    prev_index = page_index - 1
    next_index = page_index + 1
    prev_marker = _format_page_reference_marker(
        page_refs[prev_index] if page_refs and 0 <= prev_index < len(page_refs) else None
    )
    current_marker = _format_page_reference_marker(
        page_refs[page_index] if page_refs and 0 <= page_index < len(page_refs) else None
    )
    next_marker = _format_page_reference_marker(
        page_refs[next_index] if page_refs and 0 <= next_index < len(page_refs) else None
    )
    if prev_index >= 0:
        prev_text = (pages_text[prev_index] or "").strip()
        if prev_text:
            context_sections.append(
                f"[上一页末尾 {prev_marker}]\n{prev_text[-neighbor_limit:]}"
            )
    if current_excerpt:
        context_sections.append(f"[当前页 {current_marker}]\n{current_excerpt}")
    if next_index < len(pages_text):
        next_text = (pages_text[next_index] or "").strip()
        if next_text:
            context_sections.append(
                f"[下一页开头 {next_marker}]\n{next_text[:neighbor_limit]}"
            )

    return current_excerpt, "\n\n".join(context_sections).strip()


def _extract_pymupdf_native_images(
    fitz_doc: Any,
    *,
    pages_text: list[str],
    page_sizes: dict[int, tuple[float, float]],
    page_refs: list[dict[str, Any]],
    start_page_index: int = 0,
    end_page_index: int | None = None,
) -> list[dict[str, Any]]:
    if fitz_doc is None:
        return []

    native_candidates: list[dict[str, Any]] = []
    end_page = end_page_index if end_page_index is not None else fitz_doc.page_count
    per_page_picture_count: dict[int, int] = {}

    for page_idx in range(start_page_index, min(end_page, fitz_doc.page_count)):
        page = fitz_doc.load_page(page_idx)
        page_no = page_idx + 1
        local_index = page_idx - start_page_index
        page_ref = (
            page_refs[local_index]
            if 0 <= local_index < len(page_refs)
            else {"source_page": page_no}
        )
        page_text_excerpt, context_text = _build_page_context_excerpt(
            pages_text,
            local_index,
            page_refs=page_refs,
        )
        caption_hint = ""
        if 0 <= local_index < len(pages_text):
            caption_hint = (pages_text[local_index] or "").strip()[:200]

        try:
            page_images = page.get_images(full=True) or []
        except Exception as e:
            logger.debug(
                f"[multimodal] pymupdf get_images failed for {page_no=} "
                f"on {getattr(fitz_doc, 'name', '<memory-pdf>')}: {e}"
            )
            continue

        for image_entry in page_images:
            try:
                xref = int(image_entry[0])
            except (TypeError, ValueError, IndexError):
                continue
            if xref <= 0:
                continue

            try:
                extracted_image = fitz_doc.extract_image(xref)
            except Exception as e:
                logger.debug(
                    f"[multimodal] pymupdf extract_image failed for "
                    f"{page_no=} {xref=}: {e}"
                )
                continue

            if not isinstance(extracted_image, dict):
                continue
            img_bytes = extracted_image.get("image")
            if not img_bytes:
                continue

            try:
                rects = page.get_image_rects(xref) or []
            except Exception as e:
                logger.debug(
                    f"[multimodal] pymupdf get_image_rects failed for "
                    f"{page_no=} {xref=}: {e}"
                )
                rects = []

            if not rects:
                rects = [page.rect]

            for occurrence_index, rect in enumerate(rects):
                raw_rect = rect[0] if isinstance(rect, tuple) else rect
                page_width, page_height = page_sizes.get(
                    page_no, (float(page.rect.width), float(page.rect.height))
                )
                normalized_bbox = _normalize_pdf_bbox(
                    {
                        "x0": float(raw_rect.x0),
                        "y0": float(raw_rect.y0),
                        "x1": float(raw_rect.x1),
                        "y1": float(raw_rect.y1),
                        "coord_origin": "TOPLEFT",
                    },
                    page_width=page_width,
                    page_height=page_height,
                )
                candidate = {
                    "bytes": img_bytes,
                    "mime_type": _image_mime_type_from_ext(extracted_image.get("ext")),
                    "page_no": page_no,
                    "source_printed_page": page_ref.get("source_printed_page"),
                    "source_page_label": page_ref.get("source_page_label"),
                    "bbox": normalized_bbox,
                    "caption_hint": caption_hint,
                    "picture_index": len(native_candidates),
                    "page_picture_index": per_page_picture_count.get(page_no, 0),
                    "pil_size": (
                        int(extracted_image.get("width") or 0),
                        int(extracted_image.get("height") or 0),
                    ),
                    "page_size": (page_width, page_height),
                    "page_text_excerpt": page_text_excerpt,
                    "context_text": context_text,
                    "extraction_mode": "pymupdf_native_image",
                    "native_xref": xref,
                    "native_occurrence_index": occurrence_index,
                    "native_ext": str(extracted_image.get("ext") or "").strip() or None,
                }
                if not _should_keep_precise_candidate(candidate):
                    continue
                native_candidates.append(candidate)
                per_page_picture_count[page_no] = (
                    per_page_picture_count.get(page_no, 0) + 1
                )

    return native_candidates


def _extract_pymupdf_image_blocks(
    fitz_doc: Any,
    *,
    pages_text: list[str],
    page_sizes: dict[int, tuple[float, float]],
    page_refs: list[dict[str, Any]],
    start_page_index: int = 0,
    end_page_index: int | None = None,
) -> list[dict[str, Any]]:
    if fitz_doc is None:
        return []

    block_candidates: list[dict[str, Any]] = []
    end_page = end_page_index if end_page_index is not None else fitz_doc.page_count
    per_page_picture_count: dict[int, int] = {}

    for page_idx in range(start_page_index, min(end_page, fitz_doc.page_count)):
        page = fitz_doc.load_page(page_idx)
        page_no = page_idx + 1
        local_index = page_idx - start_page_index
        page_ref = (
            page_refs[local_index]
            if 0 <= local_index < len(page_refs)
            else {"source_page": page_no}
        )
        page_text_excerpt, context_text = _build_page_context_excerpt(
            pages_text,
            local_index,
            page_refs=page_refs,
        )
        caption_hint = ""
        if 0 <= local_index < len(pages_text):
            caption_hint = (pages_text[local_index] or "").strip()[:200]

        try:
            blocks = page.get_text("dict").get("blocks", []) or []
        except Exception as e:
            logger.debug(
                f"[multimodal] pymupdf get_text(dict) failed for {page_no=}: {e}"
            )
            continue

        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != 1:
                continue
            img_bytes = block.get("image")
            if not isinstance(img_bytes, (bytes, bytearray)) or not img_bytes:
                continue

            page_width, page_height = page_sizes.get(
                page_no, (float(page.rect.width), float(page.rect.height))
            )
            bbox = block.get("bbox")
            normalized_bbox = _normalize_pdf_bbox(
                {
                    "x0": bbox[0],
                    "y0": bbox[1],
                    "x1": bbox[2],
                    "y1": bbox[3],
                    "coord_origin": "TOPLEFT",
                }
                if isinstance(bbox, (tuple, list)) and len(bbox) == 4
                else bbox,
                page_width=page_width,
                page_height=page_height,
            )
            candidate = {
                "bytes": bytes(img_bytes),
                "mime_type": _image_mime_type_from_ext(block.get("ext")),
                "page_no": page_no,
                "source_printed_page": page_ref.get("source_printed_page"),
                "source_page_label": page_ref.get("source_page_label"),
                "bbox": normalized_bbox,
                "caption_hint": caption_hint,
                "picture_index": len(block_candidates),
                "page_picture_index": per_page_picture_count.get(page_no, 0),
                "pil_size": (
                    int(block.get("width") or 0),
                    int(block.get("height") or 0),
                ),
                "page_size": (page_width, page_height),
                "page_text_excerpt": page_text_excerpt,
                "context_text": context_text,
                "extraction_mode": "pymupdf_image_block",
                "native_ext": str(block.get("ext") or "").strip() or None,
            }
            if not _should_keep_precise_candidate(candidate):
                continue
            block_candidates.append(candidate)
            per_page_picture_count[page_no] = per_page_picture_count.get(page_no, 0) + 1

    return block_candidates


def _extract_docling_picture_candidates(
    file_path: Path,
    *,
    pages_text: list[str],
    page_sizes: dict[int, tuple[float, float]],
    page_refs: list[dict[str, Any]],
    page_range: tuple[int, int] | None,
    raster_dpi: int,
) -> list[dict[str, Any]]:
    if not _is_docling_available():
        return []

    candidates: list[dict[str, Any]] = []
    try:
        from docling.datamodel.base_models import InputFormat  # type: ignore
        from docling.datamodel.pipeline_options import PdfPipelineOptions  # type: ignore
        from docling.document_converter import (  # type: ignore
            DocumentConverter,
            PdfFormatOption,
        )

        pipeline_options = PdfPipelineOptions()
        pipeline_options.generate_picture_images = True
        pipeline_options.images_scale = max(1.0, raster_dpi / 72.0)
        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
        convert_kwargs: dict[str, Any] = {}
        if page_range is not None:
            convert_kwargs["page_range"] = page_range

        result = converter.convert(file_path, **convert_kwargs)
        docling_doc = result.document
        base_physical_page = (
            int(page_refs[0].get("source_page"))
            if page_refs and page_refs[0].get("source_page") is not None
            else 1
        )
        per_page_picture_count: dict[int, int] = {}

        for global_picture_index, picture in enumerate(docling_doc.pictures):
            try:
                pil = picture.get_image(docling_doc)
            except Exception as picture_error:
                logger.debug(
                    f"[multimodal] docling get_image failed for "
                    f"{file_path.name} picture {global_picture_index}: {picture_error}"
                )
                continue

            if pil is None:
                continue

            page_no = None
            normalized_bbox = None
            prov_items = list(getattr(picture, "prov", None) or [])
            for prov in prov_items:
                prov_page = getattr(prov, "page_no", None)
                if prov_page is None:
                    continue
                try:
                    page_no = int(prov_page)
                except (TypeError, ValueError):
                    page_no = None
                page_size = page_sizes.get(page_no or -1)
                normalized_bbox = _normalize_pdf_bbox(
                    getattr(prov, "bbox", None),
                    page_width=page_size[0] if page_size else None,
                    page_height=page_size[1] if page_size else None,
                )
                if page_no is not None:
                    break

            if page_no is None:
                continue

            local_index = max(0, page_no - base_physical_page)
            page_ref = (
                page_refs[local_index]
                if 0 <= local_index < len(page_refs)
                else {"source_page": page_no}
            )
            page_text_excerpt, context_text = _build_page_context_excerpt(
                pages_text,
                local_index,
                page_refs=page_refs,
            )
            caption_hint = ""
            try:
                caption_hint = str(picture.caption_text(docling_doc) or "").strip()
            except Exception:
                caption_hint = ""
            if not caption_hint and 0 <= local_index < len(pages_text):
                caption_hint = (pages_text[local_index] or "").strip()[:200]

            buffer = BytesIO()
            pil.save(buffer, format="PNG")
            candidate = {
                "bytes": buffer.getvalue(),
                "mime_type": "image/png",
                "page_no": page_no,
                "source_printed_page": page_ref.get("source_printed_page"),
                "source_page_label": page_ref.get("source_page_label"),
                "bbox": normalized_bbox,
                "caption_hint": caption_hint,
                "picture_index": global_picture_index,
                "page_picture_index": per_page_picture_count.get(page_no, 0),
                "pil_size": pil.size,
                "page_size": page_sizes.get(page_no),
                "page_text_excerpt": page_text_excerpt,
                "context_text": context_text,
                "extraction_mode": "docling_picture",
            }
            if not _should_keep_precise_candidate(candidate):
                continue
            candidates.append(candidate)
            per_page_picture_count[page_no] = per_page_picture_count.get(page_no, 0) + 1
    except Exception as e:
        logger.warning(
            f"[multimodal] docling picture extraction failed for "
            f"{file_path.name}: {type(e).__name__}: {e}"
        )
        return []

    return candidates

def _convert_with_docling_multimodal(
    file_path: Path,
    page_range: tuple[int, int] | None = None,
    max_images: int | None = None,
    raster_dpi: int = 144,
) -> tuple[str, list[dict[str, Any]]]:
    """Extract PDF text plus multimodal figures with source-page/bbox fidelity.

    Architecture:
        1. Extract per-page text with pypdf, filling gaps via PyMuPDF text.
        2. Build page references that preserve BOTH physical PDF page numbers
           and printed / footer page labels when they differ.
        3. Collect precise figure candidates from three routes:
             - Docling semantic crops
             - PyMuPDF native embedded-image extraction
             - PyMuPDF image blocks from page text dicts
        4. Deduplicate / rank precise candidates BEFORE they can flow into the
           downstream image-caption / embedding / chunk-augmentation pipeline.
        5. Add full-page raster fallbacks only on figure-heavy pages whose
           precise-figure coverage is insufficient.
    """
    import re

    pages_text: list[str] = []
    page_sizes: dict[int, tuple[float, float]] = {}
    page_refs: list[dict[str, Any]] = []
    fitz_doc = None
    start_p = (page_range[0] - 1) if page_range else 0
    end_p = 0
    extraction_truncated = False

    try:
        import fitz  # type: ignore

        fitz_doc = fitz.open(file_path)
        end_p = page_range[1] if page_range else fitz_doc.page_count
        for i in range(start_p, min(end_p, fitz_doc.page_count)):
            rect = fitz_doc.load_page(i).rect
            page_sizes[i + 1] = (float(rect.width), float(rect.height))
    except Exception as e:
        logger.warning(
            f"[multimodal] failed to read PDF geometry for {file_path.name}: {e}"
        )

    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(file_path))
        end_p = page_range[1] if page_range else max(end_p, len(reader.pages))
        for i in range(start_p, min(end_p, len(reader.pages))):
            pages_text.append(reader.pages[i].extract_text() or "")
    except Exception as e:
        logger.warning(
            f"[multimodal] pypdf text extraction failed for {file_path.name}: {e}"
        )

    if fitz_doc is not None:
        fitz_end = min(end_p or fitz_doc.page_count, fitz_doc.page_count)
        for i in range(start_p, fitz_end):
            try:
                fitz_text = fitz_doc.load_page(i).get_text("text") or ""
            except Exception:
                fitz_text = ""
            local_idx = i - start_p
            if local_idx < len(pages_text):
                if not str(pages_text[local_idx] or "").strip() and fitz_text.strip():
                    pages_text[local_idx] = fitz_text
            else:
                pages_text.append(fitz_text)

    if not pages_text and page_sizes:
        pages_text = [""] * len(page_sizes)

    scanned_page_count = max(len(pages_text), len(page_sizes))
    effective_max_images = _resolve_multimodal_max_images(
        scanned_page_count,
        max_images,
    )
    if effective_max_images != (max_images or 256):
        logger.info(
            f"[multimodal] {file_path.name}: auto-expanded image cap "
            f"to {effective_max_images} for {scanned_page_count} scanned pages"
        )

    full_text = "\n".join(pages_text)

    if not full_text.strip() and _is_docling_available():
        try:
            from docling.document_converter import DocumentConverter  # type: ignore

            converter = DocumentConverter()
            ckw: dict[str, Any] = {}
            if page_range is not None:
                ckw["page_range"] = page_range
            result = converter.convert(file_path, **ckw)
            full_text = result.document.export_to_markdown()
            if full_text.strip() and not any(text.strip() for text in pages_text):
                logger.info(
                    f"[multimodal] docling text fallback extracted "
                    f"{len(full_text):,} chars from {file_path.name}"
                )
        except Exception as e2:
            logger.warning(f"[multimodal] docling text fallback failed: {e2}")

    for local_index, page_text in enumerate(pages_text):
        physical_page_no = start_p + local_index + 1
        printed_page, page_label = _extract_page_label_info(page_text)
        page_refs.append(
            {
                "source_page": physical_page_no,
                "source_printed_page": printed_page,
                "source_page_label": page_label,
            }
        )

    precise_raw_candidates: list[dict[str, Any]] = []
    precise_raw_candidates.extend(
        _extract_docling_picture_candidates(
            file_path,
            pages_text=pages_text,
            page_sizes=page_sizes,
            page_refs=page_refs,
            page_range=page_range,
            raster_dpi=raster_dpi,
        )
    )
    if fitz_doc is not None:
        precise_raw_candidates.extend(
            _extract_pymupdf_native_images(
                fitz_doc,
                pages_text=pages_text,
                page_sizes=page_sizes,
                page_refs=page_refs,
                start_page_index=start_p,
                end_page_index=(page_range[1] if page_range else None),
            )
        )
        precise_raw_candidates.extend(
            _extract_pymupdf_image_blocks(
                fitz_doc,
                pages_text=pages_text,
                page_sizes=page_sizes,
                page_refs=page_refs,
                start_page_index=start_p,
                end_page_index=(page_range[1] if page_range else None),
            )
        )

    precise_candidates = _cap_precise_candidates_per_page(
        _dedupe_precise_candidates(precise_raw_candidates),
        max_per_page=8,
    )
    raw_mode_counts = _count_candidates_by_mode(precise_raw_candidates)

    (
        precise_picture_pages,
        precise_picture_counts_by_page,
        precise_picture_coverage_by_page,
        precise_picture_max_ratio_by_page,
    ) = _collect_precise_picture_page_stats(precise_candidates)

    figure_pattern = re.compile(
        r"图\s*\d+[\.\-]\d+"
        r"|图\s*\d+"
        r"|(?:示意|流程|平面|立面|剖面|结构|布置|施工|工艺|安装|节点|大样|应急路线|绿化|总平面|管线|配筋|详图)"
        r"|附图"
        r"|Figure\s+\d+"
        r"|Fig\.\s*\d+",
        re.IGNORECASE,
    )

    figure_page_indices: list[int] = []
    for local_idx, text in enumerate(pages_text):
        abs_idx = start_p + local_idx
        stripped = str(text or "").strip()
        is_figure_page = False
        if figure_pattern.search(stripped):
            is_figure_page = True
        elif len(stripped) <= 80:
            is_figure_page = True
        elif fitz_doc is not None and abs_idx < fitz_doc.page_count:
            try:
                page = fitz_doc.load_page(abs_idx)
                if len(page.get_images(full=True) or []) >= 1:
                    is_figure_page = True
                elif len(stripped) < 1400 and len(page.get_drawings()) >= 20:
                    is_figure_page = True
            except Exception:
                pass
        if is_figure_page:
            figure_page_indices.append(abs_idx)

    logger.info(
        f"[multimodal] {file_path.name}: {len(pages_text)} pages scanned, "
        f"{len(figure_page_indices)} figure-heavy pages detected"
    )

    page_raster_candidates: list[dict[str, Any]] = []
    max_page_raster_fallbacks = max(
        8, min(48, effective_max_images // 3 if effective_max_images > 0 else 16)
    )
    try:
        if fitz_doc is None:
            import fitz  # type: ignore

            fitz_doc = fitz.open(file_path)

        matrix = fitz.Matrix(raster_dpi / 72, raster_dpi / 72)
        fallback_rank = 0
        for page_idx in figure_page_indices:
            if len(page_raster_candidates) >= max_page_raster_fallbacks:
                break
            page_no = page_idx + 1
            if page_idx >= fitz_doc.page_count:
                continue

            exact_picture_count = precise_picture_counts_by_page.get(page_no, 0)
            exact_picture_coverage_ratio = precise_picture_coverage_by_page.get(page_no, 0.0)
            max_exact_picture_ratio = precise_picture_max_ratio_by_page.get(page_no, 0.0)
            if not _should_add_page_raster_fallback(
                exact_picture_count=exact_picture_count,
                exact_picture_coverage_ratio=exact_picture_coverage_ratio,
                max_exact_picture_ratio=max_exact_picture_ratio,
            ):
                continue

            page = fitz_doc.load_page(page_idx)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            local_idx = page_idx - start_p
            page_ref = (
                page_refs[local_idx]
                if 0 <= local_idx < len(page_refs)
                else {"source_page": page_no}
            )
            page_text_excerpt, context_text = _build_page_context_excerpt(
                pages_text,
                local_idx,
                page_refs=page_refs,
            )
            hint = ""
            if 0 <= local_idx < len(pages_text):
                hint = (pages_text[local_idx] or "").strip()[:200]

            page_raster_candidates.append(
                {
                    "bytes": pix.tobytes("png"),
                    "mime_type": "image/png",
                    "page_no": page_no,
                    "source_printed_page": page_ref.get("source_printed_page"),
                    "source_page_label": page_ref.get("source_page_label"),
                    "bbox": _normalize_pdf_bbox(
                        {
                            "x0": 0.0,
                            "y0": 0.0,
                            "x1": float(page.rect.width),
                            "y1": float(page.rect.height),
                            "coord_origin": "TOPLEFT",
                        },
                        page_width=float(page.rect.width),
                        page_height=float(page.rect.height),
                    ),
                    "caption_hint": hint,
                    "picture_index": 100000 + fallback_rank,
                    "page_picture_index": 0,
                    "pil_size": (pix.width, pix.height),
                    "page_size": (float(page.rect.width), float(page.rect.height)),
                    "page_text_excerpt": page_text_excerpt,
                    "context_text": context_text,
                    "extraction_mode": (
                        "page_raster_recall_fallback"
                        if page_no in precise_picture_pages
                        else "page_raster_fallback"
                    ),
                }
            )
            fallback_rank += 1
    except ImportError:
        logger.warning(
            "[multimodal] pymupdf not installed; skipping page raster fallback."
        )
    except Exception as e:
        logger.warning(
            f"[multimodal] page raster fallback failed for "
            f"{file_path.name}: {type(e).__name__}: {e}"
        )
    finally:
        if fitz_doc is not None:
            try:
                fitz_doc.close()
            except Exception:
                pass

    raw_mode_counts.update(_count_candidates_by_mode(page_raster_candidates))
    extracted = [*precise_candidates, *page_raster_candidates]
    if len(extracted) > effective_max_images:
        extraction_truncated = True
        extracted.sort(
            key=lambda item: (
                -_multimodal_extraction_priority(item),
                -_bbox_area_ratio(item.get("bbox")),
                -_candidate_pixel_count(item),
                item.get("page_no") if item.get("page_no") is not None else 10**9,
            )
        )
        extracted = extracted[:effective_max_images]

    if extraction_truncated:
        logger.warning(
            f"[multimodal] {file_path.name}: image extraction hit max_images="
            f"{effective_max_images}. Some PDF figures may not have been extracted."
        )

    extracted.sort(
        key=lambda item: (
            item.get("page_no") if item.get("page_no") is not None else 10**9,
            1 if str(item.get("extraction_mode") or "").startswith("page_raster") else 0,
            float((item.get("bbox") or {}).get("y0") or 0.0)
            if isinstance(item.get("bbox"), dict)
            else 0.0,
            float((item.get("bbox") or {}).get("x0") or 0.0)
            if isinstance(item.get("bbox"), dict)
            else 0.0,
            -_multimodal_extraction_priority(item),
        )
    )
    per_page_picture_counts: dict[int, int] = {}
    for picture_index, item in enumerate(extracted):
        page_no = item.get("page_no")
        item["picture_index"] = picture_index
        try:
            page_no_int = int(page_no)
        except (TypeError, ValueError):
            page_no_int = None
        if page_no_int is not None:
            page_picture_index = per_page_picture_counts.get(page_no_int, 0)
            per_page_picture_counts[page_no_int] = page_picture_index + 1
            item["page_picture_index"] = page_picture_index
        item.pop("_content_hash", None)

    logger.info(
        f"[multimodal] {file_path.name}: candidate modes raw="
        f"{_format_mode_counts(raw_mode_counts)}; unique="
        f"{_format_mode_counts(_count_candidates_by_mode(extracted))}"
    )
    logger.info(
        f"[multimodal] Result for {file_path.name}: "
        f"{len(full_text):,} chars text, {len(extracted)} multimodal figures"
    )
    return full_text, extracted


def _extract_pdf_pypdf(file_bytes: bytes, password: str = None) -> str:
    """Extract PDF content using pypdf (synchronous).

    Args:
        file_bytes: PDF file content as bytes
        password: Optional password for encrypted PDFs

    Returns:
        str: Extracted text content

    Raises:
        Exception: If PDF is encrypted and password is incorrect or missing
    """
    from pypdf import PdfReader  # type: ignore

    pdf_file = BytesIO(file_bytes)
    reader = PdfReader(pdf_file)

    # Check if PDF is encrypted
    if reader.is_encrypted:
        # Try empty password first (covers permission-only encrypted PDFs)
        decrypt_result = reader.decrypt(password or "")
        if decrypt_result == 0:
            if password:
                raise Exception("Incorrect PDF password")
            else:
                raise Exception("PDF is encrypted but no password provided")

    # Extract text from all pages
    content = ""
    for page in reader.pages:
        content += page.extract_text() + "\n"

    return content


def _extract_docx(file_bytes: bytes) -> str:
    """Extract DOCX content including tables in document order (synchronous).

    Args:
        file_bytes: DOCX file content as bytes

    Returns:
        str: Extracted text content with tables in their original positions.
             Tables are separated from paragraphs with blank lines for clarity.
    """
    from docx import Document  # type: ignore
    from docx.table import Table  # type: ignore
    from docx.text.paragraph import Paragraph  # type: ignore

    docx_file = BytesIO(file_bytes)
    doc = Document(docx_file)

    def escape_cell(cell_value: str | None) -> str:
        """Escape characters that would break tab-delimited layout.

        Escape order is critical: backslashes first, then tabs/newlines.
        This prevents double-escaping issues.

        Args:
            cell_value: The cell value to escape (can be None or str)

        Returns:
            str: Escaped cell value safe for tab-delimited format
        """
        if cell_value is None:
            return ""
        text = str(cell_value)
        # CRITICAL: Escape backslash first to avoid double-escaping
        return (
            text.replace("\\", "\\\\")  # Must be first: \ -> \\
            .replace("\t", "&emsp;&emsp;")  # Tab -> \t (visible)
            .replace("\r\n", "<br>")  # Windows newline -> \n
            .replace("\r", "<br>")  # Mac newline -> \n
            .replace("\n", "<br>")  # Unix newline -> \n
        )

    content_parts = []
    in_table = False  # Track if we're currently processing a table

    # Iterate through all body elements in document order
    for element in doc.element.body:
        # Check if element is a paragraph
        if element.tag.endswith("p"):
            # If coming out of a table, add blank line after table
            if in_table:
                content_parts.append("")  # Blank line after table
                in_table = False

            paragraph = Paragraph(element, doc)
            text = paragraph.text
            # Always append to preserve document spacing (including blank paragraphs)
            content_parts.append(text)

        # Check if element is a table
        elif element.tag.endswith("tbl"):
            # Add blank line before table (if content exists)
            if content_parts and not in_table:
                content_parts.append("")  # Blank line before table

            in_table = True
            table = Table(element, doc)
            for row in table.rows:
                row_text = []
                for cell in row.cells:
                    cell_text = cell.text
                    # Escape special characters to preserve tab-delimited structure
                    row_text.append(escape_cell(cell_text))
                # Only add row if at least one cell has content
                if any(cell for cell in row_text):
                    content_parts.append("\t".join(row_text))

    return "\n".join(content_parts)


def _extract_pptx(file_bytes: bytes) -> str:
    """Extract PPTX content (synchronous).

    Args:
        file_bytes: PPTX file content as bytes

    Returns:
        str: Extracted text content
    """
    from pptx import Presentation  # type: ignore

    pptx_file = BytesIO(file_bytes)
    prs = Presentation(pptx_file)
    content = ""
    for slide in prs.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text"):
                content += shape.text + "\n"
    return content


def _extract_xlsx(file_bytes: bytes) -> str:
    """Extract XLSX content in tab-delimited format with clear sheet separation.

    This function processes Excel workbooks and converts them to a structured text format
    suitable for LLM prompts and RAG systems. Each sheet is clearly delimited with
    separator lines, and special characters are escaped to preserve the tab-delimited structure.

    Features:
    - Each sheet is wrapped with '====================' separators for visual distinction
    - Special characters (tabs, newlines, backslashes) are escaped to prevent structure corruption
    - Column alignment is preserved across all rows to maintain tabular structure
    - Empty rows are preserved as blank lines to maintain row structure
    - Uses sheet.max_column to determine column width efficiently

    Args:
        file_bytes: XLSX file content as bytes

    Returns:
        str: Extracted text content with all sheets in tab-delimited format.
             Format: Sheet separators, sheet name, then tab-delimited rows.

    Example output:
        ==================== Sheet: Data ====================
        Name\tAge\tCity
        Alice\t30\tNew York
        Bob\t25\tLondon

        ==================== Sheet: Summary ====================
        Total\t2
        ====================
    """
    from openpyxl import load_workbook  # type: ignore

    xlsx_file = BytesIO(file_bytes)
    wb = load_workbook(xlsx_file)

    def escape_cell(cell_value: str | int | float | None) -> str:
        """Escape characters that would break tab-delimited layout.

        Escape order is critical: backslashes first, then tabs/newlines.
        This prevents double-escaping issues.

        Args:
            cell_value: The cell value to escape (can be None, str, int, or float)

        Returns:
            str: Escaped cell value safe for tab-delimited format
        """
        if cell_value is None:
            return ""
        text = str(cell_value)
        # CRITICAL: Escape backslash first to avoid double-escaping
        return (
            text.replace("\\", "\\\\")  # Must be first: \ -> \\
            .replace("\t", "\\t")  # Tab -> \t (visible)
            .replace("\r\n", "\\n")  # Windows newline -> \n
            .replace("\r", "\\n")  # Mac newline -> \n
            .replace("\n", "\\n")  # Unix newline -> \n
        )

    def escape_sheet_title(title: str) -> str:
        """Escape sheet title to prevent formatting issues in separators.

        Args:
            title: Original sheet title

        Returns:
            str: Sanitized sheet title with tabs/newlines replaced
        """
        return str(title).replace("\n", " ").replace("\t", " ").replace("\r", " ")

    content_parts: list[str] = []
    sheet_separator = "=" * 20

    for idx, sheet in enumerate(wb):
        if idx > 0:
            content_parts.append("")  # Blank line between sheets for readability

        # Escape sheet title to handle edge cases with special characters
        safe_title = escape_sheet_title(sheet.title)
        content_parts.append(f"{sheet_separator} Sheet: {safe_title} {sheet_separator}")

        # Use sheet.max_column to get the maximum column width directly
        max_columns = sheet.max_column if sheet.max_column else 0

        # Extract rows with consistent width to preserve column alignment
        for row in sheet.iter_rows(values_only=True):
            row_parts = []

            # Build row up to max_columns width
            for idx in range(max_columns):
                if idx < len(row):
                    row_parts.append(escape_cell(row[idx]))
                else:
                    row_parts.append("")  # Pad short rows

            # Check if row is completely empty
            if all(part == "" for part in row_parts):
                # Preserve empty rows as blank lines (maintains row structure)
                content_parts.append("")
            else:
                # Join all columns to maintain consistent column count
                content_parts.append("\t".join(row_parts))

    # Final separator for symmetry (makes parsing easier)
    content_parts.append(sheet_separator)
    return "\n".join(content_parts)


async def pipeline_enqueue_file(
    rag: LightRAG, file_path: Path, track_id: str = None
) -> tuple[bool, str]:
    """Add a file to the queue for processing

    Args:
        rag: LightRAG instance
        file_path: Path to the saved file
        track_id: Optional tracking ID, if not provided will be generated
    Returns:
        tuple: (success: bool, track_id: str)
    """

    # Generate track_id if not provided
    if track_id is None:
        track_id = generate_track_id("unknown")

    try:
        content = ""
        handled_by_direct_image_insert = False
        # Phase 5: when a file parser extracts embedded images (currently
        # only the PDF multimodal path via docling), it populates this
        # list of image info dicts. Non-None here means the post-
        # extraction enqueue branch routes to ``ainsert_document_with_images``
        # instead of ``apipeline_enqueue_documents``. None preserves the
        # text-only enqueue path unchanged.
        extracted_images: Optional[List[Dict[str, Any]]] = None
        ext = file_path.suffix.lower()
        file_size = 0

        # Get file size for error reporting
        try:
            stat = await asyncio.to_thread(file_path.stat)
            file_size = stat.st_size
        except Exception:
            file_size = 0

        file = None
        try:
            async with aiofiles.open(file_path, "rb") as f:
                file = await f.read()
        except PermissionError as e:
            error_files = [
                {
                    "file_path": str(file_path.name),
                    "error_description": "[File Extraction]Permission denied - cannot read file",
                    "original_error": str(e),
                    "file_size": file_size,
                }
            ]
            await rag.apipeline_enqueue_error_documents(error_files, track_id)
            logger.error(
                f"[File Extraction]Permission denied reading file: {file_path.name}"
            )
            return False, track_id
        except FileNotFoundError as e:
            error_files = [
                {
                    "file_path": str(file_path.name),
                    "error_description": "[File Extraction]File not found",
                    "original_error": str(e),
                    "file_size": file_size,
                }
            ]
            await rag.apipeline_enqueue_error_documents(error_files, track_id)
            logger.error(f"[File Extraction]File not found: {file_path.name}")
            return False, track_id
        except Exception as e:
            error_files = [
                {
                    "file_path": str(file_path.name),
                    "error_description": "[File Extraction]File reading error",
                    "original_error": str(e),
                    "file_size": file_size,
                }
            ]
            await rag.apipeline_enqueue_error_documents(error_files, track_id)
            logger.error(
                f"[File Extraction]Error reading file {file_path.name}: {str(e)}"
            )
            return False, track_id

        # Process based on file type
        try:
            if _is_direct_image_extension(ext):
                if (
                    rag.image_embedding_func is None
                    or rag.images_vdb is None
                    or rag.image_blob_store is None
                ):
                    error_files = [
                        {
                            "file_path": str(file_path.name),
                            "error_description": "[File Extraction]Multimodal image pipeline not enabled",
                            "original_error": "Image uploads require the multimodal pipeline to be configured on the server",
                            "file_size": file_size,
                        }
                    ]
                    await rag.apipeline_enqueue_error_documents(error_files, track_id)
                    logger.error(
                        f"[File Extraction]Multimodal image pipeline not enabled for {file_path.name}"
                    )
                    return False, track_id

                try:
                    await rag.ainsert_image(
                        file,
                        file_path=file_path.name,
                        mime_type=_guess_image_mime_type(file_path),
                        extra_metadata={"source_kind": "direct_image_upload"},
                        track_id=track_id,
                    )
                    handled_by_direct_image_insert = True
                    logger.info(
                        f"Successfully extracted and enqueued image file: {file_path.name}"
                    )
                except Exception as e:
                    error_files = [
                        {
                            "file_path": str(file_path.name),
                            "error_description": "[File Extraction]Image processing error",
                            "original_error": f"Failed to ingest image: {str(e)}",
                            "file_size": file_size,
                        }
                    ]
                    await rag.apipeline_enqueue_error_documents(error_files, track_id)
                    logger.error(
                        f"[File Extraction]Error processing image {file_path.name}: {str(e)}"
                    )
                    return False, track_id
            else:
                match ext:
                    case (
                        ".txt"
                        | ".md"
                        | ".mdx"
                        | ".html"
                        | ".htm"
                        | ".tex"
                        | ".json"
                        | ".xml"
                        | ".yaml"
                        | ".yml"
                        | ".rtf"
                        | ".odt"
                        | ".epub"
                        | ".csv"
                        | ".log"
                        | ".conf"
                        | ".ini"
                        | ".properties"
                        | ".sql"
                        | ".bat"
                        | ".sh"
                        | ".c"
                        | ".h"
                        | ".cpp"
                        | ".hpp"
                        | ".py"
                        | ".java"
                        | ".js"
                        | ".ts"
                        | ".swift"
                        | ".go"
                        | ".rb"
                        | ".php"
                        | ".css"
                        | ".scss"
                        | ".less"
                    ):
                        try:
                            # Try to decode as UTF-8 (offloaded to thread to avoid blocking the event loop)
                            content = await asyncio.to_thread(file.decode, "utf-8")

                            # Validate content
                            if not content or len(content.strip()) == 0:
                                error_files = [
                                    {
                                        "file_path": str(file_path.name),
                                        "error_description": "[File Extraction]Empty file content",
                                        "original_error": "File contains no content or only whitespace",
                                        "file_size": file_size,
                                    }
                                ]
                                await rag.apipeline_enqueue_error_documents(
                                    error_files, track_id
                                )
                                logger.error(
                                    f"[File Extraction]Empty content in file: {file_path.name}"
                                )
                                return False, track_id

                            # Check if content looks like binary data string representation
                            if content.startswith("b'") or content.startswith('b"'):
                                error_files = [
                                    {
                                        "file_path": str(file_path.name),
                                        "error_description": "[File Extraction]Binary data in text file",
                                        "original_error": "File appears to contain binary data representation instead of text",
                                        "file_size": file_size,
                                    }
                                ]
                                await rag.apipeline_enqueue_error_documents(
                                    error_files, track_id
                                )
                                logger.error(
                                    f"[File Extraction]File {file_path.name} appears to contain binary data representation instead of text"
                                )
                                return False, track_id

                        except UnicodeDecodeError as e:
                            error_files = [
                                {
                                    "file_path": str(file_path.name),
                                    "error_description": "[File Extraction]UTF-8 encoding error, please convert it to UTF-8 before processing",
                                    "original_error": f"File is not valid UTF-8 encoded text: {str(e)}",
                                    "file_size": file_size,
                                }
                            ]
                            await rag.apipeline_enqueue_error_documents(
                                error_files, track_id
                            )
                            logger.error(
                                f"[File Extraction]File {file_path.name} is not valid UTF-8 encoded text. Please convert it to UTF-8 before processing."
                            )
                            return False, track_id

                    case ".pdf":
                        try:
                            # Phase 5+: when the multimodal pipeline is wired up
                            # on this LightRAG instance, always run the richer
                            # PDF converter. It now degrades gracefully across
                            # Docling semantic crops, PyMuPDF native embedded
                            # images, PyMuPDF image blocks, and finally page
                            # raster fallback — so docling availability is no
                            # longer a hard requirement for multimodal ingest.
                            multimodal_ready = (
                                rag.image_embedding_func is not None
                                and rag.images_vdb is not None
                                and rag.image_blob_store is not None
                                and rag.image_metadata is not None
                            )
                            if multimodal_ready:
                                (
                                    content,
                                    extracted_images,
                                ) = await asyncio.to_thread(
                                    _convert_with_docling_multimodal, file_path
                                )
                                logger.info(
                                    f"[File Extraction] PDF multimodal: "
                                    f"{file_path.name} -> {len(extracted_images)} embedded images"
                                )
                            # Try DOCLING first if configured and available
                            elif (
                                global_args.document_loading_engine == "DOCLING"
                                and _is_docling_available()
                            ):
                                content = await asyncio.to_thread(
                                    _convert_with_docling, file_path
                                )
                            else:
                                if (
                                    global_args.document_loading_engine == "DOCLING"
                                    and not _is_docling_available()
                                ):
                                    logger.warning(
                                        f"DOCLING engine configured but not available for {file_path.name}. Falling back to pypdf."
                                    )
                                # Use pypdf (non-blocking via to_thread)
                                content = await asyncio.to_thread(
                                    _extract_pdf_pypdf,
                                    file,
                                    global_args.pdf_decrypt_password,
                                )
                        except Exception as e:
                            error_files = [
                                {
                                    "file_path": str(file_path.name),
                                    "error_description": "[File Extraction]PDF processing error",
                                    "original_error": f"Failed to extract text from PDF: {str(e)}",
                                    "file_size": file_size,
                                }
                            ]
                            await rag.apipeline_enqueue_error_documents(
                                error_files, track_id
                            )
                            logger.error(
                                f"[File Extraction]Error processing PDF {file_path.name}: {str(e)}"
                            )
                            return False, track_id

                    case ".docx":
                        try:
                            # Try DOCLING first if configured and available
                            if (
                                global_args.document_loading_engine == "DOCLING"
                                and _is_docling_available()
                            ):
                                content = await asyncio.to_thread(
                                    _convert_with_docling, file_path
                                )
                            else:
                                if (
                                    global_args.document_loading_engine == "DOCLING"
                                    and not _is_docling_available()
                                ):
                                    logger.warning(
                                        f"DOCLING engine configured but not available for {file_path.name}. Falling back to python-docx."
                                    )
                                # Use python-docx (non-blocking via to_thread)
                                content = await asyncio.to_thread(_extract_docx, file)
                        except Exception as e:
                            error_files = [
                                {
                                    "file_path": str(file_path.name),
                                    "error_description": "[File Extraction]DOCX processing error",
                                    "original_error": f"Failed to extract text from DOCX: {str(e)}",
                                    "file_size": file_size,
                                }
                            ]
                            await rag.apipeline_enqueue_error_documents(
                                error_files, track_id
                            )
                            logger.error(
                                f"[File Extraction]Error processing DOCX {file_path.name}: {str(e)}"
                            )
                            return False, track_id

                    case ".pptx":
                        try:
                            # Try DOCLING first if configured and available
                            if (
                                global_args.document_loading_engine == "DOCLING"
                                and _is_docling_available()
                            ):
                                content = await asyncio.to_thread(
                                    _convert_with_docling, file_path
                                )
                            else:
                                if (
                                    global_args.document_loading_engine == "DOCLING"
                                    and not _is_docling_available()
                                ):
                                    logger.warning(
                                        f"DOCLING engine configured but not available for {file_path.name}. Falling back to python-pptx."
                                    )
                                # Use python-pptx (non-blocking via to_thread)
                                content = await asyncio.to_thread(_extract_pptx, file)
                        except Exception as e:
                            error_files = [
                                {
                                    "file_path": str(file_path.name),
                                    "error_description": "[File Extraction]PPTX processing error",
                                    "original_error": f"Failed to extract text from PPTX: {str(e)}",
                                    "file_size": file_size,
                                }
                            ]
                            await rag.apipeline_enqueue_error_documents(
                                error_files, track_id
                            )
                            logger.error(
                                f"[File Extraction]Error processing PPTX {file_path.name}: {str(e)}"
                            )
                            return False, track_id

                    case ".xlsx":
                        try:
                            # Try DOCLING first if configured and available
                            if (
                                global_args.document_loading_engine == "DOCLING"
                                and _is_docling_available()
                            ):
                                content = await asyncio.to_thread(
                                    _convert_with_docling, file_path
                                )
                            else:
                                if (
                                    global_args.document_loading_engine == "DOCLING"
                                    and not _is_docling_available()
                                ):
                                    logger.warning(
                                        f"DOCLING engine configured but not available for {file_path.name}. Falling back to openpyxl."
                                    )
                                # Use openpyxl (non-blocking via to_thread)
                                content = await asyncio.to_thread(_extract_xlsx, file)
                        except Exception as e:
                            error_files = [
                                {
                                    "file_path": str(file_path.name),
                                    "error_description": "[File Extraction]XLSX processing error",
                                    "original_error": f"Failed to extract text from XLSX: {str(e)}",
                                    "file_size": file_size,
                                }
                            ]
                            await rag.apipeline_enqueue_error_documents(
                                error_files, track_id
                            )
                            logger.error(
                                f"[File Extraction]Error processing XLSX {file_path.name}: {str(e)}"
                            )
                            return False, track_id

                    case _:
                        error_files = [
                            {
                                "file_path": str(file_path.name),
                                "error_description": f"[File Extraction]Unsupported file type: {ext}",
                                "original_error": f"File extension {ext} is not supported",
                                "file_size": file_size,
                            }
                        ]
                        await rag.apipeline_enqueue_error_documents(
                            error_files, track_id
                        )
                        logger.error(
                            f"[File Extraction]Unsupported file type: {file_path.name} (extension {ext})"
                        )
                        return False, track_id

        except Exception as e:
            error_files = [
                {
                    "file_path": str(file_path.name),
                    "error_description": "[File Extraction]File format processing error",
                    "original_error": f"Unexpected error during file extracting: {str(e)}",
                    "file_size": file_size,
                }
            ]
            await rag.apipeline_enqueue_error_documents(error_files, track_id)
            logger.error(
                f"[File Extraction]Unexpected error during {file_path.name} extracting: {str(e)}"
            )
            return False, track_id

        if handled_by_direct_image_insert:
            try:
                await _move_file_to_enqueued_directory(file_path)
            except Exception as move_error:
                logger.error(
                    f"Failed to move image file {file_path.name} to __enqueued__ directory: {move_error}"
                )
            return True, track_id

        # Insert into the RAG queue.
        # When the multimodal pipeline extracted images (extracted_images
        # is a non-empty list), the document is valid even if the TEXT
        # content is empty or whitespace-only — art books, drawing sets,
        # and photo albums are essentially all-image PDFs.
        has_images = bool(extracted_images)
        has_text = bool(content and content.strip())

        if has_text or has_images:
            if not has_text and has_images:
                # All-image document: use a minimal placeholder so the
                # downstream text pipeline has something to chunk.
                content = content or ""
                logger.info(
                    f"[File Extraction] {file_path.name}: text is empty/whitespace "
                    f"but {len(extracted_images)} images were extracted — "
                    f"proceeding with image-only ingest."
                )
            elif not has_text:
                error_files = [
                    {
                        "file_path": str(file_path.name),
                        "error_description": "[File Extraction]File contains only whitespace",
                        "original_error": "File content contains only whitespace characters",
                        "file_size": file_size,
                    }
                ]
                await rag.apipeline_enqueue_error_documents(error_files, track_id)
                logger.warning(
                    f"[File Extraction]File contains only whitespace characters: {file_path.name}"
                )
                return False, track_id

            try:
                if extracted_images is not None:
                    # Enqueue the PLAIN text first so the frontend
                    # immediately sees the document as PENDING. Image
                    # processing happens next (~2 min with concurrency),
                    # then the text pipeline processes the augmented
                    # content (text + image annotations).
                    await rag.ainsert_document_with_images(
                        text_content=content,
                        extracted_images=extracted_images,
                        file_path=file_path.name,
                        track_id=track_id,
                    )
                    logger.info(
                        f"Successfully extracted and enqueued file with "
                        f"{len(extracted_images)} embedded images: "
                        f"{file_path.name}"
                    )
                else:
                    await rag.apipeline_enqueue_documents(
                        content, file_paths=file_path.name, track_id=track_id
                    )

                    logger.info(
                        f"Successfully extracted and enqueued file: {file_path.name}"
                    )

                # Move file to __enqueued__ directory after enqueuing
                try:
                    await _move_file_to_enqueued_directory(file_path)
                except Exception as move_error:
                    logger.error(
                        f"Failed to move file {file_path.name} to __enqueued__ directory: {move_error}"
                    )
                    # Don't affect the main function's success status

                return True, track_id

            except Exception as e:
                error_files = [
                    {
                        "file_path": str(file_path.name),
                        "error_description": "Document enqueue error",
                        "original_error": f"Failed to enqueue document: {str(e)}",
                        "file_size": file_size,
                    }
                ]
                await rag.apipeline_enqueue_error_documents(error_files, track_id)
                logger.error(f"Error enqueueing document {file_path.name}: {str(e)}")
                return False, track_id
        else:
            error_files = [
                {
                    "file_path": str(file_path.name),
                    "error_description": "No content extracted",
                    "original_error": "No content could be extracted from file",
                    "file_size": file_size,
                }
            ]
            await rag.apipeline_enqueue_error_documents(error_files, track_id)
            logger.error(f"No content extracted from file: {file_path.name}")
            return False, track_id

    except Exception as e:
        # Catch-all for any unexpected errors
        try:
            file_size = file_path.stat().st_size if file_path.exists() else 0
        except Exception:
            file_size = 0

        error_files = [
            {
                "file_path": str(file_path.name),
                "error_description": "Unexpected processing error",
                "original_error": f"Unexpected error: {str(e)}",
                "file_size": file_size,
            }
        ]
        await rag.apipeline_enqueue_error_documents(error_files, track_id)
        logger.error(f"Enqueuing file {file_path.name} error: {str(e)}")
        logger.error(traceback.format_exc())
        return False, track_id
    finally:
        if file_path.name.startswith(temp_prefix):
            try:
                file_path.unlink()
            except Exception as e:
                logger.error(f"Error deleting file {file_path}: {str(e)}")


async def pipeline_index_file(
    rag: LightRAG,
    file_path: Path,
    track_id: str = None,
    preclaimed: bool = False,
) -> bool:
    """Index a file with track_id

    Args:
        rag: LightRAG instance
        file_path: Path to the saved file
        track_id: Optional tracking ID
        preclaimed: Whether the caller already claimed the file for exclusive
            processing.
    """
    if track_id is None:
        track_id = generate_track_id("unknown")

    claim_acquired = False
    try:
        if not preclaimed:
            claimed, existing_owner = await _claim_input_file(
                rag, file_path.name, track_id
            )
            if not claimed:
                logger.info(
                    f"Skipping already claimed file {file_path.name} "
                    f"(owner={existing_owner})"
                )
                return False
            claim_acquired = True
        else:
            claim_acquired = True

        success, _ = await pipeline_enqueue_file(rag, file_path, track_id)
        if success:
            await rag.apipeline_process_enqueue_documents()
        return success

    except Exception as e:
        logger.error(f"Error indexing file {file_path.name}: {str(e)}")
        logger.error(traceback.format_exc())
        return False
    finally:
        if claim_acquired:
            await _release_input_file_claim(rag, file_path.name, track_id)


async def pipeline_index_files(
    rag: LightRAG,
    file_paths: List[Path],
    track_id: str = None,
    preclaimed: bool = False,
):
    """Index multiple files sequentially to avoid high CPU load

    Args:
        rag: LightRAG instance
        file_paths: Paths to the files to index
        track_id: Optional tracking ID to pass to all files
    """
    if not file_paths:
        return
    try:
        # Use get_pinyin_sort_key for Chinese pinyin sorting
        sorted_file_paths = sorted(
            file_paths, key=lambda p: get_pinyin_sort_key(str(p))
        )

        # Process files sequentially with track_id
        for file_path in sorted_file_paths:
            await pipeline_index_file(rag, file_path, track_id, preclaimed=preclaimed)

    except Exception as e:
        logger.error(f"Error indexing files: {str(e)}")
        logger.error(traceback.format_exc())


async def pipeline_index_texts(
    rag: LightRAG,
    texts: List[str],
    file_sources: List[str] = None,
    track_id: str = None,
):
    """Index a list of texts with track_id

    Args:
        rag: LightRAG instance
        texts: The texts to index
        file_sources: Sources of the texts
        track_id: Optional tracking ID
    """
    if not texts:
        return

    normalized_file_sources: list[str] | None = None
    if file_sources:
        normalized_file_sources = [
            normalize_file_path(source) for source in file_sources
        ]
        if len(normalized_file_sources) > len(texts):
            raise ValueError("Number of file sources must not exceed number of texts")
        if len(normalized_file_sources) < len(texts):
            normalized_file_sources.extend(
                [UNKNOWN_FILE_SOURCE] * (len(texts) - len(normalized_file_sources))
            )

    await rag.apipeline_enqueue_documents(
        input=texts, file_paths=normalized_file_sources, track_id=track_id
    )
    await rag.apipeline_process_enqueue_documents()


async def run_scanning_process(
    rag: LightRAG, doc_manager: DocumentManager, track_id: str = None
):
    """Background task to scan and index documents

    Args:
        rag: LightRAG instance
        doc_manager: DocumentManager instance
        track_id: Optional tracking ID to pass to all scanned files
    """
    scan_state, scan_state_lock = await _get_document_scan_state(rag)

    async with scan_state_lock:
        if scan_state.get("busy", False):
            scan_state["request_pending"] = True
            logger.info(
                "Document scan already in progress. Queued an additional scan pass."
            )
            return
        scan_state["busy"] = True
        scan_state["request_pending"] = False

    try:
        while True:
            new_files = doc_manager.scan_directory_for_new_files()
            total_files = len(new_files)
            logger.info(f"Found {total_files} files to index.")

            if new_files:
                valid_files = []
                skipped_files = []

                for file_path in new_files:
                    filename = file_path.name
                    existing_doc_data = await rag.doc_status.get_doc_by_file_path(
                        filename
                    )
                    existing_status = (
                        _coerce_doc_status_value(existing_doc_data.get("status"))
                        if existing_doc_data
                        else None
                    )

                    if existing_doc_data and _should_skip_scan_for_status(
                        existing_status
                    ):
                        skipped_files.append(filename)
                        logger.info(
                            f"Skipping file already tracked by pipeline: "
                            f"{filename} (status={existing_status})"
                        )
                    else:
                        valid_files.append(file_path)

                if valid_files:
                    await pipeline_index_files(rag, valid_files, track_id)
                    if skipped_files:
                        logger.info(
                            f"Scanning process completed: {len(valid_files)} files processed, "
                            f"{len(skipped_files)} skipped."
                        )
                    else:
                        logger.info(
                            f"Scanning process completed: {len(valid_files)} files processed."
                        )
                else:
                    logger.info(
                        "No files to process after filtering already tracked documents."
                    )
            else:
                logger.info(
                    "No upload file found, check if there are any documents in the queue..."
                )
                await rag.apipeline_process_enqueue_documents()

            async with scan_state_lock:
                has_pending_request = scan_state.get("request_pending", False)
                scan_state["request_pending"] = False

            if not has_pending_request:
                break

            logger.info(
                "Running an additional scan pass because another scan request arrived."
            )

    except Exception as e:
        logger.error(f"Error during scanning process: {str(e)}")
        logger.error(traceback.format_exc())
    finally:
        async with scan_state_lock:
            scan_state["busy"] = False
            scan_state["request_pending"] = False


async def background_rebuild_document_multimodal(
    rag: LightRAG,
    doc_manager: DocumentManager,
    doc_id: str,
    track_id: str,
    reuse_cache: bool = True,
    release_input_claim_filename: str | None = None,
):
    """Background task: rebuild multimodal assets for one tracked PDF."""
    original_doc_status: dict[str, Any] | None = None
    try:
        doc_status = await rag.doc_status.get_by_id(doc_id)
        if doc_status is None:
            logger.error(
                f"[rebuild_multimodal] unknown document id={doc_id}, nothing to rebuild"
            )
            return
        original_doc_status = dict(doc_status)

        logical_file_path = normalize_file_path(doc_status.get("file_path"))
        processing_record = _build_multimodal_rebuild_status_record(
            doc_status,
            track_id=track_id,
            file_path=logical_file_path,
            stage="extracting_source_pdf",
        )
        await rag.doc_status.upsert({doc_id: processing_record})
        await rag.doc_status.index_done_callback()

        source_file = _resolve_document_source_file(doc_manager, logical_file_path)
        if source_file is not None:
            logger.info(
                f"[rebuild_multimodal] extracting PDF again for doc_id={doc_id} "
                f"source={source_file.name} reuse_cache={reuse_cache}"
            )
            text_content, extracted_images = await asyncio.to_thread(
                _convert_with_docling_multimodal, source_file
            )
        else:
            logger.warning(
                f"[rebuild_multimodal] source PDF missing for doc_id={doc_id} "
                f"({logical_file_path}); falling back to existing multimodal assets"
            )
            text_content, extracted_images = (
                await rag.areconstruct_document_multimodal_payload(doc_id)
            )

        await rag.arebuild_document_multimodal(
            doc_id=doc_id,
            text_content=text_content,
            extracted_images=extracted_images,
            file_path=Path(logical_file_path).name if logical_file_path else source_file.name,
            track_id=track_id,
            reuse_existing_images=reuse_cache,
        )
        logger.info(
            f"[rebuild_multimodal] completed doc_id={doc_id} "
            f"images={len(extracted_images)} track_id={track_id}"
        )
    except Exception as e:
        logger.error(
            f"[rebuild_multimodal] failed for doc_id={doc_id}: "
            f"{type(e).__name__}: {e}"
        )
        logger.error(traceback.format_exc())
        try:
            current_status = await rag.doc_status.get_by_id(doc_id)
            failure_source = current_status or original_doc_status
            if failure_source is not None:
                failed_record = _build_multimodal_rebuild_status_record(
                    failure_source,
                    track_id=track_id,
                    file_path=normalize_file_path(
                        failure_source.get("file_path") if isinstance(failure_source, dict) else None
                    ),
                    stage="failed",
                    error_msg=f"Multimodal rebuild failed: {e}",
                )
                failed_record["status"] = DocStatus.FAILED
                failed_record["metadata"] = {
                    **dict(failed_record.get("metadata", {}) or {}),
                    "multimodal_rebuild_in_progress": False,
                }
                await rag.doc_status.upsert({doc_id: failed_record})
                await rag.doc_status.index_done_callback()
        except Exception as persist_error:
            logger.warning(
                f"[rebuild_multimodal] failed to persist failure state for {doc_id}: "
                f"{type(persist_error).__name__}: {persist_error}"
            )
    finally:
        if release_input_claim_filename:
            try:
                await _release_input_file_claim(
                    rag, release_input_claim_filename, track_id
                )
            except Exception as release_error:
                logger.warning(
                    f"[rebuild_multimodal] failed to release input-file claim "
                    f"for {release_input_claim_filename}: "
                    f"{type(release_error).__name__}: {release_error}"
                )


async def background_delete_documents(
    rag: LightRAG,
    doc_manager: DocumentManager,
    doc_ids: List[str],
    delete_file: bool = False,
    delete_llm_cache: bool = False,
):
    """Background task to delete multiple documents"""
    from lightrag.kg.shared_storage import (
        get_namespace_data,
        get_namespace_lock,
    )

    pipeline_status = await get_namespace_data(
        "pipeline_status", workspace=rag.workspace
    )
    pipeline_status_lock = get_namespace_lock(
        "pipeline_status", workspace=rag.workspace
    )

    total_docs = len(doc_ids)
    deleting_doc_ids = set(doc_ids)
    successful_deletions = []
    failed_deletions = []

    # Double-check pipeline status before proceeding
    async with pipeline_status_lock:
        if pipeline_status.get("busy", False):
            logger.warning("Error: Unexpected pipeline busy state, aborting deletion.")
            return  # Abort deletion operation

        # Set pipeline status to busy for deletion
        pipeline_status.update(
            {
                "busy": True,
                # Job name can not be changed, it's verified in adelete_by_doc_id()
                "job_name": f"Deleting {total_docs} Documents",
                "job_start": datetime.now().isoformat(),
                "docs": total_docs,
                "batchs": total_docs,
                "cur_batch": 0,
                "total_chunks": 0,
                "processed_chunks": 0,
                "current_stage": "document_deletion",
                "current_stage_label": "Deleting documents",
                "stage_unit": "documents",
                "stage_total": total_docs,
                "stage_processed": 0,
                "stage_remaining": total_docs,
                "stage_elapsed_seconds": 0,
                "stage_eta_seconds": None,
                "latest_message": "Starting document deletion process",
            }
        )
        # Use slice assignment to clear the list in place
        pipeline_status["history_messages"][:] = ["Starting document deletion process"]
        if delete_llm_cache:
            pipeline_status["history_messages"].append(
                "LLM cache cleanup requested for this deletion job"
            )

    try:
        # Loop through each document ID and delete them one by one
        for i, doc_id in enumerate(doc_ids, 1):
            # Check for cancellation at the start of each document deletion
            async with pipeline_status_lock:
                if pipeline_status.get("cancellation_requested", False):
                    cancel_msg = f"Deletion cancelled by user at document {i}/{total_docs}. {len(successful_deletions)} deleted, {total_docs - i + 1} remaining."
                    logger.info(cancel_msg)
                    pipeline_status["latest_message"] = cancel_msg
                    pipeline_status["history_messages"].append(cancel_msg)
                    # Add remaining documents to failed list with cancellation reason
                    failed_deletions.extend(
                        doc_ids[i - 1 :]
                    )  # i-1 because enumerate starts at 1
                    break  # Exit the loop, remaining documents unchanged

                start_msg = f"Deleting document {i}/{total_docs}: {doc_id}"
                logger.info(start_msg)
                pipeline_status["cur_batch"] = i
                pipeline_status["latest_message"] = start_msg
                pipeline_status["history_messages"].append(start_msg)

            file_path = "#"
            try:
                result = await rag.adelete_by_doc_id(
                    doc_id, delete_llm_cache=delete_llm_cache
                )
                file_path = (
                    getattr(result, "file_path", "-") if "result" in locals() else "-"
                )
                if result.status == "success":
                    successful_deletions.append(doc_id)
                    success_msg = (
                        f"Document deleted {i}/{total_docs}: {doc_id}[{file_path}]"
                    )
                    logger.info(success_msg)
                    async with pipeline_status_lock:
                        pipeline_status["history_messages"].append(success_msg)

                    # Handle file deletion if requested and file_path is available
                    if (
                        delete_file
                        and result.file_path
                        and result.file_path != "unknown_source"
                    ):
                        try:
                            deleted_files = []
                            if await _is_file_path_still_referenced(
                                rag,
                                result.file_path,
                                excluding_doc_ids=deleting_doc_ids,
                            ):
                                file_skip_msg = (
                                    f"Skipping source file deletion because another "
                                    f"document still references it: {result.file_path}"
                                )
                                logger.info(file_skip_msg)
                                async with pipeline_status_lock:
                                    pipeline_status["latest_message"] = file_skip_msg
                                    pipeline_status["history_messages"].append(
                                        file_skip_msg
                                    )
                                continue
                            # SECURITY FIX: Use secure path validation to prevent arbitrary file deletion
                            safe_file_path = validate_file_path_security(
                                result.file_path, doc_manager.input_dir
                            )

                            if safe_file_path is None:
                                # Security violation detected - log and skip file deletion
                                security_msg = f"Security violation: Unsafe file path detected for deletion - {result.file_path}"
                                logger.warning(security_msg)
                                async with pipeline_status_lock:
                                    pipeline_status["latest_message"] = security_msg
                                    pipeline_status["history_messages"].append(
                                        security_msg
                                    )
                            else:
                                # check and delete files from input_dir directory
                                if safe_file_path.exists():
                                    try:
                                        safe_file_path.unlink()
                                        deleted_files.append(safe_file_path.name)
                                        file_delete_msg = f"Successfully deleted input_dir file: {result.file_path}"
                                        logger.info(file_delete_msg)
                                        async with pipeline_status_lock:
                                            pipeline_status["latest_message"] = (
                                                file_delete_msg
                                            )
                                            pipeline_status["history_messages"].append(
                                                file_delete_msg
                                            )
                                    except Exception as file_error:
                                        file_error_msg = f"Failed to delete input_dir file {result.file_path}: {str(file_error)}"
                                        logger.debug(file_error_msg)
                                        async with pipeline_status_lock:
                                            pipeline_status["latest_message"] = (
                                                file_error_msg
                                            )
                                            pipeline_status["history_messages"].append(
                                                file_error_msg
                                            )

                                # Also check and delete files from __enqueued__ directory
                                enqueued_dir = doc_manager.input_dir / "__enqueued__"
                                if enqueued_dir.exists():
                                    # SECURITY FIX: Validate that the file path is safe before processing
                                    # Only proceed if the original path validation passed
                                    base_name = Path(result.file_path).stem
                                    extension = Path(result.file_path).suffix

                                    # Search for exact match and files with numeric suffixes
                                    for enqueued_file in enqueued_dir.glob(
                                        f"{base_name}*{extension}"
                                    ):
                                        # Additional security check: ensure enqueued file is within enqueued directory
                                        safe_enqueued_path = (
                                            validate_file_path_security(
                                                enqueued_file.name, enqueued_dir
                                            )
                                        )
                                        if safe_enqueued_path is not None:
                                            try:
                                                enqueued_file.unlink()
                                                deleted_files.append(enqueued_file.name)
                                                logger.info(
                                                    f"Successfully deleted enqueued file: {enqueued_file.name}"
                                                )
                                            except Exception as enqueued_error:
                                                file_error_msg = f"Failed to delete enqueued file {enqueued_file.name}: {str(enqueued_error)}"
                                                logger.debug(file_error_msg)
                                                async with pipeline_status_lock:
                                                    pipeline_status[
                                                        "latest_message"
                                                    ] = file_error_msg
                                                    pipeline_status[
                                                        "history_messages"
                                                    ].append(file_error_msg)
                                        else:
                                            security_msg = f"Security violation: Unsafe enqueued file path detected - {enqueued_file.name}"
                                            logger.warning(security_msg)

                            if deleted_files == []:
                                file_error_msg = f"File deletion skipped, missing or unsafe file: {result.file_path}"
                                logger.warning(file_error_msg)
                                async with pipeline_status_lock:
                                    pipeline_status["latest_message"] = file_error_msg
                                    pipeline_status["history_messages"].append(
                                        file_error_msg
                                    )

                        except Exception as file_error:
                            file_error_msg = f"Failed to delete file {result.file_path}: {str(file_error)}"
                            logger.error(file_error_msg)
                            async with pipeline_status_lock:
                                pipeline_status["latest_message"] = file_error_msg
                                pipeline_status["history_messages"].append(
                                    file_error_msg
                                )
                    elif delete_file:
                        no_file_msg = (
                            f"File deletion skipped, missing file path: {doc_id}"
                        )
                        logger.warning(no_file_msg)
                        async with pipeline_status_lock:
                            pipeline_status["latest_message"] = no_file_msg
                            pipeline_status["history_messages"].append(no_file_msg)
                else:
                    failed_deletions.append(doc_id)
                    error_msg = f"Failed to delete {i}/{total_docs}: {doc_id}[{file_path}] - {result.message}"
                    logger.error(error_msg)
                    async with pipeline_status_lock:
                        pipeline_status["latest_message"] = error_msg
                        pipeline_status["history_messages"].append(error_msg)

            except Exception as e:
                failed_deletions.append(doc_id)
                error_msg = f"Error deleting document {i}/{total_docs}: {doc_id}[{file_path}] - {str(e)}"
                logger.error(error_msg)
                logger.error(traceback.format_exc())
                async with pipeline_status_lock:
                    pipeline_status["latest_message"] = error_msg
                    pipeline_status["history_messages"].append(error_msg)

    except Exception as e:
        error_msg = f"Critical error during batch deletion: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        async with pipeline_status_lock:
            pipeline_status["history_messages"].append(error_msg)
    finally:
        # Final summary and check for pending requests
        async with pipeline_status_lock:
            pipeline_status["busy"] = False
            pipeline_status["pending_requests"] = False  # Reset pending requests flag
            pipeline_status["cancellation_requested"] = (
                False  # Always reset cancellation flag
            )
            completion_msg = f"Deletion completed: {len(successful_deletions)} successful, {len(failed_deletions)} failed"
            pipeline_status["latest_message"] = completion_msg
            pipeline_status["history_messages"].append(completion_msg)

            # Check if there are pending document indexing requests
            has_pending_request = pipeline_status.get("request_pending", False)

        # If there are pending requests, start document processing pipeline
        if has_pending_request:
            try:
                logger.info(
                    "Processing pending document indexing requests after deletion"
                )
                await rag.apipeline_process_enqueue_documents()
            except Exception as e:
                logger.error(f"Error processing pending documents after deletion: {e}")


def create_document_routes(
    rag: LightRAG | None = None,
    doc_manager: DocumentManager | None = None,
    api_key: Optional[str] = None,
):
    document_view_permission = require_permission(Action.KB_VIEW, api_key)
    document_upload_permission = require_permission(Action.KB_UPLOAD_DOCUMENT, api_key)
    document_delete_permission = require_permission(Action.KB_DELETE_DOCUMENT, api_key)
    graph_edit_permission = require_permission(Action.KB_EDIT_GRAPH, api_key)
    settings_permission = require_permission(Action.KB_MANAGE_SETTINGS, api_key)

    async def resolve_route_rag(request: Request) -> LightRAG:
        if rag is not None:
            return rag
        return await get_current_rag(request)

    async def resolve_route_doc_manager(
        request: Request,
        active_rag: LightRAG = Depends(resolve_route_rag),
    ) -> DocumentManager:
        if doc_manager is not None:
            return doc_manager

        state = request.app.state
        default_doc_manager = getattr(state, "default_doc_manager", None)
        if not getattr(state, "enable_kb_isolation", False):
            if default_doc_manager is not None:
                return default_doc_manager

            base_input_dir = getattr(state, "doc_manager_base_input_dir", "./inputs")
            workspace = getattr(
                state,
                "default_runtime_workspace",
                getattr(active_rag, "workspace", None),
            )
            return DocumentManager(base_input_dir, workspace=workspace)

        request_context = get_request_context(request)
        workspace_id = request_context.workspace_id or getattr(
            state, "default_workspace_id", "default"
        )
        kb_id = request_context.kb_id or getattr(state, "default_kb_id", "default")
        runtime_workspace = compose_runtime_workspace(state, workspace_id, kb_id)
        cache = getattr(state, "doc_manager_cache", None)
        if cache is None:
            cache = {}
            state.doc_manager_cache = cache

        manager = cache.get(runtime_workspace)
        if manager is None:
            base_input_dir = getattr(state, "doc_manager_base_input_dir", "./inputs")
            manager = DocumentManager(base_input_dir, workspace=runtime_workspace)
            cache[runtime_workspace] = manager
        return manager

    def _resolve_active_rag(active_rag: Any) -> LightRAG:
        if isinstance(active_rag, DependsParameter):
            if rag is None:
                raise RuntimeError("Direct route call requires an explicit LightRAG.")
            return rag
        return active_rag

    def _resolve_active_doc_manager(active_doc_manager: Any) -> DocumentManager:
        if isinstance(active_doc_manager, DependsParameter):
            if doc_manager is None:
                raise RuntimeError(
                    "Direct route call requires an explicit DocumentManager."
                )
            return doc_manager
        return active_doc_manager

    @router.post(
        "/scan",
        response_model=ScanResponse,
        dependencies=[Depends(document_upload_permission)],
    )
    async def scan_for_new_documents(
        background_tasks: BackgroundTasks,
        active_rag: LightRAG = Depends(resolve_route_rag),
        active_doc_manager: DocumentManager = Depends(resolve_route_doc_manager),
    ):
        """
        Trigger the scanning process for new documents.

        This endpoint initiates a background task that scans the input directory for new documents
        and processes them. If a scanning process is already running, it returns a status indicating
        that fact.

        Returns:
            ScanResponse: A response object containing the scanning status and track_id
        """
        rag = _resolve_active_rag(active_rag)
        doc_manager = _resolve_active_doc_manager(active_doc_manager)
        # Generate track_id with "scan" prefix for scanning operation
        track_id = generate_track_id("scan")

        # Start the scanning process in the background with track_id
        background_tasks.add_task(run_scanning_process, rag, doc_manager, track_id)
        return ScanResponse(
            status="scanning_started",
            message="Scanning process has been initiated in the background",
            track_id=track_id,
        )

    @router.post(
        "/upload",
        response_model=InsertResponse,
        dependencies=[Depends(document_upload_permission)],
    )
    async def upload_to_input_dir(
        background_tasks: BackgroundTasks,
        file: UploadFile = File(...),
        active_rag: LightRAG = Depends(resolve_route_rag),
        active_doc_manager: DocumentManager = Depends(resolve_route_doc_manager),
    ):
        """
        Upload a file to the input directory and index it.

        This API endpoint accepts a file through an HTTP POST request, checks if the
        uploaded file is of a supported type, saves it in the specified input directory,
        indexes it for retrieval, and returns a success status with relevant details.

        **File Size Limit:**
        - Configurable via `MAX_UPLOAD_SIZE` environment variable (default: 100MB)
        - Set to `None` or `0` for unlimited upload size
        - Returns HTTP 413 (Request Entity Too Large) if file exceeds limit

        **Duplicate Detection Behavior:**

        This endpoint handles two types of duplicate scenarios differently:

        1. **Filename Duplicate (Synchronous Detection)**:
           - Detected immediately before file processing
           - Usually returns `status="duplicated"` with the existing document's track_id
           - Exception: if the filename matches an existing PDF document that is
             already processed/failed and the multimodal pipeline is enabled, the
             upload is treated as a replacement source for that same document and
             triggers an in-place multimodal rebuild instead of creating a new doc
           - Non-PDF duplicates and in-flight PDFs still return `duplicated`

        2. **Content Duplicate (Asynchronous Detection)**:
           - Detected during background processing after content extraction
           - Returns `status="success"` with a new track_id immediately
           - The duplicate is detected later when processing the file content
           - Use `/documents/track_status/{track_id}` to check the final result:
             - Document will have `status="FAILED"`
             - `error_msg` contains "Content already exists. Original doc_id: xxx"
             - `metadata.is_duplicate=true` with reference to original document
             - `metadata.original_doc_id` points to the existing document
             - `metadata.original_track_id` shows the original upload's track_id

        **Why Different Behavior?**
        - Filename check is fast (simple lookup), done synchronously
        - Content extraction is expensive (PDF/DOCX parsing), done asynchronously
        - This design prevents blocking the client during expensive operations

        Args:
            background_tasks: FastAPI BackgroundTasks for async processing
            file (UploadFile): The file to be uploaded. It must have an allowed extension.

        Returns:
            InsertResponse: A response object containing the upload status and a message.
                - status="success": File accepted and queued for processing
                - status="duplicated": Filename already exists (see track_id for existing document)
                - status="success": File accepted for normal indexing OR same-doc multimodal rebuild

        Raises:
            HTTPException: If the file type is not supported (400), file too large (413), or other errors occur (500).
        """
        rag = _resolve_active_rag(active_rag)
        doc_manager = _resolve_active_doc_manager(active_doc_manager)
        try:
            # Sanitize filename to prevent Path Traversal attacks
            safe_filename = sanitize_filename(file.filename, doc_manager.input_dir)
            file_ext = Path(safe_filename).suffix.lower()
            claimed_upload_file = False

            if not doc_manager.is_supported_file(safe_filename):
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported file type. Supported types: {doc_manager.supported_extensions}",
                )

            if _is_direct_image_extension(file_ext) and (
                rag.image_embedding_func is None
                or rag.images_vdb is None
                or rag.image_blob_store is None
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Image uploads require the multimodal pipeline to be "
                        "enabled on the server."
                    ),
                )

            # Check file size limit (if configured)
            if (
                global_args.max_upload_size is not None
                and global_args.max_upload_size > 0
            ):
                # Safe access to file size (not available in older Starlette versions)
                file_size = getattr(file, "size", None)

                # Pre-flight size check (only if size is available)
                if file_size is not None:
                    if file_size > global_args.max_upload_size:
                        raise HTTPException(
                            status_code=413,
                            detail=f"File too large. Maximum size: {global_args.max_upload_size / 1024 / 1024:.1f}MB, uploaded: {file_size / 1024 / 1024:.1f}MB",
                        )
                else:
                    # If size not available, we'll check during streaming
                    logger.debug(
                        f"File size not available in UploadFile for {safe_filename}, will check during streaming"
                    )

            # Check if filename already exists in doc_status storage
            existing_doc_data = await rag.doc_status.get_doc_by_file_path(safe_filename)
            existing_doc_id: str | None = None
            should_take_over_existing_pdf = False
            if existing_doc_data:
                existing_status = _coerce_doc_status_value(
                    existing_doc_data.get("status")
                )
                if (
                    file_ext == ".pdf"
                    and existing_status in MULTIMODAL_UPLOAD_TAKEOVER_STATUSES
                    and _is_multimodal_pipeline_enabled(rag)
                ):
                    existing_doc_id, existing_doc_data = (
                        await _find_tracked_document_by_file_path(rag, safe_filename)
                    )
                    should_take_over_existing_pdf = existing_doc_id is not None

                if should_take_over_existing_pdf:
                    await _ensure_pipeline_not_busy(rag)
                else:
                    # Get document status and track_id from existing document
                    status = existing_doc_data.get("status", "unknown")
                    # Use `or ""` to handle both missing key and None value (e.g., legacy rows without track_id)
                    existing_track_id = existing_doc_data.get("track_id") or ""
                    return InsertResponse(
                        status="duplicated",
                        message=f"File '{safe_filename}' already exists in document storage (Status: {status}).",
                        track_id=existing_track_id,
                    )

            file_path = doc_manager.input_dir / safe_filename
            temp_file_path = doc_manager.input_dir / (
                f"{temp_prefix}{uuid4().hex}_{safe_filename}"
            )
            # Check if file already exists in file system
            if file_path.exists() and not should_take_over_existing_pdf:
                return InsertResponse(
                    status="duplicated",
                    message=f"File '{safe_filename}' already exists in the input directory.",
                    track_id="",
                )

            # Async streaming write with size check
            bytes_written = 0
            chunk_size = 1024 * 1024  # 1MB chunks
            needs_cleanup = False

            async with aiofiles.open(temp_file_path, "wb") as out_file:
                while True:
                    # Read chunk from upload stream
                    chunk = await file.read(chunk_size)
                    if not chunk:
                        break

                    # Check size limit during streaming (if not checked before)
                    if (
                        global_args.max_upload_size is not None
                        and global_args.max_upload_size > 0
                    ):
                        bytes_written += len(chunk)
                        if bytes_written > global_args.max_upload_size:
                            needs_cleanup = True
                            break

                    # Write chunk to file
                    await out_file.write(chunk)

            # Cleanup after file is closed
            if needs_cleanup:
                try:
                    temp_file_path.unlink()
                except Exception as cleanup_error:
                    logger.error(
                        f"Error cleaning up oversized file {safe_filename}: {cleanup_error}"
                    )

                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Maximum size: {global_args.max_upload_size / 1024 / 1024:.1f}MB, uploaded: {bytes_written / 1024 / 1024:.1f}MB",
                )

            track_id = generate_track_id(
                "rebuild_multimodal" if should_take_over_existing_pdf else "upload"
            )

            claimed_upload_file, existing_owner = await _claim_input_file(
                rag, safe_filename, track_id
            )
            if not claimed_upload_file:
                try:
                    temp_file_path.unlink()
                except Exception as cleanup_error:
                    logger.error(
                        f"Error cleaning up concurrently claimed upload {safe_filename}: {cleanup_error}"
                    )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"File '{safe_filename}' is already being processed "
                        f"(owner={existing_owner})."
                    ),
                )

            await asyncio.to_thread(temp_file_path.replace, file_path)

            if should_take_over_existing_pdf:
                doc_manager.mark_as_indexed(file_path)
                queued_record = _build_multimodal_rebuild_status_record(
                    existing_doc_data or {},
                    track_id=track_id,
                    file_path=safe_filename,
                    stage="queued_for_rebuild",
                )
                await rag.doc_status.upsert({existing_doc_id: queued_record})
                await rag.doc_status.index_done_callback()
                background_tasks.add_task(
                    background_rebuild_document_multimodal,
                    rag,
                    doc_manager,
                    existing_doc_id,
                    track_id,
                    True,
                    safe_filename,
                )
                return InsertResponse(
                    status="success",
                    message=(
                        f"Detected an existing PDF named '{safe_filename}'. "
                        f"Accepted the replacement source file, will reuse "
                        f"document {existing_doc_id} instead of creating a duplicate, "
                        f"and started multimodal rebuild in the background. "
                        f"The document will move into the Processing list shortly."
                    ),
                    track_id=track_id,
                    doc_id=existing_doc_id,
                    operation_metadata={
                        "operation": "multimodal_takeover_rebuild",
                        "target_doc_id": existing_doc_id,
                        "file_name": safe_filename,
                        "previous_status": _coerce_doc_status_value(
                            (existing_doc_data or {}).get("status")
                        ),
                        "target_status": DocStatus.PROCESSING.value,
                        "multimodal_rebuild_stage": "queued_for_rebuild",
                    },
                )

            # Add to background tasks and get track_id
            background_tasks.add_task(
                pipeline_index_file, rag, file_path, track_id, True
            )

            return InsertResponse(
                status="success",
                message=f"File '{safe_filename}' uploaded successfully. Processing will continue in background.",
                track_id=track_id,
            )

        except HTTPException:
            # Re-raise HTTP exceptions (400, 413, etc.)
            raise
        except Exception as e:
            if "claimed_upload_file" in locals() and claimed_upload_file:
                await _release_input_file_claim(rag, safe_filename, track_id)
            if "temp_file_path" in locals() and temp_file_path.exists():
                try:
                    temp_file_path.unlink()
                except Exception:
                    pass
            logger.error(f"Error /documents/upload: {file.filename}: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.get(
        "/file",
        dependencies=[Depends(document_view_permission)],
    )
    async def download_source_file(
        raw_request: Request,
        name: str = Query(
            ...,
            description=(
                "Original filename of the source document as stored in the "
                "caller's workspace input directory. Typically the "
                "``file_path`` surfaced on retrieval chunks / references."
            ),
        ),
    ):
        """Stream the source file back to the caller.

        Two-tier resolution:

        1. **On-disk fast path** — if ``input_dir/<safe_name>`` exists,
           stream it via FastAPI's ``FileResponse`` (zero-copy sendfile).
           This is the common case: anything uploaded via
           ``POST /documents/upload`` landed on disk and is handed back
           byte-for-byte.

        2. **Reconstructed-text fallback** — if no on-disk file matches,
           try to find a tracked document whose ``file_path`` equals the
           requested name and return the stored full text of that
           document as a ``.txt`` attachment. Covers documents ingested
           programmatically via
           ``rag.ainsert(texts, file_paths=["logical-id"])`` where the
           file path is a citation label and no binary source exists.
           Appending ``.txt`` to the original name (instead of replacing
           the extension) keeps the provenance visible — the user can
           tell ``doc1.pdf.txt`` is the extracted text of the
           programmatically-imported ``doc1.pdf``, not the real PDF.

        Path-traversal hardening layers (step 1 only; step 2 never
        touches the filesystem for the download name):
          * ``DocumentManager.input_dir`` is workspace-scoped so caller
            A cannot read caller B's files.
          * ``sanitize_filename`` strips path separators / ``..`` / NUL
            / control chars and re-validates ``is_relative_to`` —
            idempotent for filenames sanitized at upload time.

        Gated on ``KB_VIEW`` — the same permission required to surface
        the citation in the first place.
        """
        # Resolve the primary DocumentManager INLINE. We deliberately
        # don't use ``Depends(resolve_route_doc_manager)`` because that
        # dep transitively depends on ``resolve_route_rag``, which
        # raises HTTP 404 when the request's (workspace, kb) pair
        # isn't linked in ``kb_registry`` (e.g. frontend sends the
        # literal ``"default"`` kb id on a workspace whose only real
        # KB is ``kb_<uuid>``). Pulling that dep out lets the fan-out
        # fallback below enumerate sibling KBs and serve the file
        # from wherever it actually lives.
        state = raw_request.app.state
        context = get_request_context(raw_request)
        workspace_id = context.workspace_id or getattr(
            state, "default_workspace_id", "default"
        )
        kb_id = context.kb_id or getattr(state, "default_kb_id", "default")

        def _doc_manager_for(kb: str) -> DocumentManager:
            # Reference the factory-param ``doc_manager`` via the outer
            # ``create_document_routes`` closure. We AVOID binding any
            # local name ``doc_manager`` anywhere in ``download_source_file``
            # — Python would then treat every ``doc_manager`` reference
            # inside this nested function as a local of the OUTER
            # function (UnboundLocalError on read). Primary / per-kb
            # managers are stored under different local names below.
            if doc_manager is not None:
                return doc_manager
            if not getattr(state, "enable_kb_isolation", False):
                default_dm = getattr(state, "default_doc_manager", None)
                if default_dm is not None:
                    return default_dm
                base_input_dir = getattr(
                    state, "doc_manager_base_input_dir", "./inputs"
                )
                return DocumentManager(
                    base_input_dir,
                    workspace=getattr(state, "default_runtime_workspace", None),
                )
            runtime_workspace = compose_runtime_workspace(state, workspace_id, kb)
            cache = getattr(state, "doc_manager_cache", None)
            if cache is None:
                cache = {}
                state.doc_manager_cache = cache
            cached = cache.get(runtime_workspace)
            if cached is None:
                base_input_dir = getattr(
                    state, "doc_manager_base_input_dir", "./inputs"
                )
                cached = DocumentManager(base_input_dir, workspace=runtime_workspace)
                cache[runtime_workspace] = cached
            return cached

        # ``sanitize_filename`` runs once against the primary KB's
        # input_dir for path-traversal hardening. The sanitized name is
        # then reused across every candidate KB's ``_resolve_document_source_file``
        # call — sibling KBs share the same sanitization contract.
        # Name the local ``primary_dm`` rather than ``doc_manager`` so
        # the nested closure's free variable stays bound to the
        # factory param (see the note in ``_doc_manager_for``).
        primary_dm = _doc_manager_for(kb_id)
        safe_name = sanitize_filename(name, primary_dm.input_dir)

        # Enumerate sibling KBs linked to the workspace and try each
        # one (disk first, then reconstructed text). The request's
        # declared (workspace, kb) pair may not be a real kb_registry
        # entry: the frontend's federated-retrieval mode
        # can surface a chunk from ``kb_04af1653`` while the download
        # request inherits ``X-KB-Id: default`` from the global store.
        # Picking up the file from whichever sibling KB actually holds
        # it stays workspace-scoped — ``collect_workspace_kb_ids``
        # returns only the caller's workspace, no cross-tenant leaks.
        reason_per_kb: list[str] = []

        candidate_kbs: list[str] = [kb_id]
        if getattr(state, "enable_kb_isolation", False):
            from lightrag.api.federation import collect_workspace_kb_ids

            for sibling in collect_workspace_kb_ids(state, workspace_id) or []:
                if sibling not in candidate_kbs:
                    candidate_kbs.append(sibling)

        rag_factory = getattr(state, "rag_factory", None)

        # 1) Disk fan-out: try every candidate KB's input_dir (incl.
        #    its __enqueued__ subdir) — original file bytes win over
        #    reconstructed text when both are available.
        for candidate_kb in candidate_kbs:
            dm = _doc_manager_for(candidate_kb)
            sibling_on_disk = _resolve_document_source_file(dm, safe_name)
            if sibling_on_disk is not None:
                logger.info(
                    "Download fan-out: serving on-disk file from kb=%s path=%s",
                    candidate_kb,
                    sibling_on_disk,
                )
                content_type = (
                    mimetypes.guess_type(str(sibling_on_disk))[0]
                    or "application/octet-stream"
                )
                return FileResponse(
                    sibling_on_disk,
                    media_type=content_type,
                    filename=safe_name,
                )
            reason_per_kb.append(
                f"kb={candidate_kb}: no on-disk file under {dm.input_dir}"
            )

        # 2) full_docs fan-out: pull the original text content out of
        #    the per-KB KV storage and serve as a ``.txt`` attachment.
        content: str | None = None
        matched_doc_id: str | None = None
        matched_kb: str | None = None
        if rag_factory is None:
            reason_per_kb.append("full_docs fan-out skipped: rag_factory missing")
        else:
            for candidate_kb in candidate_kbs:
                try:
                    candidate_rag = await rag_factory.get(workspace_id, candidate_kb)
                except Exception as exc:
                    reason_per_kb.append(
                        f"kb={candidate_kb}: rag_factory.get failed ({exc!r})"
                    )
                    continue
                content, matched_doc_id, matched_kb = (
                    await _reconstruct_text_for_file_path(
                        candidate_rag,
                        name,
                        kb_label=candidate_kb,
                        reason_sink=reason_per_kb,
                    )
                )
                if content is not None:
                    break

        if content is None:
            logger.warning(
                "Download fallback exhausted for file_path '%s'; trace: %s",
                name,
                "; ".join(reason_per_kb) or "(no sinks recorded)",
            )
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Source file '{name}' not found in this workspace "
                    "(no on-disk file; no tracked document with matching "
                    "file_path in any linked KB)."
                ),
            )

        logger.info(
            "Download fallback: serving reconstructed text doc_id=%s kb=%s "
            "file_path='%s' (%d chars)",
            matched_doc_id,
            matched_kb,
            name,
            len(str(content)),
        )

        # Use the original name plus ``.txt`` so the file-tree preserves
        # the citation label. RFC 5987 ``filename*`` carries the UTF-8
        # name cleanly (Chinese / other non-ASCII filenames are common);
        # the ASCII ``filename=`` fallback is a best-effort slug.
        download_name = f"{name}.txt"
        ascii_fallback = download_name.encode("ascii", "ignore").decode("ascii") or "source.txt"
        disposition = (
            f"attachment; filename=\"{ascii_fallback}\"; "
            f"filename*=UTF-8''{url_quote(download_name)}"
        )
        return Response(
            content=str(content).encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": disposition},
        )

    @router.post(
        "/text",
        response_model=InsertResponse,
        dependencies=[Depends(document_upload_permission)],
    )
    async def insert_text(
        request: InsertTextRequest,
        background_tasks: BackgroundTasks,
        active_rag: LightRAG = Depends(resolve_route_rag),
    ):
        """
        Insert text into the RAG system.

        This endpoint allows you to insert text data into the RAG system for later retrieval
        and use in generating responses.

        Args:
            request (InsertTextRequest): The request body containing the text to be inserted.
            background_tasks: FastAPI BackgroundTasks for async processing

        Returns:
            InsertResponse: A response object containing the status of the operation.

        Raises:
            HTTPException: If an error occurs during text processing (500).
        """
        rag = _resolve_active_rag(active_rag)
        try:
            # Check if file_source already exists in doc_status storage
            if (
                request.file_source
                and request.file_source.strip()
                and request.file_source != "unknown_source"
            ):
                existing_doc_data = await rag.doc_status.get_doc_by_file_path(
                    request.file_source
                )
                if existing_doc_data:
                    # Get document status and track_id from existing document
                    status = existing_doc_data.get("status", "unknown")
                    # Use `or ""` to handle both missing key and None value (e.g., legacy rows without track_id)
                    existing_track_id = existing_doc_data.get("track_id") or ""
                    return InsertResponse(
                        status="duplicated",
                        message=f"File source '{request.file_source}' already exists in document storage (Status: {status}).",
                        track_id=existing_track_id,
                    )

            # Check if content already exists by computing content hash (doc_id)
            sanitized_text = sanitize_text_for_encoding(request.text)
            content_doc_id = compute_mdhash_id(sanitized_text, prefix="doc-")
            existing_doc = await rag.doc_status.get_by_id(content_doc_id)
            if existing_doc:
                # Content already exists, return duplicated with existing track_id
                status = existing_doc.get("status", "unknown")
                existing_track_id = existing_doc.get("track_id") or ""
                return InsertResponse(
                    status="duplicated",
                    message=f"Identical content already exists in document storage (doc_id: {content_doc_id}, Status: {status}).",
                    track_id=existing_track_id,
                )

            # Generate track_id for text insertion
            track_id = generate_track_id("insert")

            background_tasks.add_task(
                pipeline_index_texts,
                rag,
                [request.text],
                file_sources=[request.file_source],
                track_id=track_id,
            )

            return InsertResponse(
                status="success",
                message="Text successfully received. Processing will continue in background.",
                track_id=track_id,
            )
        except Exception as e:
            logger.error(f"Error /documents/text: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.post(
        "/texts",
        response_model=InsertResponse,
        dependencies=[Depends(document_upload_permission)],
    )
    async def insert_texts(
        request: InsertTextsRequest,
        background_tasks: BackgroundTasks,
        active_rag: LightRAG = Depends(resolve_route_rag),
    ):
        """
        Insert multiple texts into the RAG system.

        This endpoint allows you to insert multiple text entries into the RAG system
        in a single request.

        Args:
            request (InsertTextsRequest): The request body containing the list of texts.
            background_tasks: FastAPI BackgroundTasks for async processing

        Returns:
            InsertResponse: A response object containing the status of the operation.

        Raises:
            HTTPException: If an error occurs during text processing (500).
        """
        rag = _resolve_active_rag(active_rag)
        try:
            # Check if any file_sources already exist in doc_status storage
            if request.file_sources:
                for file_source in request.file_sources:
                    if (
                        file_source
                        and file_source.strip()
                        and file_source != "unknown_source"
                    ):
                        existing_doc_data = await rag.doc_status.get_doc_by_file_path(
                            file_source
                        )
                        if existing_doc_data:
                            # Get document status and track_id from existing document
                            status = existing_doc_data.get("status", "unknown")
                            # Use `or ""` to handle both missing key and None value (e.g., legacy rows without track_id)
                            existing_track_id = existing_doc_data.get("track_id") or ""
                            return InsertResponse(
                                status="duplicated",
                                message=f"File source '{file_source}' already exists in document storage (Status: {status}).",
                                track_id=existing_track_id,
                            )

            # Check if any content already exists by computing content hash (doc_id)
            for text in request.texts:
                sanitized_text = sanitize_text_for_encoding(text)
                content_doc_id = compute_mdhash_id(sanitized_text, prefix="doc-")
                existing_doc = await rag.doc_status.get_by_id(content_doc_id)
                if existing_doc:
                    # Content already exists, return duplicated with existing track_id
                    status = existing_doc.get("status", "unknown")
                    existing_track_id = existing_doc.get("track_id") or ""
                    return InsertResponse(
                        status="duplicated",
                        message=f"Identical content already exists in document storage (doc_id: {content_doc_id}, Status: {status}).",
                        track_id=existing_track_id,
                    )

            # Generate track_id for texts insertion
            track_id = generate_track_id("insert")

            background_tasks.add_task(
                pipeline_index_texts,
                rag,
                request.texts,
                file_sources=request.file_sources,
                track_id=track_id,
            )

            return InsertResponse(
                status="success",
                message="Texts successfully received. Processing will continue in background.",
                track_id=track_id,
            )
        except Exception as e:
            logger.error(f"Error /documents/texts: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete(
        "",
        response_model=ClearDocumentsResponse,
        dependencies=[Depends(document_delete_permission)],
    )
    async def clear_documents(
        active_rag: LightRAG = Depends(resolve_route_rag),
        active_doc_manager: DocumentManager = Depends(resolve_route_doc_manager),
    ):
        """
        Clear all documents from the RAG system.

        This endpoint deletes all documents, entities, relationships, and files from the system.
        It uses the storage drop methods to properly clean up all data and removes all files
        from the input directory.

        Returns:
            ClearDocumentsResponse: A response object containing the status and message.
                - status="success":           All documents and files were successfully cleared.
                - status="partial_success":   Document clear job exit with some errors.
                - status="busy":              Operation could not be completed because the pipeline is busy.
                - status="fail":              All storage drop operations failed, with message
                - message: Detailed information about the operation results, including counts
                  of deleted files and any errors encountered.

        Raises:
            HTTPException: Raised when a serious error occurs during the clearing process,
                          with status code 500 and error details in the detail field.
        """
        rag = _resolve_active_rag(active_rag)
        doc_manager = _resolve_active_doc_manager(active_doc_manager)
        from lightrag.kg.shared_storage import (
            get_namespace_data,
            get_namespace_lock,
        )

        # Get pipeline status and lock
        pipeline_status = await get_namespace_data(
            "pipeline_status", workspace=rag.workspace
        )
        pipeline_status_lock = get_namespace_lock(
            "pipeline_status", workspace=rag.workspace
        )

        # Check and set status with lock
        async with pipeline_status_lock:
            if pipeline_status.get("busy", False):
                return ClearDocumentsResponse(
                    status="busy",
                    message="Cannot clear documents while pipeline is busy",
                )
            # Set busy to true
            pipeline_status.update(
                {
                    "busy": True,
                    "job_name": "Clearing Documents",
                    "job_start": datetime.now().isoformat(),
                    "docs": 0,
                    "batchs": 0,
                    "cur_batch": 0,
                    "total_chunks": 0,
                    "processed_chunks": 0,
                    "current_stage": "clear_documents",
                    "current_stage_label": "Clearing documents and storage",
                    "stage_unit": "",
                    "stage_total": 0,
                    "stage_processed": 0,
                    "stage_remaining": 0,
                    "stage_elapsed_seconds": 0,
                    "stage_eta_seconds": None,
                    "request_pending": False,  # Clear any previous request
                    "latest_message": "Starting document clearing process",
                }
            )
            # Cleaning history_messages without breaking it as a shared list object
            del pipeline_status["history_messages"][:]
            pipeline_status["history_messages"].append(
                "Starting document clearing process"
            )

        try:
            # Use drop method to clear all data
            drop_tasks = []
            storages = [
                rag.text_chunks,
                rag.full_docs,
                rag.full_entities,
                rag.full_relations,
                rag.entity_chunks,
                rag.relation_chunks,
                rag.entities_vdb,
                rag.relationships_vdb,
                rag.chunks_vdb,
                rag.chunk_entity_relation_graph,
                rag.doc_status,
            ]

            # Log storage drop start
            if "history_messages" in pipeline_status:
                pipeline_status["history_messages"].append(
                    "Starting to drop storage components"
                )

            for storage in storages:
                if storage is not None:
                    drop_tasks.append(storage.drop())

            # Wait for all drop tasks to complete
            drop_results = await asyncio.gather(*drop_tasks, return_exceptions=True)

            # Check for errors and log results
            errors = []
            storage_success_count = 0
            storage_error_count = 0

            for i, result in enumerate(drop_results):
                storage_name = storages[i].__class__.__name__
                if isinstance(result, Exception):
                    error_msg = f"Error dropping {storage_name}: {str(result)}"
                    errors.append(error_msg)
                    logger.error(error_msg)
                    storage_error_count += 1
                else:
                    namespace = storages[i].namespace
                    workspace = storages[i].workspace
                    logger.info(
                        f"Successfully dropped {storage_name}: {workspace}/{namespace}"
                    )
                    storage_success_count += 1

            # Log storage drop results
            if "history_messages" in pipeline_status:
                if storage_error_count > 0:
                    pipeline_status["history_messages"].append(
                        f"Dropped {storage_success_count} storage components with {storage_error_count} errors"
                    )
                else:
                    pipeline_status["history_messages"].append(
                        f"Successfully dropped all {storage_success_count} storage components"
                    )

            # If all storage operations failed, return error status and don't proceed with file deletion
            if storage_success_count == 0 and storage_error_count > 0:
                error_message = "All storage drop operations failed. Aborting document clearing process."
                logger.error(error_message)
                if "history_messages" in pipeline_status:
                    pipeline_status["history_messages"].append(error_message)
                return ClearDocumentsResponse(status="fail", message=error_message)

            # Log file deletion start
            if "history_messages" in pipeline_status:
                pipeline_status["history_messages"].append(
                    "Starting to delete files in input directory"
                )

            # Delete only files in the current directory, preserve files in subdirectories
            deleted_files_count = 0
            file_errors_count = 0

            for file_path in doc_manager.input_dir.glob("*"):
                if file_path.is_file():
                    try:
                        file_path.unlink()
                        deleted_files_count += 1
                    except Exception as e:
                        logger.error(f"Error deleting file {file_path}: {str(e)}")
                        file_errors_count += 1

            # Log file deletion results
            if "history_messages" in pipeline_status:
                if file_errors_count > 0:
                    pipeline_status["history_messages"].append(
                        f"Deleted {deleted_files_count} files with {file_errors_count} errors"
                    )
                    errors.append(f"Failed to delete {file_errors_count} files")
                else:
                    pipeline_status["history_messages"].append(
                        f"Successfully deleted {deleted_files_count} files"
                    )

            # Prepare final result message
            final_message = ""
            if errors:
                final_message = f"Cleared documents with some errors. Deleted {deleted_files_count} files."
                status = "partial_success"
            else:
                final_message = f"All documents cleared successfully. Deleted {deleted_files_count} files."
                status = "success"

            # Log final result
            if "history_messages" in pipeline_status:
                pipeline_status["history_messages"].append(final_message)

            # Return response based on results
            return ClearDocumentsResponse(status=status, message=final_message)
        except Exception as e:
            error_msg = f"Error clearing documents: {str(e)}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            if "history_messages" in pipeline_status:
                pipeline_status["history_messages"].append(error_msg)
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            # Reset busy status after completion
            async with pipeline_status_lock:
                pipeline_status["busy"] = False
                completion_msg = "Document clearing process completed"
                pipeline_status["latest_message"] = completion_msg
                if "history_messages" in pipeline_status:
                    pipeline_status["history_messages"].append(completion_msg)

    @router.get(
        "/pipeline_status",
        dependencies=[Depends(document_view_permission)],
        response_model=PipelineStatusResponse,
    )
    async def get_pipeline_status(
        active_rag: LightRAG = Depends(resolve_route_rag),
    ) -> PipelineStatusResponse:
        """
        Get the current status of the document indexing pipeline.

        This endpoint returns information about the current state of the document processing pipeline,
        including the processing status, progress information, and history messages.

        Returns:
            PipelineStatusResponse: A response object containing:
                - autoscanned (bool): Whether auto-scan has started
                - busy (bool): Whether the pipeline is currently busy
                - job_name (str): Current job name (e.g., indexing files/indexing texts)
                - job_start (str, optional): Job start time as ISO format string
                - docs (int): Total number of documents to be indexed
                - batchs (int): Number of batches for processing documents
                - cur_batch (int): Current processing batch
                - request_pending (bool): Flag for pending request for processing
                - latest_message (str): Latest message from pipeline processing
                - history_messages (List[str], optional): List of history messages (limited to latest 1000 entries,
                  with truncation message if more than 1000 messages exist)

        Raises:
            HTTPException: If an error occurs while retrieving pipeline status (500)
        """
        rag = _resolve_active_rag(active_rag)
        try:
            from lightrag.kg.shared_storage import (
                get_namespace_data,
                get_namespace_lock,
                get_all_update_flags_status,
            )

            pipeline_status = await get_namespace_data(
                "pipeline_status", workspace=rag.workspace
            )
            pipeline_status_lock = get_namespace_lock(
                "pipeline_status", workspace=rag.workspace
            )

            # Get update flags status for all namespaces
            update_status = await get_all_update_flags_status(workspace=rag.workspace)

            # Convert MutableBoolean objects to regular boolean values
            processed_update_status = {}
            for namespace, flags in update_status.items():
                processed_flags = []
                for flag in flags:
                    # Handle both multiprocess and single process cases
                    if hasattr(flag, "value"):
                        processed_flags.append(bool(flag.value))
                    else:
                        processed_flags.append(bool(flag))
                processed_update_status[namespace] = processed_flags

            async with pipeline_status_lock:
                # Convert to regular dict if it's a Manager.dict
                status_dict = dict(pipeline_status)

            # Add processed update_status to the status dictionary
            status_dict["update_status"] = processed_update_status

            # Convert history_messages to a regular list if it's a Manager.list
            # and limit to latest 1000 entries with truncation message if needed
            if "history_messages" in status_dict:
                history_list = list(status_dict["history_messages"])
                total_count = len(history_list)

                if total_count > 1000:
                    # Calculate truncated message count
                    truncated_count = total_count - 1000

                    # Take only the latest 1000 messages
                    latest_messages = history_list[-1000:]

                    # Add truncation message at the beginning
                    truncation_message = (
                        f"[Truncated history messages: {truncated_count}/{total_count}]"
                    )
                    status_dict["history_messages"] = [
                        truncation_message
                    ] + latest_messages
                else:
                    # No truncation needed, return all messages
                    status_dict["history_messages"] = history_list

            # Ensure job_start is properly formatted as a string with timezone information
            if "job_start" in status_dict and status_dict["job_start"]:
                # Use format_datetime to ensure consistent formatting
                status_dict["job_start"] = format_datetime(status_dict["job_start"])

            return PipelineStatusResponse(**status_dict)
        except Exception as e:
            logger.error(f"Error getting pipeline status: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    # TODO: Deprecated, use /documents/paginated instead
    @router.get(
        "",
        response_model=DocsStatusesResponse,
        dependencies=[Depends(document_view_permission)],
    )
    async def documents(
        active_rag: LightRAG = Depends(resolve_route_rag),
    ) -> DocsStatusesResponse:
        """
        Get the status of all documents in the system. This endpoint is deprecated; use /documents/paginated instead.
        To prevent excessive resource consumption, a maximum of 1,000 records is returned.

        This endpoint retrieves the current status of all documents, grouped by their
        processing status (PENDING, PROCESSING, PREPROCESSED, PROCESSED, FAILED). The results are
        limited to 1000 total documents with fair distribution across all statuses.

        Returns:
            DocsStatusesResponse: A response object containing a dictionary where keys are
                                DocStatus values and values are lists of DocStatusResponse
                                objects representing documents in each status category.
                                Maximum 1000 documents total will be returned.

        Raises:
            HTTPException: If an error occurs while retrieving document statuses (500).
        """
        rag = _resolve_active_rag(active_rag)
        try:
            statuses = (
                DocStatus.PENDING,
                DocStatus.PROCESSING,
                DocStatus.PREPROCESSED,
                DocStatus.PROCESSED,
                DocStatus.FAILED,
            )

            tasks = [rag.get_docs_by_status(status) for status in statuses]
            results: List[Dict[str, DocProcessingStatus]] = await asyncio.gather(*tasks)

            response = DocsStatusesResponse()
            total_documents = 0
            max_documents = 1000

            # Convert results to lists for easier processing
            status_documents = []
            for idx, result in enumerate(results):
                status = statuses[idx]
                docs_list = []
                for doc_id, doc_status in result.items():
                    docs_list.append((doc_id, doc_status))
                status_documents.append((status, docs_list))

            # Fair distribution: round-robin across statuses
            status_indices = [0] * len(
                status_documents
            )  # Track current index for each status
            current_status_idx = 0

            while total_documents < max_documents:
                # Check if we have any documents left to process
                has_remaining = False
                for status_idx, (status, docs_list) in enumerate(status_documents):
                    if status_indices[status_idx] < len(docs_list):
                        has_remaining = True
                        break

                if not has_remaining:
                    break

                # Try to get a document from the current status
                status, docs_list = status_documents[current_status_idx]
                current_index = status_indices[current_status_idx]

                if current_index < len(docs_list):
                    doc_id, doc_status = docs_list[current_index]

                    if status not in response.statuses:
                        response.statuses[status] = []

                    response.statuses[status].append(
                        DocStatusResponse(
                            id=doc_id,
                            content_summary=doc_status.content_summary,
                            content_length=doc_status.content_length,
                            status=doc_status.status,
                            created_at=format_datetime(doc_status.created_at),
                            updated_at=format_datetime(doc_status.updated_at),
                            track_id=doc_status.track_id,
                            chunks_count=doc_status.chunks_count,
                            error_msg=doc_status.error_msg,
                            metadata=doc_status.metadata,
                            file_path=normalize_file_path(doc_status.file_path),
                        )
                    )

                    status_indices[current_status_idx] += 1
                    total_documents += 1

                # Move to next status (round-robin)
                current_status_idx = (current_status_idx + 1) % len(status_documents)

            return response
        except Exception as e:
            logger.error(f"Error GET /documents: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    class DeleteDocByIdResponse(BaseModel):
        """Response model for single document deletion operation."""

        status: Literal["deletion_started", "busy", "not_allowed"] = Field(
            description="Status of the deletion operation"
        )
        message: str = Field(description="Message describing the operation result")
        doc_id: str = Field(description="The ID of the document to delete")

    @router.delete(
        "/delete_document",
        response_model=DeleteDocByIdResponse,
        dependencies=[Depends(document_delete_permission)],
        summary="Delete a document and all its associated data by its ID.",
    )
    async def delete_document(
        delete_request: DeleteDocRequest,
        background_tasks: BackgroundTasks,
        request: Request,
        active_rag: LightRAG = Depends(resolve_route_rag),
        active_doc_manager: DocumentManager = Depends(resolve_route_doc_manager),
    ) -> DeleteDocByIdResponse:
        """
        Delete documents and all their associated data by their IDs using background processing.

        Deletes specific documents and all their associated data, including their status,
        text chunks, vector embeddings, and any related graph data. When requested,
        cached LLM extraction responses are removed after graph deletion/rebuild completes.
        The deletion process runs in the background to avoid blocking the client connection.

        This operation is irreversible and will interact with the pipeline status.

        Args:
            delete_request (DeleteDocRequest): The request containing the document IDs and deletion options.
            background_tasks: FastAPI BackgroundTasks for async processing

        Returns:
            DeleteDocByIdResponse: The result of the deletion operation.
                - status="deletion_started": The document deletion has been initiated in the background.
                - status="busy": The pipeline is busy with another operation.

        Raises:
            HTTPException:
              - 500: If an unexpected internal error occurs during initialization.
        """
        rag = _resolve_active_rag(active_rag)
        doc_manager = _resolve_active_doc_manager(active_doc_manager)
        doc_ids = delete_request.doc_ids

        try:
            from lightrag.kg.shared_storage import (
                get_namespace_data,
                get_namespace_lock,
            )

            pipeline_status = await get_namespace_data(
                "pipeline_status", workspace=rag.workspace
            )
            pipeline_status_lock = get_namespace_lock(
                "pipeline_status", workspace=rag.workspace
            )

            # Check if pipeline is busy with proper lock
            async with pipeline_status_lock:
                if pipeline_status.get("busy", False):
                    return DeleteDocByIdResponse(
                        status="busy",
                        message="Cannot delete documents while pipeline is busy",
                        doc_id=", ".join(doc_ids),
                    )

            # Add deletion task to background tasks
            background_tasks.add_task(
                background_delete_documents,
                rag,
                doc_manager,
                doc_ids,
                delete_request.delete_file,
                delete_request.delete_llm_cache,
            )

            await emit_audit_event(
                request,
                action="doc:delete",
                resource_type="document",
                # Join so a single log entry lists every doc touched — we
                # intentionally avoid one-row-per-doc so the audit table
                # does not drown on large bulk deletes.
                resource_id=", ".join(doc_ids)[:256],
                outcome="success",
                status_code=200,
                metadata={
                    "doc_count": len(doc_ids),
                    "delete_file": bool(delete_request.delete_file),
                    "delete_llm_cache": bool(delete_request.delete_llm_cache),
                },
            )

            return DeleteDocByIdResponse(
                status="deletion_started",
                message=f"Document deletion for {len(doc_ids)} documents has been initiated. Processing will continue in background.",
                doc_id=", ".join(doc_ids),
            )

        except Exception as e:
            error_msg = f"Error initiating document deletion for {delete_request.doc_ids}: {str(e)}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=error_msg)

    class CancelDocResponse(BaseModel):
        """Response for ``POST /documents/{doc_id}/cancel``."""

        status: Literal[
            "cancelled",
            "cancel_requested",
            "already_final",
            "not_found",
        ] = Field(description="Outcome of the cancellation request.")
        message: str = Field(description="Human-readable explanation.")
        doc_id: str = Field(description="Document id that was targeted.")
        previous_status: str | None = Field(
            default=None,
            description=(
                "Status the doc held before this call. Useful for auditing "
                "which path the server chose (direct flip vs. deferred)."
            ),
        )

    @router.post(
        "/{doc_id}/cancel",
        response_model=CancelDocResponse,
        dependencies=[Depends(document_delete_permission)],
        summary="Cancel a single document's processing without stopping the pipeline.",
    )
    async def cancel_document(
        doc_id: str,
        request: Request,
        active_rag: LightRAG = Depends(resolve_route_rag),
    ) -> "CancelDocResponse":  # noqa: F821 — forward ref in same function
        """
        Per-document cancel endpoint.

        The server picks one of three outcomes based on the current
        ``doc_status``:

        - ``cancelled`` (synchronous flip): the doc is in ``pending`` or
          ``preprocessed``, i.e. not yet picked up by the pipeline worker.
          We directly mark it FAILED with
          ``error_msg="cancelled by user"`` and return.

        - ``cancel_requested`` (deferred): the doc is ``processing``. We
          write ``metadata.cancel_requested = True`` on its doc_status.
          The pipeline checks this flag at natural checkpoints (after
          chunking, before entity extraction, before merge) and raises
          ``DocumentCancelledException`` on the next check, which flips
          the doc to FAILED without touching any sibling doc task.

        - ``already_final`` (409): the doc is already ``processed`` or
          ``failed``. Nothing to cancel.

        - ``not_found`` (404): no such doc in this workspace's doc_status.
        """
        rag = _resolve_active_rag(active_rag)

        record = None
        try:
            record = await rag.doc_status.get_by_id(doc_id)
        except Exception as exc:
            logger.error(
                "Failed to read doc_status for cancel request %s: %s", doc_id, exc
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to inspect document {doc_id}: {exc}",
            )

        if not record:
            raise HTTPException(
                status_code=404,
                detail=f"Document '{doc_id}' was not found in this workspace.",
            )

        previous_status = str(record.get("status") or "").lower() or None

        # Normalize expected final statuses.
        final_statuses = {
            DocStatus.PROCESSED.value,
            DocStatus.FAILED.value,
        }
        if previous_status in final_statuses:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Document '{doc_id}' is already in a final state "
                    f"('{previous_status}'); nothing to cancel."
                ),
            )

        now = datetime.now(timezone.utc).isoformat()
        existing_metadata = dict(record.get("metadata") or {})
        shared_kwargs = {
            "content_summary": record.get("content_summary", ""),
            "content_length": record.get("content_length", 0),
            "created_at": record.get("created_at", now),
            "updated_at": now,
            "file_path": record.get("file_path", ""),
            "track_id": record.get("track_id"),
            "chunks_count": record.get("chunks_count"),
            "chunks_list": record.get("chunks_list") or [],
        }

        processing_status = DocStatus.PROCESSING.value
        # PROCESSING case: set the flag; pipeline will flip status to FAILED
        # at its next checkpoint.
        if previous_status == processing_status:
            existing_metadata["cancel_requested"] = True
            existing_metadata["cancel_requested_at"] = now
            try:
                await rag.doc_status.upsert(
                    {
                        doc_id: {
                            **shared_kwargs,
                            "status": DocStatus.PROCESSING,
                            "metadata": existing_metadata,
                        }
                    }
                )
            except Exception as exc:
                logger.error(
                    "Failed to mark doc %s for deferred cancellation: %s",
                    doc_id,
                    exc,
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to request cancellation for document {doc_id}: {exc}",
                )
            await emit_audit_event(
                request,
                action="doc:cancel",
                resource_type="document",
                resource_id=doc_id,
                outcome="success",
                status_code=200,
                metadata={
                    "mode": "cancel_requested",
                    "previous_status": previous_status,
                },
            )
            return CancelDocResponse(
                status="cancel_requested",
                message=(
                    f"Cancellation requested for document '{doc_id}'. "
                    "The pipeline will finalise the document as failed at the next "
                    "processing checkpoint."
                ),
                doc_id=doc_id,
                previous_status=previous_status,
            )

        # PENDING / PREPROCESSED / etc: no worker is racing with us, so we
        # can flip the status directly. Include metadata.cancel_requested
        # for observability (audit log / UI shows "cancelled by user").
        existing_metadata.setdefault("cancel_requested", True)
        existing_metadata["cancel_requested_at"] = now
        try:
            await rag.doc_status.upsert(
                {
                    doc_id: {
                        **shared_kwargs,
                        "status": DocStatus.FAILED,
                        "error_msg": "cancelled by user",
                        "metadata": existing_metadata,
                    }
                }
            )
        except Exception as exc:
            logger.error(
                "Failed to cancel pending document %s: %s", doc_id, exc
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to cancel document {doc_id}: {exc}",
            )

        await emit_audit_event(
            request,
            action="doc:cancel",
            resource_type="document",
            resource_id=doc_id,
            outcome="success",
            status_code=200,
            metadata={
                "mode": "cancelled",
                "previous_status": previous_status,
            },
        )
        return CancelDocResponse(
            status="cancelled",
            message=f"Document '{doc_id}' cancelled before processing started.",
            doc_id=doc_id,
            previous_status=previous_status,
        )

    # ------------------------------------------------------------------
    # Move a single document between knowledge bases within the same
    # workspace. Implemented as "read content from source → enqueue in
    # target → delete from source (background)" because KB storage
    # namespaces (vector db, graph, entity/relation stores) are
    # per-KB — physically moving the extracted artefacts would require
    # re-indexing anyway, so we let the target KB re-run the pipeline.
    # ------------------------------------------------------------------

    class MoveDocRequest(BaseModel):
        target_kb_id: str = Field(
            min_length=1,
            description="KB id within the same workspace to move the document to.",
        )

    class MoveDocResponse(BaseModel):
        status: Literal["moved"] = Field(
            description=(
                "``moved``: document content was re-enqueued under the target KB "
                "and a delete-from-source background task was scheduled."
            )
        )
        message: str
        doc_id: str
        source_kb_id: str
        target_kb_id: str

    class CopyDocRequest(BaseModel):
        target_kb_id: str = Field(
            min_length=1,
            description="KB id within the same workspace to copy the document into.",
        )

    class CopyDocResponse(BaseModel):
        status: Literal["copied"] = Field(
            description=(
                "``copied``: document content was re-enqueued under the target KB; "
                "the source KB copy is left untouched."
            )
        )
        message: str
        doc_id: str
        source_kb_id: str
        target_kb_id: str

    @router.post(
        "/{doc_id}/move",
        response_model=MoveDocResponse,
        dependencies=[Depends(document_delete_permission)],
        summary="Move a document to another KB inside the same workspace.",
    )
    async def move_document(
        doc_id: str,
        payload: MoveDocRequest,
        request: Request,
        background_tasks: BackgroundTasks,
        active_rag: LightRAG = Depends(resolve_route_rag),
        active_doc_manager: DocumentManager = Depends(resolve_route_doc_manager),
    ) -> "MoveDocResponse":  # noqa: F821 — forward ref in same function
        rag_factory = getattr(request.app.state, "rag_factory", None)
        if rag_factory is None:
            raise HTTPException(
                status_code=501,
                detail=(
                    "Moving documents between KBs requires ENABLE_KB_ISOLATION=true "
                    "and a per-KB rag factory."
                ),
            )

        context = get_request_context(request)
        source_workspace = context.workspace_id or getattr(
            request.app.state, "default_workspace_id", "default"
        )
        source_kb = context.kb_id or getattr(
            request.app.state, "default_kb_id", "default"
        )
        target_kb = payload.target_kb_id.strip()

        if not target_kb:
            raise HTTPException(
                status_code=400, detail="target_kb_id must not be empty"
            )
        if target_kb == source_kb:
            raise HTTPException(
                status_code=400,
                detail="Source and target knowledge bases are the same.",
            )

        # Verify target KB is linked to the source workspace (the same
        # workspace boundary the request context is scoped to). A move
        # that would cross workspaces should go through the link/
        # unlink endpoints first rather than happening implicitly.
        registry = getattr(request.app.state, "kb_registry", None)
        if registry is not None and registry.get_kb(source_workspace, target_kb) is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Target knowledge base '{target_kb}' is not linked to "
                    f"workspace '{source_workspace}'. Link it first via "
                    f"POST /workspaces/{source_workspace}/kb/link."
                ),
            )

        source_rag = _resolve_active_rag(active_rag)
        source_doc_manager = _resolve_active_doc_manager(active_doc_manager)

        # Pull the source content + original file_path so the target
        # KB can preserve citation metadata after re-ingestion.
        source_content_record = await source_rag.full_docs.get_by_id(doc_id)
        if source_content_record is None:
            raise HTTPException(
                status_code=404,
                detail=f"Document '{doc_id}' not found in workspace "
                f"'{source_workspace}' / KB '{source_kb}'.",
            )
        content = source_content_record.get("content") or ""
        if not content:
            raise HTTPException(
                status_code=500,
                detail=f"Document '{doc_id}' has empty content — cannot move.",
            )

        source_status_record = await source_rag.doc_status.get_by_id(doc_id)
        file_path = (
            (source_status_record or {}).get("file_path")
            or source_content_record.get("file_path")
            or ""
        )

        # Pipeline cooperation: don't move a document mid-processing,
        # otherwise the in-flight entity-extraction task for this
        # doc_id will race with the target-side enqueue.
        try:
            from lightrag.kg.shared_storage import (
                get_namespace_data,
                get_namespace_lock,
            )

            pipeline_status = await get_namespace_data(
                "pipeline_status", workspace=source_rag.workspace
            )
            pipeline_status_lock = get_namespace_lock(
                "pipeline_status", workspace=source_rag.workspace
            )
            async with pipeline_status_lock:
                if pipeline_status.get("busy", False):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Cannot move documents while the source-KB pipeline "
                            "is busy. Wait for current processing to finish, "
                            "then retry."
                        ),
                    )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(
                "move_document: failed to check source pipeline state: %s", exc
            )

        # Enqueue in target KB. ``ainsert`` derives an MD5-based doc_id
        # from the content, so the same content yields the same id in
        # both source and target — we intentionally forward the
        # original id so downstream logs / citations line up.
        target_rag = await rag_factory.get(source_workspace, target_kb)
        try:
            await target_rag.ainsert(
                content,
                ids=[doc_id],
                file_paths=[file_path] if file_path else None,
            )
        except Exception as exc:
            logger.error(
                "move_document: insert into target KB %s failed: %s", target_kb, exc
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to enqueue document in target KB '{target_kb}': {exc}",
            )

        # Delete from source in the background so the HTTP response
        # returns quickly. The target-side processing also runs async,
        # driven by the pipeline's own queue.
        background_tasks.add_task(
            background_delete_documents,
            source_rag,
            source_doc_manager,
            [doc_id],
            False,  # delete_file — keep the on-disk source; move is a
                    # logical op, not a file wipe.
            False,  # delete_llm_cache
        )

        await emit_audit_event(
            request,
            action="doc:move",
            resource_type="document",
            resource_id=doc_id,
            outcome="success",
            status_code=200,
            metadata={
                "source_workspace_id": source_workspace,
                "source_kb_id": source_kb,
                "target_kb_id": target_kb,
            },
        )

        return MoveDocResponse(
            status="moved",
            message=(
                f"Document '{doc_id}' enqueued in KB '{target_kb}'. "
                f"Delete from source KB '{source_kb}' scheduled in the background."
            ),
            doc_id=doc_id,
            source_kb_id=source_kb,
            target_kb_id=target_kb,
        )

    @router.post(
        "/{doc_id}/copy",
        response_model=CopyDocResponse,
        dependencies=[Depends(document_upload_permission)],
        summary="Copy a document into another KB inside the same workspace.",
    )
    async def copy_document(
        doc_id: str,
        payload: CopyDocRequest,
        request: Request,
        active_rag: LightRAG = Depends(resolve_route_rag),
    ) -> "CopyDocResponse":  # noqa: F821 — forward ref in same function
        """Copy = same as move, but the SOURCE copy is kept.

        Reads the source document's content + file_path and re-enqueues it in
        the target KB (which re-runs extraction, since KB storage namespaces
        are per-KB). Gated on KB_UPLOAD_DOCUMENT (it adds to the target and
        removes nothing) rather than KB_DELETE_DOCUMENT.
        """
        rag_factory = getattr(request.app.state, "rag_factory", None)
        if rag_factory is None:
            raise HTTPException(
                status_code=501,
                detail=(
                    "Copying documents between KBs requires ENABLE_KB_ISOLATION=true "
                    "and a per-KB rag factory."
                ),
            )

        context = get_request_context(request)
        source_workspace = context.workspace_id or getattr(
            request.app.state, "default_workspace_id", "default"
        )
        source_kb = context.kb_id or getattr(
            request.app.state, "default_kb_id", "default"
        )
        target_kb = payload.target_kb_id.strip()

        if not target_kb:
            raise HTTPException(
                status_code=400, detail="target_kb_id must not be empty"
            )
        if target_kb == source_kb:
            raise HTTPException(
                status_code=400,
                detail="Source and target knowledge bases are the same.",
            )

        registry = getattr(request.app.state, "kb_registry", None)
        if registry is not None and registry.get_kb(source_workspace, target_kb) is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Target knowledge base '{target_kb}' is not linked to "
                    f"workspace '{source_workspace}'. Link it first via "
                    f"POST /workspaces/{source_workspace}/kb/link."
                ),
            )

        source_rag = _resolve_active_rag(active_rag)

        source_content_record = await source_rag.full_docs.get_by_id(doc_id)
        if source_content_record is None:
            raise HTTPException(
                status_code=404,
                detail=f"Document '{doc_id}' not found in workspace "
                f"'{source_workspace}' / KB '{source_kb}'.",
            )
        content = source_content_record.get("content") or ""
        if not content:
            raise HTTPException(
                status_code=500,
                detail=f"Document '{doc_id}' has empty content — cannot copy.",
            )

        source_status_record = await source_rag.doc_status.get_by_id(doc_id)
        file_path = (
            (source_status_record or {}).get("file_path")
            or source_content_record.get("file_path")
            or ""
        )

        # Enqueue in target KB (keep the source). ``ainsert`` derives an
        # MD5-based doc_id from the content; forwarding the original id keeps
        # citations/logs aligned between the source and target copies.
        target_rag = await rag_factory.get(source_workspace, target_kb)
        try:
            await target_rag.ainsert(
                content,
                ids=[doc_id],
                file_paths=[file_path] if file_path else None,
            )
        except Exception as exc:
            logger.error(
                "copy_document: insert into target KB %s failed: %s", target_kb, exc
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to enqueue document in target KB '{target_kb}': {exc}",
            )

        await emit_audit_event(
            request,
            action="doc:copy",
            resource_type="document",
            resource_id=doc_id,
            outcome="success",
            status_code=200,
            metadata={
                "source_workspace_id": source_workspace,
                "source_kb_id": source_kb,
                "target_kb_id": target_kb,
            },
        )

        return CopyDocResponse(
            status="copied",
            message=(
                f"Document '{doc_id}' copied into KB '{target_kb}'. "
                f"The source KB '{source_kb}' copy is unchanged."
            ),
            doc_id=doc_id,
            source_kb_id=source_kb,
            target_kb_id=target_kb,
        )

    @router.post(
        "/clear_cache",
        response_model=ClearCacheResponse,
        dependencies=[Depends(settings_permission)],
    )
    async def clear_cache(
        request: ClearCacheRequest,
        active_rag: LightRAG = Depends(resolve_route_rag),
    ):
        """
        Clear all cache data from the LLM response cache storage.

        This endpoint clears all cached LLM responses regardless of mode.
        The request body is accepted for API compatibility but is ignored.

        Args:
            request (ClearCacheRequest): The request body (ignored for compatibility).

        Returns:
            ClearCacheResponse: A response object containing the status and message.

        Raises:
            HTTPException: If an error occurs during cache clearing (500).
        """
        rag = _resolve_active_rag(active_rag)
        try:
            # Call the aclear_cache method (no modes parameter)
            await rag.aclear_cache()

            # Prepare success message
            message = "Successfully cleared all cache"

            return ClearCacheResponse(status="success", message=message)
        except Exception as e:
            logger.error(f"Error clearing cache: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete(
        "/delete_entity",
        response_model=DeletionResult,
        dependencies=[Depends(graph_edit_permission)],
    )
    async def delete_entity(
        request: DeleteEntityRequest,
        active_rag: LightRAG = Depends(resolve_route_rag),
    ):
        """
        Delete an entity and all its relationships from the knowledge graph.

        Args:
            request (DeleteEntityRequest): The request body containing the entity name.

        Returns:
            DeletionResult: An object containing the outcome of the deletion process.

        Raises:
            HTTPException: If the entity is not found (404) or an error occurs (500).
        """
        rag = _resolve_active_rag(active_rag)
        try:
            result = await rag.adelete_by_entity(entity_name=request.entity_name)
            if result.status == "not_found":
                raise HTTPException(status_code=404, detail=result.message)
            if result.status == "fail":
                raise HTTPException(status_code=500, detail=result.message)
            # Set doc_id to empty string since this is an entity operation, not document
            result.doc_id = ""
            return result
        except HTTPException:
            raise
        except Exception as e:
            error_msg = f"Error deleting entity '{request.entity_name}': {str(e)}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=error_msg)

    @router.delete(
        "/delete_relation",
        response_model=DeletionResult,
        dependencies=[Depends(graph_edit_permission)],
    )
    async def delete_relation(
        request: DeleteRelationRequest,
        active_rag: LightRAG = Depends(resolve_route_rag),
    ):
        """
        Delete a relationship between two entities from the knowledge graph.

        Args:
            request (DeleteRelationRequest): The request body containing the source and target entity names.

        Returns:
            DeletionResult: An object containing the outcome of the deletion process.

        Raises:
            HTTPException: If the relation is not found (404) or an error occurs (500).
        """
        rag = _resolve_active_rag(active_rag)
        try:
            result = await rag.adelete_by_relation(
                source_entity=request.source_entity,
                target_entity=request.target_entity,
            )
            if result.status == "not_found":
                raise HTTPException(status_code=404, detail=result.message)
            if result.status == "fail":
                raise HTTPException(status_code=500, detail=result.message)
            # Set doc_id to empty string since this is a relation operation, not document
            result.doc_id = ""
            return result
        except HTTPException:
            raise
        except Exception as e:
            error_msg = f"Error deleting relation from '{request.source_entity}' to '{request.target_entity}': {str(e)}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=error_msg)

    @router.get(
        "/track_status/{track_id}",
        response_model=TrackStatusResponse,
        dependencies=[Depends(document_view_permission)],
    )
    async def get_track_status(
        track_id: str,
        active_rag: LightRAG = Depends(resolve_route_rag),
    ) -> TrackStatusResponse:
        """
        Get the processing status of documents by tracking ID.

        This endpoint retrieves all documents associated with a specific tracking ID,
        allowing users to monitor the processing progress of their uploaded files or inserted texts.

        Args:
            track_id (str): The tracking ID returned from upload, text, or texts endpoints

        Returns:
            TrackStatusResponse: A response object containing:
                - track_id: The tracking ID
                - documents: List of documents associated with this track_id
                - total_count: Total number of documents for this track_id

        Raises:
            HTTPException: If track_id is invalid (400) or an error occurs (500).
        """
        rag = _resolve_active_rag(active_rag)
        try:
            # Validate track_id
            if not track_id or not track_id.strip():
                raise HTTPException(status_code=400, detail="Track ID cannot be empty")

            track_id = track_id.strip()

            # Get documents by track_id
            docs_by_track_id = await rag.aget_docs_by_track_id(track_id)

            # Convert to response format
            documents = []
            status_summary = {}

            for doc_id, doc_status in docs_by_track_id.items():
                documents.append(
                    DocStatusResponse(
                        id=doc_id,
                        content_summary=doc_status.content_summary,
                        content_length=doc_status.content_length,
                        status=doc_status.status,
                        created_at=format_datetime(doc_status.created_at),
                        updated_at=format_datetime(doc_status.updated_at),
                        track_id=doc_status.track_id,
                        chunks_count=doc_status.chunks_count,
                        error_msg=doc_status.error_msg,
                        metadata=doc_status.metadata,
                        file_path=normalize_file_path(doc_status.file_path),
                    )
                )

                # Build status summary
                # Handle both DocStatus enum and string cases for robust deserialization
                status_key = str(doc_status.status)
                status_summary[status_key] = status_summary.get(status_key, 0) + 1

            return TrackStatusResponse(
                track_id=track_id,
                documents=documents,
                total_count=len(documents),
                status_summary=status_summary,
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting track status for {track_id}: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.get(
        "/{doc_id}/images",
        dependencies=[Depends(document_view_permission)],
    )
    async def get_document_images(
        doc_id: str,
        active_rag: LightRAG = Depends(resolve_route_rag),
    ) -> dict[str, Any]:
        """List the image blob_ids associated with a document.

        Reads the reverse index that ``ainsert_image`` writes to
        ``doc_status[doc_id].metadata.image_ids``. For documents ingested
        via ``ainsert_image`` this returns a single-entry list (one image
        per virtual doc). For Phase 5 PDF documents with embedded images,
        the same list carries every extracted image's blob_id.

        Returns 404 when the doc_id is unknown, or 200 with an empty list
        when the document exists but has no associated images.

        Args:
            doc_id: The document identifier.

        Returns:
            dict with fields:
                - doc_id: echo of the requested doc_id
                - image_ids: list[str] — blob_ids (may be empty)
                - modality: "image" / "mixed" / "text" from doc_status metadata
                - source_kind: origin hint (direct_image_upload, pdf_extracted, ...)
        """
        rag = _resolve_active_rag(active_rag)
        if rag.image_metadata is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "Multimodal pipeline is not enabled on this LightRAG "
                    "instance. Configure image_embedding_func and reingest "
                    "documents to populate the image index."
                ),
            )
        try:
            doc_status = await rag.doc_status.get_by_id(doc_id)
            if doc_status is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown document: {doc_id}",
                )
            metadata = doc_status.get("metadata") or {}
            image_ids = list(metadata.get("image_ids") or [])
            return {
                "doc_id": doc_id,
                "image_ids": image_ids,
                "modality": metadata.get("modality", "text"),
                "source_kind": metadata.get("source_kind"),
                "count": len(image_ids),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                f"Error listing images for doc_id {doc_id}: {type(e).__name__}: {e}"
            )
            raise HTTPException(status_code=500, detail=str(e))

    @router.post(
        "/{doc_id}/rebuild_multimodal",
        response_model=RebuildMultimodalResponse,
        dependencies=[Depends(document_upload_permission)],
    )
    async def rebuild_document_multimodal(
        doc_id: str,
        request: RebuildMultimodalRequest,
        background_tasks: BackgroundTasks,
        active_rag: LightRAG = Depends(resolve_route_rag),
        active_doc_manager: DocumentManager = Depends(resolve_route_doc_manager),
    ) -> RebuildMultimodalResponse:
        """Rebuild multimodal assets for one previously uploaded PDF document."""
        rag = _resolve_active_rag(active_rag)
        doc_manager = _resolve_active_doc_manager(active_doc_manager)
        if (
            rag.image_embedding_func is None
            or rag.images_vdb is None
            or rag.image_blob_store is None
            or rag.image_metadata is None
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Multimodal pipeline is not enabled on this LightRAG "
                    "instance. Configure image_embedding_func first."
                ),
            )

        doc_status = await rag.doc_status.get_by_id(doc_id)
        if doc_status is None:
            raise HTTPException(status_code=404, detail=f"Unknown document: {doc_id}")

        logical_file_path = normalize_file_path(doc_status.get("file_path"))
        if not logical_file_path.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=400,
                detail="Only PDF documents support multimodal rebuild.",
            )

        source_file = _resolve_document_source_file(doc_manager, logical_file_path)
        if source_file is None:
            recovered_image_ids = await rag._get_existing_image_ids_for_doc(doc_id)
            if not recovered_image_ids:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"Cannot locate the original PDF on disk for {logical_file_path}. "
                        "Expected it in the input directory or __enqueued__, and no "
                        "previously stored multimodal image assets were found for fallback."
                    ),
                )

        await _ensure_pipeline_not_busy(rag)

        track_id = generate_track_id("rebuild_multimodal")
        queued_record = _build_multimodal_rebuild_status_record(
            doc_status,
            track_id=track_id,
            file_path=logical_file_path,
            stage="queued_for_rebuild",
        )
        await rag.doc_status.upsert({doc_id: queued_record})
        await rag.doc_status.index_done_callback()
        background_tasks.add_task(
            background_rebuild_document_multimodal,
            rag,
            doc_manager,
            doc_id,
            track_id,
            request.reuse_cache,
        )
        return RebuildMultimodalResponse(
            status="rebuild_started",
            message=(
                "Multimodal rebuild has been initiated in the background. "
                "The document will move into the Processing list, be re-extracted "
                "from the original PDF, and reuse cached image captions/embeddings "
                "whenever possible."
            ),
            track_id=track_id,
            doc_id=doc_id,
        )

    async def _get_documents_paginated_all_kbs(
        raw_request: Request,
        request: DocumentsRequest,
    ) -> PaginatedDocsResponse:
        """Aggregated path: fan out the paginated fetch across every KB in
        the current workspace, merge the rows, re-sort, and slice the
        requested page. Status counts are summed across shards.
        """
        from lightrag.api.federation import collect_workspace_kb_ids

        state = raw_request.app.state
        rag_factory = getattr(state, "rag_factory", None)
        if rag_factory is None:
            raise HTTPException(
                status_code=500,
                detail="KB isolation enabled but rag_factory is not configured",
            )

        context = get_request_context(raw_request)
        workspace_id = context.workspace_id or getattr(
            state, "default_workspace_id", "default"
        )
        kb_ids = collect_workspace_kb_ids(state, workspace_id)

        if not kb_ids:
            return PaginatedDocsResponse(
                documents=[],
                pagination=PaginationInfo(
                    page=request.page,
                    page_size=request.page_size,
                    total_count=0,
                    total_pages=0,
                    has_next=False,
                    has_prev=False,
                ),
                status_counts={},
            )

        semaphore = asyncio.Semaphore(4)

        async def _fetch_one(kb_id: str):
            async with semaphore:
                try:
                    rag = await rag_factory.get(workspace_id, kb_id)
                    docs_result, counts = await asyncio.gather(
                        rag.doc_status.get_docs_paginated(
                            status_filter=request.status_filter,
                            page=1,
                            page_size=10000,
                            sort_field=request.sort_field,
                            sort_direction=request.sort_direction,
                        ),
                        rag.doc_status.get_all_status_counts(),
                    )
                    return kb_id, docs_result, counts
                except Exception as exc:
                    logger.warning(
                        "[documents/paginated][all_kbs] shard failed workspace=%s kb=%s: %s",
                        workspace_id,
                        kb_id,
                        exc,
                    )
                    return kb_id, None, None

        shard_results = await asyncio.gather(*(_fetch_one(kb) for kb in kb_ids))

        merged_counts: Dict[str, int] = {}
        # (kb_id, doc_id, DocProcessingStatus)
        merged_rows: List[tuple] = []
        for kb_id, docs_result, counts in shard_results:
            if counts:
                for key, value in counts.items():
                    if isinstance(value, int):
                        merged_counts[key] = merged_counts.get(key, 0) + value
            if docs_result:
                docs_with_ids, _total = docs_result
                for doc_id, doc in docs_with_ids:
                    merged_rows.append((kb_id, doc_id, doc))

        sort_field = request.sort_field
        reverse_sort = request.sort_direction.lower() == "desc"

        def _sort_key(row):
            _, doc_id, doc = row
            if sort_field == "id":
                return doc_id or ""
            if sort_field == "file_path":
                return get_pinyin_sort_key(getattr(doc, "file_path", "") or "")
            return getattr(doc, sort_field, "") or ""

        merged_rows.sort(key=_sort_key, reverse=reverse_sort)

        total_count = len(merged_rows)
        page = request.page
        page_size = request.page_size
        start = (page - 1) * page_size
        end = start + page_size
        page_rows = merged_rows[start:end]

        doc_responses: List[DocStatusResponse] = []
        for kb_id, doc_id, doc in page_rows:
            doc_responses.append(
                DocStatusResponse(
                    id=doc_id,
                    content_summary=doc.content_summary,
                    content_length=doc.content_length,
                    status=doc.status,
                    created_at=format_datetime(doc.created_at),
                    updated_at=format_datetime(doc.updated_at),
                    track_id=doc.track_id,
                    chunks_count=doc.chunks_count,
                    error_msg=doc.error_msg,
                    metadata=doc.metadata,
                    file_path=normalize_file_path(doc.file_path),
                    knowledge_base_id=kb_id,
                )
            )

        total_pages = (total_count + page_size - 1) // page_size if page_size else 0
        return PaginatedDocsResponse(
            documents=doc_responses,
            pagination=PaginationInfo(
                page=page,
                page_size=page_size,
                total_count=total_count,
                total_pages=total_pages,
                has_next=page < total_pages,
                has_prev=page > 1,
            ),
            status_counts=merged_counts,
        )

    @router.post(
        "/paginated",
        response_model=PaginatedDocsResponse,
        dependencies=[Depends(document_view_permission)],
    )
    async def get_documents_paginated(
        request: DocumentsRequest,
        raw_request: Request,
    ) -> PaginatedDocsResponse:
        """
        Get documents with pagination support.

        This endpoint retrieves documents with pagination, filtering, and sorting capabilities.
        It provides better performance for large document collections by loading only the
        requested page of data.

        Args:
            request (DocumentsRequest): The request body containing pagination parameters

        Returns:
            PaginatedDocsResponse: A response object containing:
                - documents: List of documents for the current page
                - pagination: Pagination information (page, total_count, etc.)
                - status_counts: Count of documents by status for all documents

        Raises:
            HTTPException: If an error occurs while retrieving documents (500).
        """
        state = raw_request.app.state
        if request.all_kbs and getattr(state, "enable_kb_isolation", False):
            return await _get_documents_paginated_all_kbs(raw_request, request)

        rag = _resolve_active_rag(await resolve_route_rag(raw_request))
        trace_id = uuid4().hex[:8]
        request_start = time.perf_counter()
        status_filter_value = (
            request.status_filter.value if request.status_filter is not None else None
        )

        performance_timing_log(
            "[documents/paginated][%s] Request start workspace=%s status_filter=%s page=%s page_size=%s sort_field=%s sort_direction=%s",
            trace_id,
            rag.workspace,
            status_filter_value,
            request.page,
            request.page_size,
            request.sort_field,
            request.sort_direction,
        )

        try:

            async def _timed_call(operation_name: str, operation):
                operation_start = time.perf_counter()
                performance_timing_log(
                    "[documents/paginated][%s] %s started",
                    trace_id,
                    operation_name,
                )
                try:
                    result = await operation
                except Exception:
                    elapsed = time.perf_counter() - operation_start
                    performance_timing_log(
                        "[documents/paginated][%s] %s failed after %.4fs",
                        trace_id,
                        operation_name,
                        elapsed,
                    )
                    raise

                elapsed = time.perf_counter() - operation_start
                performance_timing_log(
                    "[documents/paginated][%s] %s completed in %.4fs",
                    trace_id,
                    operation_name,
                    elapsed,
                )
                return result

            query_task_create_start = time.perf_counter()
            docs_task = asyncio.create_task(
                _timed_call(
                    "get_docs_paginated",
                    rag.doc_status.get_docs_paginated(
                        status_filter=request.status_filter,
                        page=request.page,
                        page_size=request.page_size,
                        sort_field=request.sort_field,
                        sort_direction=request.sort_direction,
                    ),
                )
            )
            status_counts_task = asyncio.create_task(
                _timed_call(
                    "get_all_status_counts",
                    rag.doc_status.get_all_status_counts(),
                )
            )
            query_task_create_elapsed = time.perf_counter() - query_task_create_start
            performance_timing_log(
                "[documents/paginated][%s] Query tasks created in %.4fs",
                trace_id,
                query_task_create_elapsed,
            )

            query_await_start = time.perf_counter()
            (documents_with_ids, total_count), status_counts = await asyncio.gather(
                docs_task, status_counts_task
            )
            query_await_elapsed = time.perf_counter() - query_await_start
            performance_timing_log(
                "[documents/paginated][%s] Query tasks awaited in %.4fs",
                trace_id,
                query_await_elapsed,
            )

            # Convert documents to response format
            response_assembly_start = time.perf_counter()
            doc_responses = []
            for doc_id, doc in documents_with_ids:
                doc_responses.append(
                    DocStatusResponse(
                        id=doc_id,
                        content_summary=doc.content_summary,
                        content_length=doc.content_length,
                        status=doc.status,
                        created_at=format_datetime(doc.created_at),
                        updated_at=format_datetime(doc.updated_at),
                        track_id=doc.track_id,
                        chunks_count=doc.chunks_count,
                        error_msg=doc.error_msg,
                        metadata=doc.metadata,
                        file_path=normalize_file_path(doc.file_path),
                    )
                )

            # Calculate pagination info
            total_pages = (total_count + request.page_size - 1) // request.page_size
            has_next = request.page < total_pages
            has_prev = request.page > 1

            pagination = PaginationInfo(
                page=request.page,
                page_size=request.page_size,
                total_count=total_count,
                total_pages=total_pages,
                has_next=has_next,
                has_prev=has_prev,
            )
            response = PaginatedDocsResponse(
                documents=doc_responses,
                pagination=pagination,
                status_counts=status_counts,
            )
            response_assembly_elapsed = time.perf_counter() - response_assembly_start
            total_elapsed = time.perf_counter() - request_start

            performance_timing_log(
                "[documents/paginated][%s] Response assembled in %.4fs",
                trace_id,
                response_assembly_elapsed,
            )
            performance_timing_log(
                "[documents/paginated][%s] Request completed in %.4fs returned_rows=%s total_count=%s status_count_keys=%s",
                trace_id,
                total_elapsed,
                len(doc_responses),
                total_count,
                sorted(status_counts.keys()),
            )

            return response

        except Exception as e:
            total_elapsed = time.perf_counter() - request_start
            performance_timing_log(
                "[documents/paginated][%s] Request failed after %.4fs",
                trace_id,
                total_elapsed,
            )
            logger.error(f"Error getting paginated documents: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.get(
        "/status_counts",
        response_model=StatusCountsResponse,
        dependencies=[Depends(document_view_permission)],
    )
    async def get_document_status_counts(
        raw_request: Request,
        all_kbs: bool = False,
    ) -> StatusCountsResponse:
        """
        Get counts of documents by status.

        This endpoint retrieves the count of documents in each processing status
        (PENDING, PROCESSING, PROCESSED, FAILED) for all documents in the system.

        When ``all_kbs=true`` and KB isolation is enabled, counts are summed
        across every KB linked to the current workspace.

        Returns:
            StatusCountsResponse: A response object containing status counts

        Raises:
            HTTPException: If an error occurs while retrieving status counts (500).
        """
        state = raw_request.app.state
        try:
            if all_kbs and getattr(state, "enable_kb_isolation", False):
                from lightrag.api.federation import collect_workspace_kb_ids

                rag_factory = getattr(state, "rag_factory", None)
                if rag_factory is None:
                    raise HTTPException(
                        status_code=500,
                        detail="KB isolation enabled but rag_factory is not configured",
                    )

                context = get_request_context(raw_request)
                workspace_id = context.workspace_id or getattr(
                    state, "default_workspace_id", "default"
                )
                kb_ids = collect_workspace_kb_ids(state, workspace_id)
                if not kb_ids:
                    return StatusCountsResponse(status_counts={})

                semaphore = asyncio.Semaphore(4)

                async def _fetch_counts(kb_id: str):
                    async with semaphore:
                        try:
                            shard_rag = await rag_factory.get(workspace_id, kb_id)
                            return await shard_rag.doc_status.get_all_status_counts()
                        except Exception as exc:
                            logger.warning(
                                "[documents/status_counts][all_kbs] shard failed workspace=%s kb=%s: %s",
                                workspace_id,
                                kb_id,
                                exc,
                            )
                            return None

                shard_counts = await asyncio.gather(
                    *(_fetch_counts(kb) for kb in kb_ids)
                )
                merged: Dict[str, int] = {}
                for counts in shard_counts:
                    if not counts:
                        continue
                    for key, value in counts.items():
                        if isinstance(value, int):
                            merged[key] = merged.get(key, 0) + value
                return StatusCountsResponse(status_counts=merged)

            rag = _resolve_active_rag(await resolve_route_rag(raw_request))
            status_counts = await rag.doc_status.get_all_status_counts()
            return StatusCountsResponse(status_counts=status_counts)

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting document status counts: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.post(
        "/reprocess_failed",
        response_model=ReprocessResponse,
        dependencies=[Depends(document_upload_permission)],
    )
    async def reprocess_failed_documents(
        background_tasks: BackgroundTasks,
        active_rag: LightRAG = Depends(resolve_route_rag),
    ):
        """
        Reprocess failed and pending documents.

        This endpoint triggers the document processing pipeline which automatically
        picks up and reprocesses documents in the following statuses:
        - FAILED: Documents that failed during previous processing attempts
        - PENDING: Documents waiting to be processed
        - PROCESSING: Documents with abnormally terminated processing (e.g., server crashes)

        This is useful for recovering from server crashes, network errors, LLM service
        outages, or other temporary failures that caused document processing to fail.

        The processing happens in the background and can be monitored by checking the
        pipeline status. The reprocessed documents retain their original track_id from
        initial upload, so use their original track_id to monitor progress.

        Returns:
            ReprocessResponse: Response with status and message.
                track_id is always empty string because reprocessed documents retain
                their original track_id from initial upload.

        Raises:
            HTTPException: If an error occurs while initiating reprocessing (500).
        """
        rag = _resolve_active_rag(active_rag)
        try:
            # Start the reprocessing in the background
            # Note: Reprocessed documents retain their original track_id from initial upload
            background_tasks.add_task(rag.apipeline_process_enqueue_documents)
            logger.info("Reprocessing of failed documents initiated")

            return ReprocessResponse(
                status="reprocessing_started",
                message="Reprocessing of failed documents has been initiated in background. Documents retain their original track_id.",
            )

        except Exception as e:
            logger.error(f"Error initiating reprocessing of failed documents: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    @router.post(
        "/cancel_pipeline",
        response_model=CancelPipelineResponse,
        dependencies=[Depends(settings_permission)],
    )
    async def cancel_pipeline(
        active_rag: LightRAG = Depends(resolve_route_rag),
    ):
        """
        Request cancellation of the currently running pipeline.

        This endpoint sets a cancellation flag in the pipeline status. The pipeline will:
        1. Check this flag at key processing points
        2. Stop processing new documents
        3. Cancel all running document processing tasks
        4. Mark all PROCESSING documents as FAILED with reason "User cancelled"

        The cancellation is graceful and ensures data consistency. Documents that have
        completed processing will remain in PROCESSED status.

        Returns:
            CancelPipelineResponse: Response with status and message
                - status="cancellation_requested": Cancellation flag has been set
                - status="not_busy": Pipeline is not currently running

        Raises:
            HTTPException: If an error occurs while setting cancellation flag (500).
        """
        rag = _resolve_active_rag(active_rag)
        try:
            from lightrag.kg.shared_storage import (
                get_namespace_data,
                get_namespace_lock,
            )

            pipeline_status = await get_namespace_data(
                "pipeline_status", workspace=rag.workspace
            )
            pipeline_status_lock = get_namespace_lock(
                "pipeline_status", workspace=rag.workspace
            )

            async with pipeline_status_lock:
                if not pipeline_status.get("busy", False):
                    return CancelPipelineResponse(
                        status="not_busy",
                        message="Pipeline is not currently running. No cancellation needed.",
                    )

                # Set cancellation flag
                pipeline_status["cancellation_requested"] = True
                cancel_msg = "Pipeline cancellation requested by user"
                logger.info(cancel_msg)
                pipeline_status["latest_message"] = cancel_msg
                pipeline_status["history_messages"].append(cancel_msg)

            return CancelPipelineResponse(
                status="cancellation_requested",
                message="Pipeline cancellation has been requested. Documents will be marked as FAILED.",
            )

        except Exception as e:
            logger.error(f"Error requesting pipeline cancellation: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=str(e))

    return router
