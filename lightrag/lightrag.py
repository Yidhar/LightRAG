from __future__ import annotations

import traceback
import asyncio
import inspect
import os
import re
import time
import warnings
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from functools import partial
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    cast,
    final,
    Literal,
    Optional,
    List,
    Dict,
    Union,
)
from lightrag.prompt import PROMPTS
from lightrag.exceptions import (
    DocumentCancelledException,
    PipelineCancelledException,
)
from lightrag.constants import (
    DEFAULT_MAX_GLEANING,
    DEFAULT_FORCE_LLM_SUMMARY_ON_MERGE,
    DEFAULT_TOP_K,
    DEFAULT_CHUNK_TOP_K,
    DEFAULT_MAX_ENTITY_TOKENS,
    DEFAULT_MAX_RELATION_TOKENS,
    DEFAULT_MAX_TOTAL_TOKENS,
    DEFAULT_COSINE_THRESHOLD,
    DEFAULT_RELATED_CHUNK_NUMBER,
    DEFAULT_KG_CHUNK_PICK_METHOD,
    DEFAULT_MIN_RERANK_SCORE,
    DEFAULT_SUMMARY_MAX_TOKENS,
    DEFAULT_SUMMARY_CONTEXT_SIZE,
    DEFAULT_SUMMARY_LENGTH_RECOMMENDED,
    DEFAULT_MAX_EXTRACT_INPUT_TOKENS,
    DEFAULT_MAX_ASYNC,
    DEFAULT_MAX_PARALLEL_INSERT,
    DEFAULT_MAX_GRAPH_NODES,
    DEFAULT_MAX_SOURCE_IDS_PER_ENTITY,
    DEFAULT_MAX_SOURCE_IDS_PER_RELATION,
    DEFAULT_ENTITY_TYPES,
    DEFAULT_SUMMARY_LANGUAGE,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_EMBEDDING_TIMEOUT,
    DEFAULT_SOURCE_IDS_LIMIT_METHOD,
    DEFAULT_MAX_FILE_PATHS,
    DEFAULT_FILE_PATH_MORE_PLACEHOLDER,
)
from lightrag.utils import get_env_value

from lightrag.kg import (
    STORAGES,
    verify_storage_implementation,
)


from lightrag.kg.shared_storage import (
    get_namespace_data,
    get_data_init_lock,
    get_default_workspace,
    set_default_workspace,
    get_namespace_lock,
)

from lightrag.base import (
    BaseBlobStorage,
    BaseGraphStorage,
    BaseKVStorage,
    BaseVectorStorage,
    DocProcessingStatus,
    DocStatus,
    DocStatusStorage,
    QueryParam,
    StorageNameSpace,
    StoragesStatus,
    DeletionResult,
    OllamaServerInfos,
    QueryResult,
)
from lightrag.namespace import NameSpace
from lightrag.operate import (
    chunking_by_token_size,
    extract_entities,
    merge_nodes_and_edges,
    kg_query,
    naive_query,
    rebuild_knowledge_from_chunks,
)
from lightrag.constants import GRAPH_FIELD_SEP
from lightrag.utils import (
    Tokenizer,
    TiktokenTokenizer,
    EmbeddingFunc,
    MultimodalEmbeddingFunc,
    always_get_an_event_loop,
    compute_mdhash_id,
    lazy_external_import,
    priority_limit_async_func_call,
    get_content_summary,
    sanitize_text_for_encoding,
    check_storage_env_vars,
    generate_track_id,
    convert_to_user_format,
    logger,
    subtract_source_ids,
    make_relation_chunk_key,
    normalize_source_ids_limit_method,
)
from lightrag.types import KnowledgeGraph
from dotenv import load_dotenv

# use the .env that is inside the current folder
# allows to use different .env file for each lightrag instance
# the OS environment variables take precedence over the .env file
load_dotenv(dotenv_path=".env", override=False)


def _chunk_fields_from_status_doc(
    status_doc: "DocProcessingStatus",
) -> tuple[list[str], int]:
    """Return (chunks_list, chunks_count) preserved from a status document.

    Filters out any non-string or empty chunk IDs.  When chunks_count is
    absent or invalid, it is inferred from the length of chunks_list.
    """
    chunks_list: list[str] = []
    if isinstance(status_doc.chunks_list, list):
        chunks_list = [
            chunk_id
            for chunk_id in status_doc.chunks_list
            if isinstance(chunk_id, str) and chunk_id
        ]

    if isinstance(status_doc.chunks_count, int) and status_doc.chunks_count >= 0:
        return chunks_list, status_doc.chunks_count

    return chunks_list, len(chunks_list)


def _resolve_doc_file_path(
    status_doc: "DocProcessingStatus" | None = None,
    content_data: dict[str, Any] | None = None,
) -> str:
    """Resolve the best available document file path.

    Prefer a non-placeholder path from doc_status, then fall back to full_docs.
    This avoids overwriting historical file paths with placeholder values during
    retries or early-cancellation paths.
    """

    placeholder_paths = {"", "no-file-path", "unknown_source"}

    def _normalize_path(candidate: Any) -> str | None:
        if not isinstance(candidate, str):
            return None

        normalized = candidate.strip()
        if not normalized:
            return None

        return normalized

    candidates = [
        _normalize_path(getattr(status_doc, "file_path", None)),
        _normalize_path(content_data.get("file_path") if content_data else None),
    ]

    for candidate in candidates:
        if candidate and candidate not in placeholder_paths:
            return candidate

    for candidate in candidates:
        if candidate:
            return "unknown_source" if candidate == "no-file-path" else candidate

    return "unknown_source"


def _normalize_string_list(raw_values: Any, context: str = "") -> list[str]:
    """Return a list of non-empty strings from raw_values.

    Non-string elements are dropped and logged as warnings. If raw_values is
    not a list, an empty list is returned.
    """
    if not isinstance(raw_values, list):
        return []
    result = []
    for i, value in enumerate(raw_values):
        if isinstance(value, str) and value:
            result.append(value)
        else:
            logger.warning(
                "Non-string element dropped from list%s at index %d: %r",
                f" ({context})" if context else "",
                i,
                value,
            )
    return result


def _build_multimodal_rebuild_status_record(
    existing_status: dict[str, Any],
    *,
    track_id: str,
    file_path: str,
    stage: str,
    error_msg: str = "",
) -> dict[str, Any]:
    """Build a transient doc_status row for an in-flight multimodal rebuild."""
    now_iso = datetime.now(timezone.utc).isoformat()
    existing_meta = dict(existing_status.get("metadata", {}) or {})
    processing_start_time = existing_meta.get("processing_start_time")
    if not isinstance(processing_start_time, int):
        processing_start_time = int(time.time())

    rebuild_meta = {
        **existing_meta,
        "processing_start_time": processing_start_time,
        "multimodal_rebuild_in_progress": True,
        "multimodal_rebuild_track_id": track_id,
        "multimodal_rebuild_stage": stage,
        "multimodal_rebuild_started_at": existing_meta.get(
            "multimodal_rebuild_started_at", now_iso
        ),
    }
    if error_msg:
        rebuild_meta["multimodal_rebuild_failed"] = True
    else:
        rebuild_meta.pop("multimodal_rebuild_failed", None)

    return {
        **existing_status,
        "status": DocStatus.PROCESSING,
        "track_id": track_id,
        "file_path": file_path or existing_status.get("file_path") or "unknown_source",
        "updated_at": now_iso,
        "error_msg": error_msg,
        "metadata": rebuild_meta,
    }


def _coerce_bbox_number(value: Any) -> float | None:
    """Best-effort float coercion for image bbox fields."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _format_image_bbox_text(raw_bbox: Any) -> str | None:
    """Render a bbox dict into a compact human-readable location string."""
    if not isinstance(raw_bbox, dict):
        return None

    x0 = _coerce_bbox_number(raw_bbox.get("x0", raw_bbox.get("l")))
    y0 = _coerce_bbox_number(raw_bbox.get("y0", raw_bbox.get("t")))
    x1 = _coerce_bbox_number(raw_bbox.get("x1", raw_bbox.get("r")))
    y1 = _coerce_bbox_number(raw_bbox.get("y1", raw_bbox.get("b")))
    width = _coerce_bbox_number(raw_bbox.get("width"))
    height = _coerce_bbox_number(raw_bbox.get("height"))

    if width is None and x0 is not None and x1 is not None:
        width = max(0.0, x1 - x0)
    if height is None and y0 is not None and y1 is not None:
        height = max(0.0, y1 - y0)

    parts: list[str] = []
    if x0 is not None:
        parts.append(f"x={x0:.1f}")
    if y0 is not None:
        parts.append(f"y={y0:.1f}")
    if width is not None:
        parts.append(f"w={width:.1f}")
    if height is not None:
        parts.append(f"h={height:.1f}")

    if not parts:
        return None
    return ", ".join(parts)


def _parse_image_caption_json(raw: str | None) -> dict[str, Any] | None:
    """Robustly parse the JSON returned by ``vision_model_func``.

    Vision models sometimes wrap their output in markdown fences or emit
    leading/trailing prose despite being told not to. This helper strips
    markdown fences, extracts the first top-level JSON object it can find,
    and falls back to wrapping the raw text in a synthetic dict if parsing
    ultimately fails (so the caller always gets *something* usable for the
    annotation text stage).
    """
    if not raw:
        return None

    import json
    import re

    text = raw.strip()

    # Strip ```json ... ``` or ``` ... ``` fences if present.
    fence_match = re.match(
        r"^```(?:json|JSON)?\s*\n(.*?)\n```\s*$", text, flags=re.DOTALL
    )
    if fence_match:
        text = fence_match.group(1).strip()

    # First attempt: parse the text directly.
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    # Second attempt: locate the first balanced top-level object.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # Last resort: wrap the raw text so the downstream annotation still
    # has content to chunk and feed to entity extraction.
    logger.warning(
        f"_parse_image_caption_json: failed to parse vision response as JSON; "
        f"falling back to raw text wrapper. First 120 chars: {raw[:120]!r}"
    )
    return {
        "image_category": "Other",
        "sub_type": "",
        "caption": raw[:120].strip(),
        "detailed_description": raw.strip(),
        "detected_entities": [],
        "key_attributes": {},
    }


def _caption_json_has_meaningful_summary(caption_json: Any) -> bool:
    """Return True when cached caption JSON contains useful descriptive signal.

    Rebuild flows often reuse cached caption_json by blob hash. If the cached
    payload only contains a coarse category and no actual caption / detail /
    entities, reusing it will permanently preserve an under-described image and
    make repeated rebuilds look like a quality regression.
    """
    if not isinstance(caption_json, dict):
        return False

    def _has_text(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    if _has_text(caption_json.get("caption")):
        return True
    if _has_text(caption_json.get("detailed_description")):
        return True

    detected_entities = caption_json.get("detected_entities")
    if isinstance(detected_entities, list):
        for entity in detected_entities:
            if _has_text(entity):
                return True

    key_attributes = caption_json.get("key_attributes")
    if isinstance(key_attributes, dict) and any(
        _has_text(key) or _has_text(value)
        for key, value in key_attributes.items()
    ):
        return True

    return False


def _build_image_page_locator(extra_metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(extra_metadata, dict):
        return None

    parts: list[str] = []
    page = extra_metadata.get("source_page")
    printed_page = extra_metadata.get("source_printed_page")
    page_label = str(extra_metadata.get("source_page_label") or "").strip()
    physical_page_text = ""
    printed_page_text = ""

    if page is not None:
        try:
            physical_page_text = f"PDF物理第 {int(page)} 页"
        except (TypeError, ValueError):
            physical_page_text = f"PDF物理第 {page} 页"
        parts.append(physical_page_text)
    if printed_page is not None:
        try:
            printed_page_text = str(int(printed_page))
        except (TypeError, ValueError):
            printed_page_text = str(printed_page)
        if printed_page_text and printed_page_text != str(page):
            parts.append(f"文档页码 {printed_page_text}")

    if page_label:
        if page_label != str(page) and page_label != printed_page_text:
            parts.append(f"页标 {page_label}")
    elif page is None and physical_page_text:
        parts.append(physical_page_text)

    if page is None and not parts and page_label:
        parts.append(f"页标 {page_label}")

    if not parts:
        return None
    return " · ".join(parts)


def _ingest_bbox_signature(
    raw_bbox: Any, quantum: float = 8.0
) -> tuple[int, int, int, int] | None:
    if not isinstance(raw_bbox, dict):
        return None
    try:
        return (
            int(round(float(raw_bbox.get("x0")) / quantum)),
            int(round(float(raw_bbox.get("y0")) / quantum)),
            int(round(float(raw_bbox.get("x1")) / quantum)),
            int(round(float(raw_bbox.get("y1")) / quantum)),
        )
    except (TypeError, ValueError):
        return None


def _merge_extracted_image_for_ingest(
    preferred: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    def _prefer_longer_text(key: str) -> None:
        current = str(preferred.get(key) or "").strip()
        other = str(candidate.get(key) or "").strip()
        if other and len(other) > len(current):
            preferred[key] = candidate.get(key)

    for text_key in ("caption_hint", "page_text_excerpt", "context_text"):
        _prefer_longer_text(text_key)

    for key in (
        "bbox",
        "mime_type",
        "page_no",
        "source_printed_page",
        "source_page_label",
        "pil_size",
        "page_size",
        "native_xref",
    ):
        if preferred.get(key) is None and candidate.get(key) is not None:
            preferred[key] = candidate.get(key)

    merged_chunk_ids = list(
        dict.fromkeys(
            [
                *(
                    preferred.get("context_chunk_ids")
                    if isinstance(preferred.get("context_chunk_ids"), list)
                    else []
                ),
                *(
                    candidate.get("context_chunk_ids")
                    if isinstance(candidate.get("context_chunk_ids"), list)
                    else []
                ),
            ]
        )
    )
    if merged_chunk_ids:
        preferred["context_chunk_ids"] = merged_chunk_ids

    merged_context_chunks: list[dict[str, Any]] = []
    seen_chunk_keys: set[str] = set()
    for source in (
        preferred.get("context_chunks"),
        candidate.get("context_chunks"),
    ):
        if not isinstance(source, list):
            continue
        for chunk in source:
            if not isinstance(chunk, dict):
                continue
            chunk_key = str(chunk.get("chunk_id") or chunk)
            if chunk_key in seen_chunk_keys:
                continue
            seen_chunk_keys.add(chunk_key)
            merged_context_chunks.append(chunk)
    if merged_context_chunks:
        preferred["context_chunks"] = merged_context_chunks

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


def _dedupe_extracted_images_for_multimodal_ingest(
    extracted_images: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Conservative last-mile dedupe before captioning / embedding / chunk append."""
    if not extracted_images:
        return []

    priority_by_mode = {
        "pymupdf_native_image": 500,
        "pymupdf_image_block": 450,
        "docling_picture": 400,
        "page_raster_recall_fallback": 150,
        "page_raster_fallback": 100,
    }

    sortable_images = [
        img for img in extracted_images if isinstance(img, dict) and img.get("bytes")
    ]
    sortable_images.sort(
        key=lambda item: (
            -priority_by_mode.get(str(item.get("extraction_mode") or "").strip(), 0),
            -len(item.get("bytes") or b""),
        )
    )

    deduped: list[dict[str, Any]] = []
    seen_by_key: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    passthrough_items = [
        img
        for img in extracted_images
        if not isinstance(img, dict) or not img.get("bytes")
    ]

    for item in sortable_images:
        blob_id = compute_mdhash_id(item.get("bytes"), prefix="img-")
        page_no = item.get("page_no")
        try:
            page_key: Any = int(page_no)
        except (TypeError, ValueError):
            page_key = page_no
        dedupe_key = (
            blob_id,
            page_key,
            _ingest_bbox_signature(item.get("bbox")),
        )
        existing = seen_by_key.get(dedupe_key)
        if existing is None:
            cloned = dict(item)
            seen_by_key[dedupe_key] = cloned
            deduped.append(cloned)
        else:
            _merge_extracted_image_for_ingest(existing, item)

    return [*deduped, *passthrough_items]


def _image_annotation_to_text(
    caption_json: dict[str, Any] | None,
    *,
    blob_id: str,
    file_path: str,
    extra_metadata: dict[str, Any] | None = None,
    include_context: bool = True,
) -> str:
    """Render a caption JSON into the human-readable text block that will flow
    through LightRAG's standard text ingestion pipeline.

    The text is deliberately structured (labelled sections and bullet lines)
    so that the existing ``entity_extraction`` prompt produces high-quality
    results on it. Entities listed in ``detected_entities`` become prime
    candidates for extraction since the prompt looks for named objects.
    """
    loc_parts: list[str] = []
    if extra_metadata:
        page_locator = _build_image_page_locator(extra_metadata)
        if page_locator:
            loc_parts.append(page_locator)
        bbox_text = _format_image_bbox_text(extra_metadata.get("source_bbox"))
        if bbox_text:
            loc_parts.append(f"原图位置: {bbox_text}")
        page_picture_index = extra_metadata.get("page_picture_index")
        if page_picture_index is not None:
            try:
                loc_parts.append(f"页内第 {int(page_picture_index) + 1} 张图")
            except (TypeError, ValueError):
                pass
        source_pdf = extra_metadata.get("source_pdf")
        if source_pdf:
            loc_parts.append(f"来源文件: {source_pdf}")
    loc = " · ".join(loc_parts) if loc_parts else file_path

    lines: list[str] = []
    lines.append(f"【图像】 blob_id={blob_id}")
    lines.append(f"【位置】 {loc}")
    extraction_mode = ""
    caption_hint = ""
    context_text = ""
    if extra_metadata:
        extraction_mode = str(extra_metadata.get("extraction_mode") or "").strip()
        caption_hint = str(extra_metadata.get("caption_hint") or "").strip()
        context_text = str(
            extra_metadata.get("context_text")
            or extra_metadata.get("page_text_excerpt")
            or ""
        ).strip()
    if extraction_mode:
        lines.append(f"【提取方式】 {extraction_mode}")
    if extra_metadata:
        merged_modes = extra_metadata.get("merged_extraction_modes")
        if isinstance(merged_modes, list):
            merged_modes = [str(mode).strip() for mode in merged_modes if str(mode).strip()]
            if merged_modes:
                lines.append(f"【合并提取来源】 {'、'.join(merged_modes)}")
        native_xref = extra_metadata.get("native_xref")
        if native_xref is not None:
            lines.append(f"【PDF原生图像】 xref={native_xref}")

    if not caption_json:
        lines.append("【说明】 未获得视觉模型标注。")
        if caption_hint:
            lines.append(f"【页面提示】 {caption_hint}")
        if include_context:
            raw_context_chunks = (
                extra_metadata.get("context_chunks") if extra_metadata else None
            )
            if isinstance(raw_context_chunks, list) and raw_context_chunks:
                lines.append("【图像邻近上下文块】")
                for chunk in raw_context_chunks[:3]:
                    if not isinstance(chunk, dict):
                        continue
                    chunk_order_index = chunk.get("chunk_order_index")
                    try:
                        chunk_label = f"Chunk #{int(chunk_order_index) + 1}"
                    except (TypeError, ValueError):
                        chunk_label = "Chunk"
                    chunk_content = str(chunk.get("content") or "").strip()
                    if not chunk_content:
                        continue
                    lines.append(f"- {chunk_label}: {chunk_content[:500]}")
            if context_text:
                lines.append("【图像邻近上下文】")
                lines.append(context_text[:1600])
        return "\n".join(lines) + "\n"

    category = caption_json.get("image_category", "")
    sub_type = caption_json.get("sub_type", "")
    if category or sub_type:
        lines.append(f"【类型】 {category}" + (f" / {sub_type}" if sub_type else ""))

    caption = caption_json.get("caption", "")
    if caption:
        lines.append(f"【概述】 {caption}")

    detailed = caption_json.get("detailed_description", "")
    if detailed:
        lines.append(f"【详细描述】 {detailed}")

    entities = caption_json.get("detected_entities")
    if isinstance(entities, list) and entities:
        joined = "、".join(str(e) for e in entities if e)
        if joined:
            lines.append(f"【识别到的对象】 {joined}")

    key_attrs = caption_json.get("key_attributes")
    if isinstance(key_attrs, dict) and key_attrs:
        attr_parts = []
        for k, v in key_attrs.items():
            if isinstance(v, (list, tuple)):
                v_str = "、".join(str(x) for x in v)
            else:
                v_str = str(v)
            attr_parts.append(f"{k}: {v_str}")
        if attr_parts:
            lines.append("【关键属性】 " + "; ".join(attr_parts))

    if caption_hint:
        lines.append(f"【页面提示】 {caption_hint}")
    if include_context:
        context_chunks = []
        if extra_metadata:
            raw_context_chunks = extra_metadata.get("context_chunks")
            if isinstance(raw_context_chunks, list):
                context_chunks = raw_context_chunks
        if context_chunks:
            lines.append("【图像邻近上下文块】")
            for chunk in context_chunks[:3]:
                if not isinstance(chunk, dict):
                    continue
                chunk_order_index = chunk.get("chunk_order_index")
                try:
                    chunk_label = f"Chunk #{int(chunk_order_index) + 1}"
                except (TypeError, ValueError):
                    chunk_label = "Chunk"
                chunk_content = str(chunk.get("content") or "").strip()
                if not chunk_content:
                    continue
                lines.append(f"- {chunk_label}: {chunk_content[:500]}")
        if context_text:
            lines.append("【图像邻近上下文】")
            lines.append(context_text[:1600])

    return "\n".join(lines) + "\n"


def _strip_appended_image_annotations(text: str) -> str:
    """Remove the synthetic image-annotation appendix from a full_doc payload.

    ``ainsert_document_with_images`` appends a deterministic marker section to
    the document text before sending it through the normal text/KG pipeline.
    When we need to rebuild multimodal data from already-stored image assets
    (because the original PDF is no longer on disk), we should start from the
    original document body instead of duplicating those annotation blocks.
    """
    if not text:
        return ""
    marker = "===== 本文档中的图像(自动提取)====="
    marker_index = text.find(marker)
    if marker_index == -1:
        return text
    return text[:marker_index].rstrip()


@final
@dataclass
class LightRAG:
    """LightRAG: Simple and Fast Retrieval-Augmented Generation."""

    # Directory
    # ---

    working_dir: str = field(default="./rag_storage")
    """Directory where cache and temporary files are stored."""

    # Storage
    # ---

    kv_storage: str = field(default="JsonKVStorage")
    """Storage backend for key-value data."""

    vector_storage: str = field(default="NanoVectorDBStorage")
    """Storage backend for vector embeddings."""

    graph_storage: str = field(default="NetworkXStorage")
    """Storage backend for knowledge graphs."""

    doc_status_storage: str = field(default="JsonDocStatusStorage")
    """Storage type for tracking document processing statuses."""

    image_blob_storage: str = field(default="FileSystemBlobStorage")
    """Storage backend for raw image (and other multimodal) binary blobs.

    Only instantiated when ``image_embedding_func`` is set. When the
    multimodal pipeline is unused, this field has no effect — existing
    text-only deployments behave exactly as before.
    """

    image_cosine_threshold: float = field(
        default=float(os.getenv("IMAGE_COSINE_THRESHOLD", "0.05"))
    )
    """Minimum cosine similarity threshold for ``images_vdb`` retrieval.

    Cross-modal (text query → image vector) similarities are systematically
    lower than text-text similarities in aligned-space multimodal models —
    typical range is 0.05 to 0.30 for semantically matching pairs, vs
    0.6+ for text-text pairs. The default ``cosine_better_than_threshold``
    of 0.2 (calibrated for text retrieval) would silently drop most
    legitimate cross-modal matches.

    Only used when the multimodal pipeline is enabled. Override via the
    ``IMAGE_COSINE_THRESHOLD`` environment variable or this constructor
    parameter.
    """

    # Workspace
    # ---

    workspace: str = field(default_factory=lambda: os.getenv("WORKSPACE", ""))
    """Workspace for data isolation. Defaults to empty string if WORKSPACE environment variable is not set."""

    # ---
    # TODO: Deprecated, use setup_logger in utils.py instead
    log_level: int | None = field(default=None)
    log_file_path: str | None = field(default=None)

    # Query parameters
    # ---

    top_k: int = field(default=get_env_value("TOP_K", DEFAULT_TOP_K, int))
    """Number of entities/relations to retrieve for each query."""

    chunk_top_k: int = field(
        default=get_env_value("CHUNK_TOP_K", DEFAULT_CHUNK_TOP_K, int)
    )
    """Maximum number of chunks in context."""

    max_entity_tokens: int = field(
        default=get_env_value("MAX_ENTITY_TOKENS", DEFAULT_MAX_ENTITY_TOKENS, int)
    )
    """Maximum number of tokens for entity in context."""

    max_relation_tokens: int = field(
        default=get_env_value("MAX_RELATION_TOKENS", DEFAULT_MAX_RELATION_TOKENS, int)
    )
    """Maximum number of tokens for relation in context."""

    max_total_tokens: int = field(
        default=get_env_value("MAX_TOTAL_TOKENS", DEFAULT_MAX_TOTAL_TOKENS, int)
    )
    """Maximum total tokens in context (including system prompt, entities, relations and chunks)."""

    cosine_threshold: int = field(
        default=get_env_value("COSINE_THRESHOLD", DEFAULT_COSINE_THRESHOLD, int)
    )
    """Cosine threshold of vector DB retrieval for entities, relations and chunks."""

    related_chunk_number: int = field(
        default=get_env_value("RELATED_CHUNK_NUMBER", DEFAULT_RELATED_CHUNK_NUMBER, int)
    )
    """Number of related chunks to grab from single entity or relation."""

    kg_chunk_pick_method: str = field(
        default=get_env_value("KG_CHUNK_PICK_METHOD", DEFAULT_KG_CHUNK_PICK_METHOD, str)
    )
    """Method for selecting text chunks: 'WEIGHT' for weight-based selection, 'VECTOR' for embedding similarity-based selection."""

    # Entity extraction
    # ---

    entity_extract_max_gleaning: int = field(
        default=get_env_value("MAX_GLEANING", DEFAULT_MAX_GLEANING, int)
    )
    """Maximum number of entity extraction attempts for ambiguous content."""

    max_extract_input_tokens: int = field(
        default=get_env_value(
            "MAX_EXTRACT_INPUT_TOKENS", DEFAULT_MAX_EXTRACT_INPUT_TOKENS, int
        )
    )
    """Maximum tokens allowed for entity extraction input context."""

    force_llm_summary_on_merge: int = field(
        default=get_env_value(
            "FORCE_LLM_SUMMARY_ON_MERGE", DEFAULT_FORCE_LLM_SUMMARY_ON_MERGE, int
        )
    )

    # Text chunking
    # ---

    chunk_token_size: int = field(default=int(os.getenv("CHUNK_SIZE", 1200)))
    """Maximum number of tokens per text chunk when splitting documents."""

    chunk_overlap_token_size: int = field(
        default=int(os.getenv("CHUNK_OVERLAP_SIZE", 100))
    )
    """Number of overlapping tokens between consecutive text chunks to preserve context."""

    tokenizer: Optional[Tokenizer] = field(default=None)
    """
    A function that returns a Tokenizer instance.
    If None, and a `tiktoken_model_name` is provided, a TiktokenTokenizer will be created.
    If both are None, the default TiktokenTokenizer is used.
    """

    tiktoken_model_name: str = field(default="gpt-4o-mini")
    """Model name used for tokenization when chunking text with tiktoken. Defaults to `gpt-4o-mini`."""

    chunking_func: Callable[
        [
            Tokenizer,
            str,
            Optional[str],
            bool,
            int,
            int,
        ],
        Union[List[Dict[str, Any]], Awaitable[List[Dict[str, Any]]]],
    ] = field(default_factory=lambda: chunking_by_token_size)
    """
    Custom chunking function for splitting text into chunks before processing.

    The function can be either synchronous or asynchronous.

    The function should take the following parameters:

        - `tokenizer`: A Tokenizer instance to use for tokenization.
        - `content`: The text to be split into chunks.
        - `split_by_character`: The character to split the text on. If None, the text is split into chunks of `chunk_token_size` tokens.
        - `split_by_character_only`: If True, the text is split only on the specified character.
        - `chunk_overlap_token_size`: The number of overlapping tokens between consecutive chunks.
        - `chunk_token_size`: The maximum number of tokens per chunk.


    The function should return a list of dictionaries (or an awaitable that resolves to a list),
    where each dictionary contains the following keys:
        - `tokens` (int): The number of tokens in the chunk.
        - `content` (str): The text content of the chunk.
        - `chunk_order_index` (int): Zero-based index indicating the chunk's order in the document.

    Defaults to `chunking_by_token_size` if not specified.
    """

    # Embedding
    # ---

    embedding_func: EmbeddingFunc | None = field(default=None)
    """Function for computing text embeddings. Must be set before use."""

    image_embedding_func: MultimodalEmbeddingFunc | None = field(default=None)
    """Optional multimodal embedding function for the image pipeline.

    When set, LightRAG initializes the image blob store, image metadata KV
    store, and (in Phase 3+) an image vector store alongside the existing
    text stack. When None (the default), the multimodal pipeline is
    disabled and the text-only behaviour is unchanged.

    Use :func:`lightrag.llm.tongyi.tongyi_multimodal_embedding` or any
    custom :class:`~lightrag.utils.MultimodalEmbeddingFunc` implementation.
    """

    embedding_token_limit: int | None = field(default=None, init=False)
    """Token limit for embedding model. Set automatically from embedding_func.max_token_size in __post_init__."""

    embedding_batch_num: int = field(default=int(os.getenv("EMBEDDING_BATCH_NUM", 10)))
    """Batch size for embedding computations."""

    embedding_func_max_async: int = field(
        default=int(os.getenv("EMBEDDING_FUNC_MAX_ASYNC", 8))
    )
    """Maximum number of concurrent embedding function calls."""

    embedding_cache_config: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False,
            "similarity_threshold": 0.95,
            "use_llm_check": False,
        }
    )
    """Configuration for embedding cache.
    - enabled: If True, enables caching to avoid redundant computations.
    - similarity_threshold: Minimum similarity score to use cached embeddings.
    - use_llm_check: If True, validates cached embeddings using an LLM.
    """

    default_embedding_timeout: int = field(
        default=int(os.getenv("EMBEDDING_TIMEOUT", DEFAULT_EMBEDDING_TIMEOUT))
    )

    # LLM Configuration
    # ---

    llm_model_func: Callable[..., object] | None = field(default=None)
    """Function for interacting with the large language model (LLM). Must be set before use."""

    query_llm_model_func: Callable[..., object] | None = field(default=None)
    """Optional separate LLM function used exclusively for query-time answer
    generation (the final LLM call in ``kg_query`` / ``naive_query``).

    When set, the query path uses this function for generating the answer
    while indexing (entity extraction, description summarization) continues
    to use ``llm_model_func``. Keyword extraction during queries also stays
    on ``llm_model_func`` since it's a fast structured-output call that
    doesn't benefit from reasoning.

    Primary use case: running the same model with different parameters —
    e.g. ``enable_thinking=false`` for indexing (cost-sensitive, high
    throughput) and ``enable_thinking=true`` for queries (quality-sensitive,
    low volume).

    When None (the default), all LLM calls go through ``llm_model_func``
    and behavior is identical to the pre-Phase-6 single-function setup.
    """

    vision_model_func: Callable[..., object] | None = field(default=None)
    """Optional vision-capable LLM function for image captioning.

    Used by the multimodal pipeline (Phase 3+) to turn an image into
    a structured JSON annotation that then flows into the normal text
    chunking + entity-extraction path. Typically a ``functools.partial``
    of :func:`lightrag.llm.openai.openai_complete_if_cache` bound to a
    vision model (e.g. ``qwen3-vl-plus`` via the DashScope compatible
    endpoint) — see ``.env`` ``VISION_*`` variables.

    When None, no image captioning is performed and the image pipeline
    degrades to embedding-only (still useful for cross-modal search).
    """

    llm_model_name: str = field(default="gpt-4o-mini")
    """Name of the LLM model used for generating responses."""

    summary_max_tokens: int = field(
        default=int(os.getenv("SUMMARY_MAX_TOKENS", DEFAULT_SUMMARY_MAX_TOKENS))
    )
    """Maximum tokens allowed for entity/relation description."""

    summary_context_size: int = field(
        default=int(os.getenv("SUMMARY_CONTEXT_SIZE", DEFAULT_SUMMARY_CONTEXT_SIZE))
    )
    """Maximum number of tokens allowed per LLM response."""

    summary_length_recommended: int = field(
        default=int(
            os.getenv("SUMMARY_LENGTH_RECOMMENDED", DEFAULT_SUMMARY_LENGTH_RECOMMENDED)
        )
    )
    """Recommended length of LLM summary output."""

    llm_model_max_async: int = field(
        default=int(os.getenv("MAX_ASYNC", DEFAULT_MAX_ASYNC))
    )
    """Maximum number of concurrent LLM calls."""

    llm_model_kwargs: dict[str, Any] = field(default_factory=dict)
    """Additional keyword arguments passed to the LLM model function."""

    llm_binding: str = field(default_factory=lambda: get_env_value("LLM_BINDING", "", str))
    """Logical LLM binding name (for example ``openai`` or ``azure_openai``)."""

    llm_binding_host: str | None = field(
        default_factory=lambda: get_env_value("LLM_BINDING_HOST", None, special_none=True)
    )
    """Provider host/base URL for OpenAI-compatible batch workflows."""

    llm_binding_api_key: str | None = field(
        default_factory=lambda: get_env_value(
            "LLM_BINDING_API_KEY",
            os.getenv("OPENAI_API_KEY"),
            special_none=True,
        )
    )
    """Provider API key for OpenAI-compatible batch workflows."""

    llm_openai_options: dict[str, Any] = field(default_factory=dict)
    """Resolved OpenAI-compatible request options for indexing calls."""

    entity_extraction_mode: str = field(
        default_factory=lambda: get_env_value("ENTITY_EXTRACTION_MODE", "auto", str)
    )
    """Entity extraction execution mode: ``auto``, ``realtime``, or ``batch``."""

    entity_extraction_batch_min_chunks: int = field(
        default_factory=lambda: get_env_value(
            "ENTITY_EXTRACTION_BATCH_MIN_CHUNKS", 120, int
        )
    )
    """Minimum chunk count before ``auto`` mode switches to batch extraction."""

    entity_extraction_batch_poll_interval_seconds: int = field(
        default_factory=lambda: get_env_value(
            "ENTITY_EXTRACTION_BATCH_POLL_INTERVAL_SECONDS", 5, int
        )
    )
    """Polling interval in seconds for OpenAI-compatible batch entity extraction."""

    entity_extraction_batch_timeout_seconds: int = field(
        default_factory=lambda: get_env_value(
            "ENTITY_EXTRACTION_BATCH_TIMEOUT_SECONDS", 3600, int
        )
    )
    """Timeout in seconds before an entity extraction batch is cancelled/fallback begins."""

    entity_extraction_batch_fallback_to_realtime: bool = field(
        default_factory=lambda: get_env_value(
            "ENTITY_EXTRACTION_BATCH_FALLBACK_TO_REALTIME", True, bool
        )
    )
    """Fallback to realtime extraction when batch mode fails or returns partial results."""

    default_llm_timeout: int = field(
        default=int(os.getenv("LLM_TIMEOUT", DEFAULT_LLM_TIMEOUT))
    )

    # Rerank Configuration
    # ---

    rerank_model_func: Callable[..., object] | None = field(default=None)
    """Function for reranking retrieved documents. All rerank configurations (model name, API keys, top_k, etc.) should be included in this function. Optional."""

    min_rerank_score: float = field(
        default=get_env_value("MIN_RERANK_SCORE", DEFAULT_MIN_RERANK_SCORE, float)
    )
    """Minimum rerank score threshold for filtering chunks after reranking."""

    # Storage
    # ---

    vector_db_storage_cls_kwargs: dict[str, Any] = field(default_factory=dict)
    """Additional parameters for vector database storage."""

    enable_llm_cache: bool = field(default=True)
    """Enables caching for LLM responses to avoid redundant computations."""

    enable_llm_cache_for_entity_extract: bool = field(default=True)
    """If True, enables caching for entity extraction steps to reduce LLM costs."""

    # Extensions
    # ---

    max_parallel_insert: int = field(
        default=int(os.getenv("MAX_PARALLEL_INSERT", DEFAULT_MAX_PARALLEL_INSERT))
    )
    """Maximum number of parallel insert operations."""

    max_graph_nodes: int = field(
        default=get_env_value("MAX_GRAPH_NODES", DEFAULT_MAX_GRAPH_NODES, int)
    )
    """Maximum number of graph nodes to return in knowledge graph queries."""

    max_source_ids_per_entity: int = field(
        default=get_env_value(
            "MAX_SOURCE_IDS_PER_ENTITY", DEFAULT_MAX_SOURCE_IDS_PER_ENTITY, int
        )
    )
    """Maximum number of source (chunk) ids in entity Grpah + VDB."""

    max_source_ids_per_relation: int = field(
        default=get_env_value(
            "MAX_SOURCE_IDS_PER_RELATION",
            DEFAULT_MAX_SOURCE_IDS_PER_RELATION,
            int,
        )
    )
    """Maximum number of source (chunk) ids in relation Graph + VDB."""

    source_ids_limit_method: str = field(
        default_factory=lambda: normalize_source_ids_limit_method(
            get_env_value(
                "SOURCE_IDS_LIMIT_METHOD",
                DEFAULT_SOURCE_IDS_LIMIT_METHOD,
                str,
            )
        )
    )
    """Strategy for enforcing source_id limits: IGNORE_NEW or FIFO."""

    max_file_paths: int = field(
        default=get_env_value("MAX_FILE_PATHS", DEFAULT_MAX_FILE_PATHS, int)
    )
    """Maximum number of file paths to store in entity/relation file_path field."""

    file_path_more_placeholder: str = field(default=DEFAULT_FILE_PATH_MORE_PLACEHOLDER)
    """Placeholder text when file paths exceed max_file_paths limit."""

    addon_params: dict[str, Any] = field(
        default_factory=lambda: {
            "language": get_env_value(
                "SUMMARY_LANGUAGE", DEFAULT_SUMMARY_LANGUAGE, str
            ),
            "entity_types": get_env_value("ENTITY_TYPES", DEFAULT_ENTITY_TYPES, list),
        }
    )

    # Storages Management
    # ---

    # TODO: Deprecated (LightRAG will never initialize storage automatically on creation，and finalize should be call before destroying)
    auto_manage_storages_states: bool = field(default=False)
    """If True, lightrag will automatically calls initialize_storages and finalize_storages at the appropriate times."""

    cosine_better_than_threshold: float = field(
        default=float(os.getenv("COSINE_THRESHOLD", 0.2))
    )

    ollama_server_infos: Optional[OllamaServerInfos] = field(default=None)
    """Configuration for Ollama server information."""

    _storages_status: StoragesStatus = field(default=StoragesStatus.NOT_CREATED)

    def __post_init__(self):
        from lightrag.kg.shared_storage import (
            initialize_share_data,
        )

        # Handle deprecated parameters
        if self.log_level is not None:
            warnings.warn(
                "WARNING: log_level parameter is deprecated, use setup_logger in utils.py instead",
                UserWarning,
                stacklevel=2,
            )
        if self.log_file_path is not None:
            warnings.warn(
                "WARNING: log_file_path parameter is deprecated, use setup_logger in utils.py instead",
                UserWarning,
                stacklevel=2,
            )

        # Remove these attributes to prevent their use
        if hasattr(self, "log_level"):
            delattr(self, "log_level")
        if hasattr(self, "log_file_path"):
            delattr(self, "log_file_path")

        initialize_share_data()

        if not os.path.exists(self.working_dir):
            logger.info(f"Creating working directory {self.working_dir}")
            os.makedirs(self.working_dir)

        # Verify storage implementation compatibility and environment variables
        storage_configs = [
            ("KV_STORAGE", self.kv_storage),
            ("VECTOR_STORAGE", self.vector_storage),
            ("GRAPH_STORAGE", self.graph_storage),
            ("DOC_STATUS_STORAGE", self.doc_status_storage),
        ]

        for storage_type, storage_name in storage_configs:
            # Verify storage implementation compatibility
            verify_storage_implementation(storage_type, storage_name)
            # Check environment variables
            check_storage_env_vars(storage_name)

        # Ensure vector_db_storage_cls_kwargs has required fields
        self.vector_db_storage_cls_kwargs = {
            "cosine_better_than_threshold": self.cosine_better_than_threshold,
            **self.vector_db_storage_cls_kwargs,
        }

        # Init Tokenizer
        # Post-initialization hook to handle backward compatabile tokenizer initialization based on provided parameters
        if self.tokenizer is None:
            if self.tiktoken_model_name:
                self.tokenizer = TiktokenTokenizer(self.tiktoken_model_name)
            else:
                self.tokenizer = TiktokenTokenizer()

        # Initialize ollama_server_infos if not provided
        if self.ollama_server_infos is None:
            self.ollama_server_infos = OllamaServerInfos()

        # Validate config
        if self.force_llm_summary_on_merge < 3:
            logger.warning(
                f"force_llm_summary_on_merge should be at least 3, got {self.force_llm_summary_on_merge}"
            )
        if self.summary_context_size > self.max_total_tokens:
            logger.warning(
                f"summary_context_size({self.summary_context_size}) should no greater than max_total_tokens({self.max_total_tokens})"
            )
        if self.summary_length_recommended > self.summary_max_tokens:
            logger.warning(
                f"max_total_tokens({self.summary_max_tokens}) should greater than summary_length_recommended({self.summary_length_recommended})"
            )

        # Init Embedding
        # Step 1: Capture embedding_func and max_token_size before applying rate_limit decorator
        original_embedding_func = self.embedding_func
        embedding_max_token_size = None
        if self.embedding_func and hasattr(self.embedding_func, "max_token_size"):
            embedding_max_token_size = self.embedding_func.max_token_size
            logger.debug(
                f"Captured embedding max_token_size: {embedding_max_token_size}"
            )
        self.embedding_token_limit = embedding_max_token_size

        # Fix global_config now
        global_config = asdict(self)
        # Restore original EmbeddingFunc object (asdict converts it to dict)
        global_config["embedding_func"] = original_embedding_func

        _print_config = ",\n  ".join([f"{k} = {v}" for k, v in global_config.items()])
        logger.debug(f"LightRAG init with param:\n  {_print_config}\n")

        # Step 2: Apply priority wrapper decorator to EmbeddingFunc's inner func
        # Create a NEW EmbeddingFunc instance with the wrapped func to avoid mutating the caller's object
        # This ensures _generate_collection_suffix can still access attributes (model_name, embedding_dim)
        # while preventing side effects when the same EmbeddingFunc is reused across multiple LightRAG instances
        if self.embedding_func is not None:
            wrapped_func = priority_limit_async_func_call(
                self.embedding_func_max_async,
                llm_timeout=self.default_embedding_timeout,
                queue_name="Embedding func",
            )(self.embedding_func.func)
            # Use dataclasses.replace() to create a new instance, leaving the original unchanged
            self.embedding_func = replace(self.embedding_func, func=wrapped_func)

        # Initialize all storages
        self.key_string_value_json_storage_cls: type[BaseKVStorage] = (
            self._get_storage_class(self.kv_storage)
        )  # type: ignore
        self.vector_db_storage_cls: type[BaseVectorStorage] = self._get_storage_class(
            self.vector_storage
        )  # type: ignore
        self.graph_storage_cls: type[BaseGraphStorage] = self._get_storage_class(
            self.graph_storage
        )  # type: ignore
        self.key_string_value_json_storage_cls = partial(  # type: ignore
            self.key_string_value_json_storage_cls, global_config=global_config
        )
        self.vector_db_storage_cls = partial(  # type: ignore
            self.vector_db_storage_cls, global_config=global_config
        )
        self.graph_storage_cls = partial(  # type: ignore
            self.graph_storage_cls, global_config=global_config
        )

        # Initialize document status storage
        self.doc_status_storage_cls = self._get_storage_class(self.doc_status_storage)

        self.llm_response_cache: BaseKVStorage = self.key_string_value_json_storage_cls(  # type: ignore
            namespace=NameSpace.KV_STORE_LLM_RESPONSE_CACHE,
            workspace=self.workspace,
            global_config=global_config,
            embedding_func=self.embedding_func,
        )

        self.text_chunks: BaseKVStorage = self.key_string_value_json_storage_cls(  # type: ignore
            namespace=NameSpace.KV_STORE_TEXT_CHUNKS,
            workspace=self.workspace,
            embedding_func=self.embedding_func,
        )

        self.full_docs: BaseKVStorage = self.key_string_value_json_storage_cls(  # type: ignore
            namespace=NameSpace.KV_STORE_FULL_DOCS,
            workspace=self.workspace,
            embedding_func=self.embedding_func,
        )

        self.full_entities: BaseKVStorage = self.key_string_value_json_storage_cls(  # type: ignore
            namespace=NameSpace.KV_STORE_FULL_ENTITIES,
            workspace=self.workspace,
            embedding_func=self.embedding_func,
        )

        self.full_relations: BaseKVStorage = self.key_string_value_json_storage_cls(  # type: ignore
            namespace=NameSpace.KV_STORE_FULL_RELATIONS,
            workspace=self.workspace,
            embedding_func=self.embedding_func,
        )

        self.entity_chunks: BaseKVStorage = self.key_string_value_json_storage_cls(  # type: ignore
            namespace=NameSpace.KV_STORE_ENTITY_CHUNKS,
            workspace=self.workspace,
            embedding_func=self.embedding_func,
        )

        self.relation_chunks: BaseKVStorage = self.key_string_value_json_storage_cls(  # type: ignore
            namespace=NameSpace.KV_STORE_RELATION_CHUNKS,
            workspace=self.workspace,
            embedding_func=self.embedding_func,
        )

        self.chunk_entity_relation_graph: BaseGraphStorage = self.graph_storage_cls(  # type: ignore
            namespace=NameSpace.GRAPH_STORE_CHUNK_ENTITY_RELATION,
            workspace=self.workspace,
            embedding_func=self.embedding_func,
        )

        self.entities_vdb: BaseVectorStorage = self.vector_db_storage_cls(  # type: ignore
            namespace=NameSpace.VECTOR_STORE_ENTITIES,
            workspace=self.workspace,
            embedding_func=self.embedding_func,
            meta_fields={"entity_name", "source_id", "content", "file_path"},
        )
        self.relationships_vdb: BaseVectorStorage = self.vector_db_storage_cls(  # type: ignore
            namespace=NameSpace.VECTOR_STORE_RELATIONSHIPS,
            workspace=self.workspace,
            embedding_func=self.embedding_func,
            meta_fields={"src_id", "tgt_id", "source_id", "content", "file_path"},
        )
        self.chunks_vdb: BaseVectorStorage = self.vector_db_storage_cls(  # type: ignore
            namespace=NameSpace.VECTOR_STORE_CHUNKS,
            workspace=self.workspace,
            embedding_func=self.embedding_func,
            meta_fields={"full_doc_id", "content", "file_path"},
        )

        # Initialize document status storage
        self.doc_status: DocStatusStorage = self.doc_status_storage_cls(
            namespace=NameSpace.DOC_STATUS,
            workspace=self.workspace,
            global_config=global_config,
            embedding_func=None,
        )

        # --- Multimodal pipeline storages (gated) ---------------------------
        # These are only instantiated when the user wires in a multimodal
        # embedding function. For text-only deployments, all four remain
        # None and no new storage files/directories are created.
        self.image_blob_store: BaseBlobStorage | None = None
        self.image_metadata: BaseKVStorage | None = None
        self.images_vdb: BaseVectorStorage | None = None
        if self.image_embedding_func is not None:
            image_blob_storage_cls = self._get_storage_class(self.image_blob_storage)
            self.image_blob_store = image_blob_storage_cls(
                namespace=NameSpace.BLOB_STORE_IMAGES,
                workspace=self.workspace,
                global_config=global_config,
            )
            # Sidecar KV store for image metadata (caption JSON, source-doc
            # backlink, page / bbox for PDF-extracted images, etc.). Reuses
            # the same KV backend as text chunks — no new dependencies.
            self.image_metadata = self.key_string_value_json_storage_cls(  # type: ignore
                namespace=NameSpace.KV_STORE_IMAGE_META,
                workspace=self.workspace,
                embedding_func=self.embedding_func,
            )
            # Image vector store — backed by the multimodal embedding function.
            # Upserts use upsert_with_embeddings() to bypass the text-oriented
            # embedding_func call path (image bytes cannot flow through
            # text_encode). Queries use the default __call__ -> text_encode
            # routing in MultimodalEmbeddingFunc, which gives cross-modal
            # retrieval (text query -> image results) for free without
            # touching the vector store internals.
            self.images_vdb = self.vector_db_storage_cls(  # type: ignore
                namespace=NameSpace.VECTOR_STORE_IMAGES,
                workspace=self.workspace,
                embedding_func=self.image_embedding_func,
                meta_fields={
                    "blob_id",
                    "blob_ref",
                    "caption",
                    "image_category",
                    "sub_type",
                    "full_doc_id",
                    "file_path",
                    "source_page",
                },
            )
            # Cross-modal retrieval calibration: the 0.2 default threshold
            # baked into vector_db_storage_cls_kwargs is tuned for text-text
            # similarity and would silently drop most legitimate image hits.
            # Override with the multimodal-aware threshold.
            self.images_vdb.cosine_better_than_threshold = self.image_cosine_threshold
            logger.info(
                f"[{self.workspace}] Multimodal pipeline enabled: "
                f"blob_store={self.image_blob_storage} "
                f"image_embedding_func={getattr(self.image_embedding_func, 'model_name', '?')} "
                f"image_embedding_dim={self.image_embedding_func.embedding_dim} "
                f"image_cosine_threshold={self.image_cosine_threshold} "
                f"vision_model_func={'set' if self.vision_model_func else 'unset'}"
            )

        # Directly use llm_response_cache, don't create a new object
        hashing_kv = self.llm_response_cache

        # Get timeout from LLM model kwargs for dynamic timeout calculation
        self.llm_model_func = priority_limit_async_func_call(
            self.llm_model_max_async,
            llm_timeout=self.default_llm_timeout,
            queue_name="LLM func",
        )(
            partial(
                self.llm_model_func,  # type: ignore
                hashing_kv=hashing_kv,
                **self.llm_model_kwargs,
            )
        )

        # Query-specific LLM: same priority queue setup as the indexing
        # LLM but potentially different model parameters (e.g.
        # enable_thinking=true for higher answer quality). When not set,
        # kg_query / naive_query fall back to llm_model_func transparently.
        if self.query_llm_model_func is not None:
            self.query_llm_model_func = priority_limit_async_func_call(
                self.llm_model_max_async,
                llm_timeout=self.default_llm_timeout,
                queue_name="Query LLM func",
            )(
                partial(
                    self.query_llm_model_func,  # type: ignore
                    hashing_kv=hashing_kv,
                    **self.llm_model_kwargs,
                )
            )

        self._storages_status = StoragesStatus.CREATED

    async def initialize_storages(self):
        """Storage initialization must be called one by one to prevent deadlock"""
        if self._storages_status == StoragesStatus.CREATED:
            # Set the first initialized workspace will set the default workspace
            # Allows namespace operation without specifying workspace for backward compatibility
            default_workspace = get_default_workspace()
            if default_workspace is None:
                set_default_workspace(self.workspace)
            elif default_workspace != self.workspace:
                logger.info(
                    f"Creating LightRAG instance with workspace='{self.workspace}' "
                    f"while default workspace is set to '{default_workspace}'"
                )

            # Auto-initialize pipeline_status for this workspace
            from lightrag.kg.shared_storage import initialize_pipeline_status

            await initialize_pipeline_status(workspace=self.workspace)

            for storage in (
                self.full_docs,
                self.text_chunks,
                self.full_entities,
                self.full_relations,
                self.entity_chunks,
                self.relation_chunks,
                self.entities_vdb,
                self.relationships_vdb,
                self.chunks_vdb,
                self.chunk_entity_relation_graph,
                self.llm_response_cache,
                self.doc_status,
                # Multimodal stores — None unless image_embedding_func is set.
                self.image_blob_store,
                self.image_metadata,
                self.images_vdb,
            ):
                if storage:
                    # logger.debug(f"Initializing storage: {storage}")
                    await storage.initialize()

            self._storages_status = StoragesStatus.INITIALIZED
            logger.debug("All storage types initialized")

    async def finalize_storages(self):
        """Asynchronously finalize the storages with improved error handling"""
        if self._storages_status == StoragesStatus.INITIALIZED:
            storages = [
                ("full_docs", self.full_docs),
                ("text_chunks", self.text_chunks),
                ("full_entities", self.full_entities),
                ("full_relations", self.full_relations),
                ("entity_chunks", self.entity_chunks),
                ("relation_chunks", self.relation_chunks),
                ("entities_vdb", self.entities_vdb),
                ("relationships_vdb", self.relationships_vdb),
                ("chunks_vdb", self.chunks_vdb),
                ("chunk_entity_relation_graph", self.chunk_entity_relation_graph),
                ("llm_response_cache", self.llm_response_cache),
                ("doc_status", self.doc_status),
                # Multimodal stores — None unless image_embedding_func is set.
                ("image_blob_store", self.image_blob_store),
                ("image_metadata", self.image_metadata),
                ("images_vdb", self.images_vdb),
            ]

            # Finalize each storage individually to ensure one failure doesn't prevent others from closing
            successful_finalizations = []
            failed_finalizations = []

            for storage_name, storage in storages:
                if storage:
                    try:
                        await storage.finalize()
                        successful_finalizations.append(storage_name)
                        logger.debug(f"Successfully finalized {storage_name}")
                    except Exception as e:
                        error_msg = f"Failed to finalize {storage_name}: {e}"
                        logger.error(error_msg)
                        failed_finalizations.append(storage_name)

            # Log summary of finalization results
            if successful_finalizations:
                logger.info(
                    f"Successfully finalized {len(successful_finalizations)} storages"
                )

            if failed_finalizations:
                logger.error(
                    f"Failed to finalize {len(failed_finalizations)} storages: {', '.join(failed_finalizations)}"
                )
            else:
                logger.debug("All storages finalized successfully")

            self._storages_status = StoragesStatus.FINALIZED

    async def check_and_migrate_data(self):
        """Check if data migration is needed and perform migration if necessary"""
        async with get_data_init_lock():
            try:
                # Check if migration is needed:
                # 1. chunk_entity_relation_graph has entities and relations (count > 0)
                # 2. full_entities and full_relations are empty

                # Get all entity labels from graph
                all_entity_labels = (
                    await self.chunk_entity_relation_graph.get_all_labels()
                )

                if not all_entity_labels:
                    logger.debug("No entities found in graph, skipping migration check")
                    return

                try:
                    # Initialize chunk tracking storage after migration
                    await self._migrate_chunk_tracking_storage()
                except Exception as e:
                    logger.error(f"Error during chunk_tracking migration: {e}")
                    raise e

                # Check if full_entities and full_relations are empty
                # Get all processed documents to check their entity/relation data
                try:
                    processed_docs = await self.doc_status.get_docs_by_status(
                        DocStatus.PROCESSED
                    )

                    if not processed_docs:
                        logger.debug("No processed documents found, skipping migration")
                        return

                    # Check first few documents to see if they have full_entities/full_relations data
                    migration_needed = True
                    checked_count = 0
                    max_check = min(5, len(processed_docs))  # Check up to 5 documents

                    for doc_id in list(processed_docs.keys())[:max_check]:
                        checked_count += 1
                        entity_data = await self.full_entities.get_by_id(doc_id)
                        relation_data = await self.full_relations.get_by_id(doc_id)

                        if entity_data or relation_data:
                            migration_needed = False
                            break

                    if not migration_needed:
                        logger.debug(
                            "Full entities/relations data already exists, no migration needed"
                        )
                        return

                    logger.info(
                        f"Data migration needed: found {len(all_entity_labels)} entities in graph but no full_entities/full_relations data"
                    )

                    # Perform migration
                    await self._migrate_entity_relation_data(processed_docs)

                except Exception as e:
                    logger.error(f"Error during migration check: {e}")
                    raise e

            except Exception as e:
                logger.error(f"Error in data migration check: {e}")
                raise e

    async def _migrate_entity_relation_data(self, processed_docs: dict):
        """Migrate existing entity and relation data to full_entities and full_relations storage"""
        logger.info(f"Starting data migration for {len(processed_docs)} documents")

        # Create mapping from chunk_id to doc_id
        chunk_to_doc = {}
        for doc_id, doc_status in processed_docs.items():
            chunk_ids = (
                doc_status.chunks_list
                if hasattr(doc_status, "chunks_list") and doc_status.chunks_list
                else []
            )
            for chunk_id in chunk_ids:
                chunk_to_doc[chunk_id] = doc_id

        # Initialize document entity and relation mappings
        doc_entities = {}  # doc_id -> set of entity_names
        doc_relations = {}  # doc_id -> set of relation_pairs (as tuples)

        # Get all nodes and edges from graph
        all_nodes = await self.chunk_entity_relation_graph.get_all_nodes()
        all_edges = await self.chunk_entity_relation_graph.get_all_edges()

        # Process all nodes once
        for node in all_nodes:
            if "source_id" in node:
                entity_id = node.get("entity_id") or node.get("id")
                if not entity_id:
                    continue

                # Get chunk IDs from source_id
                source_ids = node["source_id"].split(GRAPH_FIELD_SEP)

                # Find which documents this entity belongs to
                for chunk_id in source_ids:
                    doc_id = chunk_to_doc.get(chunk_id)
                    if doc_id:
                        if doc_id not in doc_entities:
                            doc_entities[doc_id] = set()
                        doc_entities[doc_id].add(entity_id)

        # Process all edges once
        for edge in all_edges:
            if "source_id" in edge:
                src = edge.get("source")
                tgt = edge.get("target")
                if not src or not tgt:
                    continue

                # Get chunk IDs from source_id
                source_ids = edge["source_id"].split(GRAPH_FIELD_SEP)

                # Find which documents this relation belongs to
                for chunk_id in source_ids:
                    doc_id = chunk_to_doc.get(chunk_id)
                    if doc_id:
                        if doc_id not in doc_relations:
                            doc_relations[doc_id] = set()
                        # Use tuple for set operations, convert to list later
                        doc_relations[doc_id].add(tuple(sorted((src, tgt))))

        # Store the results in full_entities and full_relations
        migration_count = 0

        # Store entities
        if doc_entities:
            entities_data = {}
            for doc_id, entity_set in doc_entities.items():
                entities_data[doc_id] = {
                    "entity_names": list(entity_set),
                    "count": len(entity_set),
                }
            await self.full_entities.upsert(entities_data)

        # Store relations
        if doc_relations:
            relations_data = {}
            for doc_id, relation_set in doc_relations.items():
                # Convert tuples back to lists
                relations_data[doc_id] = {
                    "relation_pairs": [list(pair) for pair in relation_set],
                    "count": len(relation_set),
                }
            await self.full_relations.upsert(relations_data)

        migration_count = len(
            set(list(doc_entities.keys()) + list(doc_relations.keys()))
        )

        # Persist the migrated data
        await self.full_entities.index_done_callback()
        await self.full_relations.index_done_callback()

        logger.info(
            f"Data migration completed: migrated {migration_count} documents with entities/relations"
        )

    async def _migrate_chunk_tracking_storage(self) -> None:
        """Ensure entity/relation chunk tracking KV stores exist and are seeded."""

        if not self.entity_chunks or not self.relation_chunks:
            return

        need_entity_migration = False
        need_relation_migration = False

        try:
            need_entity_migration = await self.entity_chunks.is_empty()
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error(f"Failed to check entity chunks storage: {exc}")
            raise exc

        try:
            need_relation_migration = await self.relation_chunks.is_empty()
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error(f"Failed to check relation chunks storage: {exc}")
            raise exc

        if not need_entity_migration and not need_relation_migration:
            return

        BATCH_SIZE = 500  # Process 500 records per batch

        if need_entity_migration:
            try:
                nodes = await self.chunk_entity_relation_graph.get_all_nodes()
            except Exception as exc:
                logger.error(f"Failed to fetch nodes for chunk migration: {exc}")
                nodes = []

            logger.info(f"Starting chunk_tracking data migration: {len(nodes)} nodes")

            # Process nodes in batches
            total_nodes = len(nodes)
            total_batches = (total_nodes + BATCH_SIZE - 1) // BATCH_SIZE
            total_migrated = 0

            for batch_idx in range(total_batches):
                start_idx = batch_idx * BATCH_SIZE
                end_idx = min((batch_idx + 1) * BATCH_SIZE, total_nodes)
                batch_nodes = nodes[start_idx:end_idx]

                upsert_payload: dict[str, dict[str, object]] = {}
                for node in batch_nodes:
                    entity_id = node.get("entity_id") or node.get("id")
                    if not entity_id:
                        continue

                    raw_source = node.get("source_id") or ""
                    chunk_ids = [
                        chunk_id
                        for chunk_id in raw_source.split(GRAPH_FIELD_SEP)
                        if chunk_id
                    ]
                    if not chunk_ids:
                        continue

                    upsert_payload[entity_id] = {
                        "chunk_ids": chunk_ids,
                        "count": len(chunk_ids),
                    }

                if upsert_payload:
                    await self.entity_chunks.upsert(upsert_payload)
                    total_migrated += len(upsert_payload)
                    logger.info(
                        f"Processed entity batch {batch_idx + 1}/{total_batches}: {len(upsert_payload)} records (total: {total_migrated}/{total_nodes})"
                    )

            if total_migrated > 0:
                # Persist entity_chunks data to disk
                await self.entity_chunks.index_done_callback()
                logger.info(
                    f"Entity chunk_tracking migration completed: {total_migrated} records persisted"
                )

        if need_relation_migration:
            try:
                edges = await self.chunk_entity_relation_graph.get_all_edges()
            except Exception as exc:
                logger.error(f"Failed to fetch edges for chunk migration: {exc}")
                edges = []

            logger.info(f"Starting chunk_tracking data migration: {len(edges)} edges")

            # Process edges in batches
            total_edges = len(edges)
            total_batches = (total_edges + BATCH_SIZE - 1) // BATCH_SIZE
            total_migrated = 0

            for batch_idx in range(total_batches):
                start_idx = batch_idx * BATCH_SIZE
                end_idx = min((batch_idx + 1) * BATCH_SIZE, total_edges)
                batch_edges = edges[start_idx:end_idx]

                upsert_payload: dict[str, dict[str, object]] = {}
                for edge in batch_edges:
                    src = edge.get("source") or edge.get("src_id") or edge.get("src")
                    tgt = edge.get("target") or edge.get("tgt_id") or edge.get("tgt")
                    if not src or not tgt:
                        continue

                    raw_source = edge.get("source_id") or ""
                    chunk_ids = [
                        chunk_id
                        for chunk_id in raw_source.split(GRAPH_FIELD_SEP)
                        if chunk_id
                    ]
                    if not chunk_ids:
                        continue

                    storage_key = make_relation_chunk_key(src, tgt)
                    upsert_payload[storage_key] = {
                        "chunk_ids": chunk_ids,
                        "count": len(chunk_ids),
                    }

                if upsert_payload:
                    await self.relation_chunks.upsert(upsert_payload)
                    total_migrated += len(upsert_payload)
                    logger.info(
                        f"Processed relation batch {batch_idx + 1}/{total_batches}: {len(upsert_payload)} records (total: {total_migrated}/{total_edges})"
                    )

            if total_migrated > 0:
                # Persist relation_chunks data to disk
                await self.relation_chunks.index_done_callback()
                logger.info(
                    f"Relation chunk_tracking migration completed: {total_migrated} records persisted"
                )

    async def get_graph_labels(self):
        text = await self.chunk_entity_relation_graph.get_all_labels()
        return text

    async def get_knowledge_graph(
        self,
        node_label: str,
        max_depth: int = 3,
        max_nodes: int = None,
    ) -> KnowledgeGraph:
        """Get knowledge graph for a given label

        Args:
            node_label (str): Label to get knowledge graph for
            max_depth (int): Maximum depth of graph
            max_nodes (int, optional): Maximum number of nodes to return. Defaults to self.max_graph_nodes.

        Returns:
            KnowledgeGraph: Knowledge graph containing nodes and edges
        """
        # Use self.max_graph_nodes as default if max_nodes is None
        if max_nodes is None:
            max_nodes = self.max_graph_nodes
        else:
            # Limit max_nodes to not exceed self.max_graph_nodes
            max_nodes = min(max_nodes, self.max_graph_nodes)

        return await self.chunk_entity_relation_graph.get_knowledge_graph(
            node_label, max_depth, max_nodes
        )

    def _get_storage_class(self, storage_name: str) -> Callable[..., Any]:
        # Direct imports for default storage implementations
        if storage_name == "JsonKVStorage":
            from lightrag.kg.json_kv_impl import JsonKVStorage

            return JsonKVStorage
        elif storage_name == "NanoVectorDBStorage":
            from lightrag.kg.nano_vector_db_impl import NanoVectorDBStorage

            return NanoVectorDBStorage
        elif storage_name == "NetworkXStorage":
            from lightrag.kg.networkx_impl import NetworkXStorage

            return NetworkXStorage
        elif storage_name == "JsonDocStatusStorage":
            from lightrag.kg.json_doc_status_impl import JsonDocStatusStorage

            return JsonDocStatusStorage
        else:
            # Fallback to dynamic import for other storage implementations
            import_path = STORAGES[storage_name]
            storage_class = lazy_external_import(import_path, storage_name)
            return storage_class

    def insert(
        self,
        input: str | list[str],
        split_by_character: str | None = None,
        split_by_character_only: bool = False,
        ids: str | list[str] | None = None,
        file_paths: str | list[str] | None = None,
        track_id: str | None = None,
    ) -> str:
        """Sync Insert documents with checkpoint support

        Args:
            input: Single document string or list of document strings
            split_by_character: if split_by_character is not None, split the string by character, if chunk longer than
            chunk_token_size, it will be split again by token size.
            split_by_character_only: if split_by_character_only is True, split the string by character only, when
            split_by_character is None, this parameter is ignored.
            ids: single string of the document ID or list of unique document IDs, if not provided, MD5 hash IDs will be generated
            file_paths: single string of the file path or list of file paths, used for citation
            track_id: tracking ID for monitoring processing status, if not provided, will be generated

        Returns:
            str: tracking ID for monitoring processing status
        """
        loop = always_get_an_event_loop()
        return loop.run_until_complete(
            self.ainsert(
                input,
                split_by_character,
                split_by_character_only,
                ids,
                file_paths,
                track_id,
            )
        )

    async def ainsert(
        self,
        input: str | list[str],
        split_by_character: str | None = None,
        split_by_character_only: bool = False,
        ids: str | list[str] | None = None,
        file_paths: str | list[str] | None = None,
        track_id: str | None = None,
    ) -> str:
        """Async Insert documents with checkpoint support

        Args:
            input: Single document string or list of document strings
            split_by_character: if split_by_character is not None, split the string by character, if chunk longer than
            chunk_token_size, it will be split again by token size.
            split_by_character_only: if split_by_character_only is True, split the string by character only, when
            split_by_character is None, this parameter is ignored.
            ids: list of unique document IDs, if not provided, MD5 hash IDs will be generated
            file_paths: list of file paths corresponding to each document, used for citation
            track_id: tracking ID for monitoring processing status, if not provided, will be generated

        Returns:
            str: tracking ID for monitoring processing status
        """
        # Generate track_id if not provided
        if track_id is None:
            track_id = generate_track_id("insert")

        await self.apipeline_enqueue_documents(input, ids, file_paths, track_id)
        await self.apipeline_process_enqueue_documents(
            split_by_character, split_by_character_only
        )

        return track_id

    async def ainsert_image(
        self,
        image: "bytes | str | Path",
        *,
        file_path: str | None = None,
        mime_type: str = "image/jpeg",
        extra_metadata: dict[str, Any] | None = None,
        ids: str | None = None,
        track_id: str | None = None,
    ) -> str:
        """Insert a single image into the multimodal pipeline.

        Pipeline stages:
            1. Normalize the input to raw bytes.
            2. Persist the original bytes to the blob store (content-hashed id).
            3. Generate a structured caption via ``vision_model_func`` (if set).
            4. Compute the image-side embedding via ``image_embedding_func``.
            5. Upsert the vector into ``images_vdb`` with meta_fields populated
               from the caption JSON.
            6. Upsert the sidecar record into the ``image_metadata`` KV store.
            7. Thread the human-readable annotation text through the standard
               text ingestion pipeline so entity extraction populates the KG
               with entities detected in the image. This means the knowledge
               graph learns about objects IN the image (poplar trees, tower
               cranes, rebar, ...) from the caption text, without needing the
               vision model at query time.

        Args:
            image: The image content. Accepts raw ``bytes``, a local file path
                (``str`` or ``Path``), or anything else ``Path(...).read_bytes()``
                can handle.
            file_path: Logical file path recorded in the KG and blob sidecar.
                When ``image`` is a path and this is ``None``, it defaults to
                ``str(image)``. For bytes-only input, leave it ``None`` and a
                synthetic ``image://<blob_id>`` reference is used.
            mime_type: MIME type for blob storage and vision model forwarding.
                Defaults to ``"image/jpeg"``.
            extra_metadata: Arbitrary user metadata merged into the blob
                sidecar and into ``image_metadata``. Reserved keys:
                ``source_page``, ``source_doc_id``, ``source_file_path``,
                ``total_pages``.
            ids: Optional explicit document id for the KG linkage. If omitted,
                a content-hashed ``doc-img-<...>`` id is derived from the image
                bytes.
            track_id: Optional tracking id for the pipeline. If omitted a new
                one is generated with prefix ``insert_image``.

        Returns:
            The tracking id for monitoring pipeline progress (mirrors
            :meth:`ainsert`).

        Raises:
            RuntimeError: If ``image_embedding_func`` is not configured.
            FileNotFoundError: If ``image`` is a path that does not exist.
            TypeError: If ``image`` is not bytes or a path-like.
        """
        if self.image_embedding_func is None or self.images_vdb is None:
            raise RuntimeError(
                "ainsert_image requires the multimodal pipeline. Pass "
                "image_embedding_func=... when constructing LightRAG."
            )
        if self.image_blob_store is None or self.image_metadata is None:
            raise RuntimeError(
                "ainsert_image called before initialize_storages(). "
                "Call `await rag.initialize_storages()` first."
            )

        # --- (1) Normalize input to bytes ---
        from pathlib import Path as _Path

        if isinstance(image, (bytes, bytearray, memoryview)):
            image_bytes = bytes(image)
        elif isinstance(image, (str, _Path)):
            image_path_obj = _Path(image)
            if not image_path_obj.is_file():
                raise FileNotFoundError(f"ainsert_image: image file not found: {image}")
            image_bytes = image_path_obj.read_bytes()
            if file_path is None:
                file_path = str(image_path_obj)
        else:
            raise TypeError(
                f"ainsert_image: image must be bytes or a path-like, "
                f"got {type(image).__name__}"
            )

        if not image_bytes:
            raise ValueError("ainsert_image: image is empty (0 bytes)")

        # --- (2) Derive content-hashed ids ---
        blob_id = compute_mdhash_id(image_bytes, prefix="img-")
        doc_id = ids or compute_mdhash_id(image_bytes, prefix="doc-img-")
        effective_file_path = file_path or f"image://{blob_id}"

        if track_id is None:
            track_id = generate_track_id("insert_image")

        logger.info(
            f"[{self.workspace}] ainsert_image: blob_id={blob_id} "
            f"doc_id={doc_id} size={len(image_bytes)} file_path={effective_file_path}"
        )

        # --- (3) Persist raw bytes to the blob store ---
        blob_sidecar_meta = {
            "source_doc_id": doc_id,
            "source_file_path": effective_file_path,
            **(extra_metadata or {}),
        }
        blob_ref = await self.image_blob_store.put(
            blob_id,
            image_bytes,
            content_type=mime_type,
            metadata=blob_sidecar_meta,
        )

        # --- (4) Vision caption (optional, falls back gracefully) ---
        language = self.addon_params.get("language", "English")
        caption_json: dict[str, Any] | None = None
        if self.vision_model_func is not None:
            try:
                caption_raw = await self.vision_model_func(
                    prompt=PROMPTS["image_caption_user_prompt"].format(
                        language=language
                    ),
                    system_prompt=PROMPTS["image_caption_system_prompt"].format(
                        language=language
                    ),
                    image_data=image_bytes,
                    image_mime_type=mime_type,
                )
                caption_json = _parse_image_caption_json(caption_raw)
            except Exception as e:
                logger.warning(
                    f"[{self.workspace}] ainsert_image: vision captioning "
                    f"failed for {blob_id}: {type(e).__name__}: {e}"
                )
                caption_json = None
        else:
            logger.debug(
                f"[{self.workspace}] ainsert_image: no vision_model_func "
                f"configured, skipping caption generation for {blob_id}"
            )

        annotation_text = _image_annotation_to_text(
            caption_json,
            blob_id=blob_id,
            file_path=effective_file_path,
            extra_metadata=extra_metadata,
        )

        # --- (5) Compute image-side embedding ---
        vectors = await self.image_embedding_func.image_encode([image_bytes])
        if vectors is None or len(vectors) != 1:
            raise RuntimeError(
                f"ainsert_image: image_embedding_func.image_encode returned "
                f"unexpected shape for {blob_id}"
            )
        image_vector = vectors[0]

        # --- (6) Upsert vector into images_vdb (bypasses embedding_func) ---
        caption_safe = caption_json or {}
        vdb_record: dict[str, Any] = {
            "blob_id": blob_id,
            "blob_ref": blob_ref,
            "caption": str(caption_safe.get("caption", ""))[:500],
            "image_category": str(caption_safe.get("image_category", "")),
            "sub_type": str(caption_safe.get("sub_type", "")),
            "full_doc_id": doc_id,
            "file_path": effective_file_path,
        }
        if extra_metadata and "source_page" in extra_metadata:
            vdb_record["source_page"] = extra_metadata["source_page"]
        await self.images_vdb.upsert_with_embeddings(
            data={blob_id: vdb_record},
            embeddings={blob_id: image_vector},
        )

        # --- (7) Upsert sidecar into image_metadata KV ---
        await self.image_metadata.upsert(
            {
                blob_id: {
                    "blob_ref": blob_ref,
                    "content_type": mime_type,
                    "caption_json": caption_json,
                    "annotation_text": annotation_text,
                    "source_doc_id": doc_id,
                    "source_file_path": effective_file_path,
                    "source_page": (
                        extra_metadata.get("source_page")
                        if isinstance(extra_metadata, dict)
                        else None
                    ),
                    "source_bbox": (
                        extra_metadata.get("source_bbox")
                        if isinstance(extra_metadata, dict)
                        else None
                    ),
                    "picture_index": (
                        extra_metadata.get("picture_index")
                        if isinstance(extra_metadata, dict)
                        else None
                    ),
                    "context_text": (
                        extra_metadata.get("context_text")
                        if isinstance(extra_metadata, dict)
                        else None
                    ),
                    "extraction_mode": (
                        extra_metadata.get("extraction_mode")
                        if isinstance(extra_metadata, dict)
                        else None
                    ),
                    "extra": extra_metadata or {},
                }
            }
        )

        # --- (8) Thread annotation text through the standard text pipeline ---
        # This is what lets entity extraction populate the KG with the
        # objects/entities detected IN the image (trees, cranes, rebar, ...).
        # The doc_id and file_path flow through unchanged so KG source_ids
        # point back at this image, enabling "which image mentions X?" queries.
        await self.apipeline_enqueue_documents(
            input=annotation_text,
            ids=doc_id,
            file_paths=effective_file_path,
            track_id=track_id,
        )
        await self.apipeline_process_enqueue_documents()

        # --- (9) Reverse index: doc_status[doc_id].metadata.image_ids ---
        # Write the blob_id into the doc's metadata so the
        # GET /documents/{doc_id}/images endpoint can enumerate images for
        # a document in O(1). Read-modify-write because Phase 5 will extend
        # this to cover PDF documents that contain multiple embedded images.
        #
        # CRITICAL: JsonDocStatusStorage.upsert() uses dict.update() which
        # REPLACES the entire value at each key. We MUST spread all existing
        # fields into the merged record, otherwise status / file_path /
        # content_summary / chunks_count / etc. are wiped out and subsequent
        # pipeline calls crash on the record's missing 'status' field.
        try:
            existing_status = await self.doc_status.get_by_id(doc_id)
            if existing_status is None:
                logger.warning(
                    f"[{self.workspace}] doc_status missing for {doc_id} "
                    "after apipeline_process_enqueue_documents — skipping "
                    "reverse image index write."
                )
            else:
                existing_meta = existing_status.get("metadata", {}) or {}
                existing_image_ids = list(existing_meta.get("image_ids") or [])
                if blob_id not in existing_image_ids:
                    existing_image_ids.append(blob_id)
                merged_meta = {
                    **existing_meta,
                    "modality": "image"
                    if existing_meta.get("modality") in (None, "image")
                    else "mixed",
                    "image_ids": existing_image_ids,
                    "source_kind": existing_meta.get(
                        "source_kind", "direct_image_upload"
                    ),
                }
                # Spread the existing full record first so fields like
                # status, file_path, content_summary, chunks_count,
                # chunks_list, created_at, updated_at, track_id are all
                # preserved. Then override metadata and multimodal_processed.
                merged_record = {
                    **existing_status,
                    "metadata": merged_meta,
                    "multimodal_processed": True,
                }
                await self.doc_status.upsert({doc_id: merged_record})
        except Exception as e:
            logger.warning(
                f"[{self.workspace}] Failed to write reverse image index "
                f"for doc_id={doc_id}: {type(e).__name__}: {e}"
            )

        # Flush image-side stores alongside the text pipeline's index_done.
        # apipeline_process_enqueue_documents persists chunks/entities/graph
        # but does not know about image_blob_store / image_metadata / images_vdb.
        await asyncio.gather(
            self.image_blob_store.index_done_callback(),
            self.image_metadata.index_done_callback(),
            self.images_vdb.index_done_callback(),
            self.doc_status.index_done_callback(),
        )

        logger.info(
            f"[{self.workspace}] ainsert_image complete: blob_id={blob_id} "
            f"category={caption_safe.get('image_category', 'unknown')} "
            f"track_id={track_id}"
        )
        return track_id

    async def ainsert_document_with_images(
        self,
        text_content: str,
        extracted_images: list[dict[str, Any]],
        *,
        file_path: str,
        ids: str | None = None,
        track_id: str | None = None,
        reuse_existing_images: bool = True,
        allow_existing_doc_id: bool = False,
        initial_doc_status: DocStatus = DocStatus.PENDING,
        initial_doc_metadata: dict[str, Any] | None = None,
    ) -> str:
        """Ingest a document whose text AND embedded images should both
        participate in the multimodal pipeline.

        This is the Phase 5 entry point for PDFs (and eventually DOCX /
        PPTX) parsed by ``_convert_with_docling_multimodal``. It ensures
        that:

        1. Every embedded image is persisted to the blob store, captioned
           by ``vision_model_func``, embedded by ``image_embedding_func``,
           and upserted into ``images_vdb`` and ``image_metadata``.
        2. All image annotations are appended to the parent document's
           text so a single trip through the text pipeline populates the
           KG with entities detected in EVERY image as well as the body
           text. The KG source_id for those entities points at the parent
           document, which means "which image mentions rebar?" queries
           can fan out through the parent doc's image_ids list.
        3. The parent ``doc_id`` is shared by every extracted image, so
           the reverse index in ``doc_status[doc_id].metadata.image_ids``
           enumerates all images for the document and
           ``GET /documents/{doc_id}/images`` returns them in a single
           call.

        When ``extracted_images`` is empty, this method degrades to a
        plain text ingest (equivalent to ``ainsert(text_content)``), so
        callers can use it unconditionally when Docling's picture
        extraction returns no pictures.

        Args:
            text_content: The full text/markdown of the document (typically
                from ``_convert_with_docling_multimodal``).
            extracted_images: List of picture info dicts as returned by
                ``_convert_with_docling_multimodal``. Each must have
                ``bytes``, ``mime_type``, and is expected to optionally
                carry ``page_no``, ``bbox``, ``caption_hint``,
                ``picture_index``, ``pil_size``.
            file_path: The original document path (PDF / DOCX / PPTX).
                Used as the file_path for KG citation and for the sidecar
                metadata on each stored blob.
            ids: Optional explicit parent doc_id. Defaults to a content
                hash of ``text_content``.
            track_id: Optional tracking id. Defaults to a new
                ``insert_doc_images`` prefixed id.
            reuse_existing_images: When True, try to reuse any existing
                caption JSON and image embeddings for matching content-hashed
                image blobs instead of re-running the vision model and
                image encoder. Blob sidecars and annotation_text are still
                refreshed so page / bbox / context metadata stay current.
            allow_existing_doc_id: When True, allow Step A to overwrite an
                existing doc_status/full_docs row for ``ids``. Used by
                in-place multimodal rebuild so the document stays visible
                while its content is regenerated.
            initial_doc_status: Initial doc status written during Step A.
                Defaults to ``PENDING`` for normal ingest; rebuild flows may
                use ``PROCESSING`` so the document appears under the active
                processing bucket immediately.
            initial_doc_metadata: Optional metadata to seed on the Step A
                doc_status row before the text pipeline takes over.

        Returns:
            The pipeline tracking id.

        Raises:
            RuntimeError: If the multimodal pipeline is not configured.
        """
        if self.image_embedding_func is None or self.images_vdb is None:
            raise RuntimeError(
                "ainsert_document_with_images requires the multimodal "
                "pipeline. Pass image_embedding_func=... when constructing "
                "LightRAG."
            )
        if self.image_blob_store is None or self.image_metadata is None:
            raise RuntimeError(
                "ainsert_document_with_images called before "
                "initialize_storages(). Call `await rag.initialize_storages()` "
                "first."
            )
        if not text_content and not extracted_images:
            raise ValueError(
                "ainsert_document_with_images: text_content is empty and "
                "no images were extracted — nothing to ingest."
            )
        raw_extracted_image_count = len(extracted_images)
        extracted_images = _dedupe_extracted_images_for_multimodal_ingest(
            extracted_images
        )
        if raw_extracted_image_count != len(extracted_images):
            logger.info(
                f"[{self.workspace}] multimodal ingest deduped extracted images: "
                f"{raw_extracted_image_count} -> {len(extracted_images)}"
            )
        if not text_content and not extracted_images:
            raise ValueError(
                "ainsert_document_with_images: all extracted images were "
                "filtered/deduplicated away and text_content is empty — "
                "nothing to ingest."
            )

        # Parent document id: hash of the extracted text, same convention
        # as the plain `ainsert` path so that re-ingesting the same doc is
        # idempotent and doesn't create duplicates.
        parent_doc_id = ids or compute_mdhash_id(
            text_content or file_path, prefix="doc-"
        )
        if track_id is None:
            track_id = generate_track_id("insert_doc_images")

        effective_file_path = file_path or f"doc://{parent_doc_id}"
        language = self.addon_params.get("language", "English")

        logger.info(
            f"[{self.workspace}] ainsert_document_with_images: "
            f"doc_id={parent_doc_id} file_path={effective_file_path} "
            f"text_len={len(text_content or '')} "
            f"n_images={len(extracted_images)}"
        )

        await self._attach_context_chunks_to_extracted_images(
            text_content, extracted_images
        )

        # --- Process images concurrently ---
        # Each image needs: blob write + vision caption (~15s) + embedding
        # (~1s) + vdb upsert + metadata upsert. Serial processing of 50
        # images takes ~13 min; 8-way concurrency brings it to ~2 min.
        image_concurrency = min(self.llm_model_max_async, 8)
        sem = asyncio.Semaphore(image_concurrency)
        caption_prompt = PROMPTS["image_caption_user_prompt"].format(
            language=language
        )
        caption_sys = PROMPTS["image_caption_system_prompt"].format(
            language=language
        )
        n_total = len(extracted_images)
        existing_meta_by_blob: dict[str, dict[str, Any]] = {}
        existing_vectors_by_blob: dict[str, Any] = {}

        if reuse_existing_images and extracted_images:
            raw_blob_ids: list[str] = []
            for img_info in extracted_images:
                img_bytes = img_info.get("bytes")
                if img_bytes:
                    raw_blob_ids.append(compute_mdhash_id(img_bytes, prefix="img-"))
            unique_blob_ids = list(dict.fromkeys(raw_blob_ids))
            if unique_blob_ids:
                try:
                    metadata_records, vectors_by_blob = await asyncio.gather(
                        self.image_metadata.get_by_ids(unique_blob_ids),
                        self.images_vdb.get_vectors_by_ids(unique_blob_ids),
                    )
                    for blob_id, metadata_record in zip(
                        unique_blob_ids, metadata_records
                    ):
                        if isinstance(metadata_record, dict):
                            existing_meta_by_blob[blob_id] = metadata_record
                    if isinstance(vectors_by_blob, dict):
                        existing_vectors_by_blob = vectors_by_blob
                    logger.info(
                        f"[{self.workspace}] image-cache probe: "
                        f"{len(existing_meta_by_blob)}/{len(unique_blob_ids)} captions, "
                        f"{len(existing_vectors_by_blob)}/{len(unique_blob_ids)} embeddings reusable"
                    )
                except Exception as e:
                    logger.warning(
                        f"[{self.workspace}] image cache probe failed: "
                        f"{type(e).__name__}: {e}"
                    )

        async def _process_one_image(
            idx: int, img_info: dict[str, Any]
        ) -> dict[str, Any] | None:
            async with sem:
                img_bytes = img_info.get("bytes")
                if not img_bytes:
                    return None
                mime_type = img_info.get("mime_type") or "image/png"
                page_no = img_info.get("page_no")
                source_bbox = img_info.get("bbox")
                picture_index = img_info.get("picture_index")
                page_picture_index = img_info.get("page_picture_index")
                extraction_mode = img_info.get("extraction_mode")
                source_printed_page = img_info.get("source_printed_page")
                source_page_label = img_info.get("source_page_label")
                native_xref = img_info.get("native_xref")
                merged_extraction_modes = img_info.get("merged_extraction_modes")
                caption_hint = img_info.get("caption_hint")
                page_text_excerpt = img_info.get("page_text_excerpt")
                context_text = img_info.get("context_text")
                context_chunk_ids = img_info.get("context_chunk_ids")
                context_chunks = img_info.get("context_chunks")

                blob_id = compute_mdhash_id(img_bytes, prefix="img-")
                existing_meta = existing_meta_by_blob.get(blob_id) or {}
                logger.info(
                    f"[{self.workspace}] image [{idx+1}/{n_total}] "
                    f"page={page_no} blob_id={blob_id} processing..."
                )

                # (a) Blob store
                try:
                        blob_ref = await self.image_blob_store.put(
                            blob_id,
                            img_bytes,
                            content_type=mime_type,
                            metadata={
                                "source_doc_id": parent_doc_id,
                                "source_file_path": effective_file_path,
                                "source_page": page_no,
                                "source_printed_page": source_printed_page,
                                "source_page_label": source_page_label,
                                "source_bbox": source_bbox,
                                "picture_index": picture_index,
                                "page_picture_index": page_picture_index,
                                "extraction_mode": extraction_mode,
                                "native_xref": native_xref,
                                "merged_extraction_modes": merged_extraction_modes,
                                "context_text": context_text,
                                "context_chunk_ids": context_chunk_ids,
                                "context_chunks": context_chunks,
                            },
                        )
                except Exception as e:
                    logger.warning(
                        f"[{self.workspace}] blob_store.put failed for "
                        f"picture {picture_index}: {type(e).__name__}: {e}"
                    )
                    return None

                # (b) Vision caption (optional)
                caption_json: dict[str, Any] | None = None
                cached_caption_json = existing_meta.get("caption_json")
                if _caption_json_has_meaningful_summary(cached_caption_json):
                    caption_json = cached_caption_json
                elif cached_caption_json is not None and self.vision_model_func is None:
                    caption_json = cached_caption_json
                elif self.vision_model_func is not None:
                    if cached_caption_json is not None:
                        logger.info(
                            f"[{self.workspace}] cached caption for {blob_id} "
                            f"is too sparse; regenerating vision summary"
                        )
                    try:
                        caption_raw = await self.vision_model_func(
                            prompt=caption_prompt,
                            system_prompt=caption_sys,
                            image_data=img_bytes,
                            image_mime_type=mime_type,
                        )
                        caption_json = _parse_image_caption_json(caption_raw)
                    except Exception as e:
                        logger.warning(
                            f"[{self.workspace}] vision failed for "
                            f"{blob_id}: {type(e).__name__}: {e}"
                        )

                # (c) Image embedding
                image_vector = None
                existing_vector_raw = existing_vectors_by_blob.get(blob_id)
                if existing_vector_raw is not None:
                    try:
                        import numpy as np

                        image_vector = (
                            existing_vector_raw
                            if isinstance(existing_vector_raw, np.ndarray)
                            else np.asarray(existing_vector_raw, dtype=np.float32)
                        )
                    except Exception as e:
                        logger.warning(
                            f"[{self.workspace}] failed to coerce cached embedding "
                            f"for {blob_id}: {type(e).__name__}: {e}"
                        )
                if image_vector is None:
                    try:
                        vectors = await self.image_embedding_func.image_encode(
                            [img_bytes]
                        )
                        if vectors is not None and len(vectors) == 1:
                            image_vector = vectors[0]
                    except Exception as e:
                        logger.warning(
                            f"[{self.workspace}] image_encode failed for "
                            f"{blob_id}: {type(e).__name__}: {e}"
                        )

                # (d) Upsert vector
                if image_vector is not None:
                    caption_safe = caption_json or {}
                    vdb_record: dict[str, Any] = {
                        "blob_id": blob_id,
                        "blob_ref": blob_ref,
                        "caption": str(
                            caption_safe.get("caption", "")
                        )[:500],
                        "image_category": str(
                            caption_safe.get("image_category", "")
                        ),
                        "sub_type": str(
                            caption_safe.get("sub_type", "")
                        ),
                        "full_doc_id": parent_doc_id,
                        "file_path": effective_file_path,
                    }
                    if page_no is not None:
                        vdb_record["source_page"] = page_no
                    if source_printed_page is not None:
                        vdb_record["source_printed_page"] = source_printed_page
                    try:
                        await self.images_vdb.upsert_with_embeddings(
                            data={blob_id: vdb_record},
                            embeddings={blob_id: image_vector},
                        )
                    except Exception as e:
                        logger.warning(
                            f"[{self.workspace}] images_vdb upsert failed "
                            f"for {blob_id}: {type(e).__name__}: {e}"
                        )

                # (e) Image metadata KV
                annotation_text = _image_annotation_to_text(
                    caption_json,
                    blob_id=blob_id,
                    file_path=effective_file_path,
                    extra_metadata={
                        "source_page": page_no,
                        "source_printed_page": source_printed_page,
                        "source_page_label": source_page_label,
                        "source_bbox": source_bbox,
                        "picture_index": picture_index,
                        "page_picture_index": page_picture_index,
                        "extraction_mode": extraction_mode,
                        "native_xref": native_xref,
                        "merged_extraction_modes": merged_extraction_modes,
                        "caption_hint": caption_hint,
                        "page_text_excerpt": page_text_excerpt,
                        "context_text": context_text,
                        "context_chunk_ids": context_chunk_ids,
                        "context_chunks": context_chunks,
                        "source_pdf": os.path.basename(file_path)
                        if file_path
                        else None,
                    },
                    include_context=False,
                )
                try:
                    await self.image_metadata.upsert(
                        {
                            blob_id: {
                                "blob_ref": blob_ref,
                                "content_type": mime_type,
                                "caption_json": caption_json,
                                "annotation_text": annotation_text,
                                "source_doc_id": parent_doc_id,
                                "source_file_path": effective_file_path,
                                "source_page": page_no,
                                "source_printed_page": source_printed_page,
                                "source_page_label": source_page_label,
                                "source_bbox": source_bbox,
                                "picture_index": picture_index,
                                "page_picture_index": page_picture_index,
                                "context_text": context_text,
                                "extraction_mode": extraction_mode,
                                "native_xref": native_xref,
                                "merged_extraction_modes": merged_extraction_modes,
                                "context_chunk_ids": context_chunk_ids,
                                "context_chunks": context_chunks,
                                "extra": {
                                    "caption_hint": caption_hint,
                                    "page_text_excerpt": page_text_excerpt,
                                    "context_text": context_text,
                                    "extraction_mode": extraction_mode,
                                    "page_picture_index": page_picture_index,
                                    "source_printed_page": source_printed_page,
                                    "source_page_label": source_page_label,
                                    "native_xref": native_xref,
                                    "merged_extraction_modes": merged_extraction_modes,
                                    "context_chunk_ids": context_chunk_ids,
                                    "context_chunks": context_chunks,
                                },
                            }
                        }
                    )
                except Exception as e:
                    logger.warning(
                        f"[{self.workspace}] image_metadata upsert failed "
                        f"for {blob_id}: {type(e).__name__}: {e}"
                    )

                logger.info(
                    f"[{self.workspace}] image [{idx+1}/{n_total}] "
                    f"page={page_no} blob_id={blob_id} done"
                )
                return {
                    "blob_id": blob_id,
                    "blob_ref": blob_ref,
                    "page_no": page_no,
                    "annotation_text": annotation_text,
                    "caption_json": caption_json,
                }

        logger.info(
            f"[{self.workspace}] Processing {n_total} images with "
            f"concurrency={image_concurrency}"
        )
        raw_results = await asyncio.gather(
            *[
                _process_one_image(i, img)
                for i, img in enumerate(extracted_images)
            ],
            return_exceptions=True,
        )
        per_image_results: list[dict[str, Any]] = []
        for r in raw_results:
            if isinstance(r, Exception):
                logger.warning(
                    f"[{self.workspace}] image processing exception: "
                    f"{type(r).__name__}: {r}"
                )
            elif r is not None:
                per_image_results.append(r)

        # --- Step A: Enqueue the PLAIN text immediately ---
        # This creates a PENDING doc_status entry so the frontend sees the
        # document right away. Image annotations will be appended BEFORE
        # the text pipeline actually processes the chunks.
        await self.apipeline_enqueue_documents(
            input=text_content or f"[Document with {len(extracted_images)} images]",
            ids=parent_doc_id,
            file_paths=effective_file_path,
            track_id=track_id,
            allow_existing_ids=allow_existing_doc_id,
            initial_status=initial_doc_status,
            initial_metadata=initial_doc_metadata,
        )
        logger.info(
            f"[{self.workspace}] Document enqueued as PENDING "
            f"(text only, images processing in parallel)"
        )

        # --- Step B: Build augmented text with image annotations ---
        if per_image_results:
            sections = ["", "", "===== 本文档中的图像(自动提取)====="]
            for r in per_image_results:
                sections.append("")
                sections.append(r["annotation_text"].rstrip())
            augmented_text = (text_content or "") + "\n".join(sections) + "\n"
        else:
            augmented_text = text_content or ""

        # --- Step C: Update full_docs with augmented text ---
        # The enqueue in Step A stored the plain text. Now overwrite with
        # the augmented version (text + image annotations) BEFORE the text
        # pipeline processes it, so entity extraction sees everything.
        await self.full_docs.upsert(
            {
                parent_doc_id: {
                    "content": augmented_text,
                    "file_path": effective_file_path,
                }
            }
        )

        # --- Step D: Process the enqueued document ---
        await self.apipeline_process_enqueue_documents()

        # --- Reverse index: doc_status.metadata.image_ids ---
        # Same read-modify-write pattern as ainsert_image step 9: preserve
        # ALL existing doc_status fields (status, file_path, chunks_count,
        # etc.) and only override metadata + multimodal_processed. The
        # Phase 4 bug was caused by spreading only metadata in an upsert
        # and losing the status field — don't repeat it here.
        try:
            existing_status = await self.doc_status.get_by_id(parent_doc_id)
            if existing_status is None:
                logger.warning(
                    f"[{self.workspace}] doc_status missing for "
                    f"{parent_doc_id} after apipeline_process_enqueue — "
                    "skipping reverse image index write."
                )
            else:
                existing_meta = existing_status.get("metadata", {}) or {}
                new_image_ids = [r["blob_id"] for r in per_image_results]
                merged_image_ids = list(dict.fromkeys(new_image_ids))
                merged_meta = {
                    **existing_meta,
                    "modality": "mixed"
                    if text_content and per_image_results
                    else ("image" if per_image_results else "text"),
                    "image_ids": merged_image_ids,
                    "source_kind": "pdf_extracted",
                    "n_images_extracted": len(per_image_results),
                }
                merged_record = {
                    **existing_status,
                    "metadata": merged_meta,
                    "multimodal_processed": True,
                }
                await self.doc_status.upsert({parent_doc_id: merged_record})
        except Exception as e:
            logger.warning(
                f"[{self.workspace}] Failed to write reverse image index "
                f"for doc_id={parent_doc_id}: {type(e).__name__}: {e}"
            )

        # Flush image-side stores alongside the text pipeline's index_done.
        await asyncio.gather(
            self.image_blob_store.index_done_callback(),
            self.image_metadata.index_done_callback(),
            self.images_vdb.index_done_callback(),
            self.doc_status.index_done_callback(),
        )

        logger.info(
            f"[{self.workspace}] ainsert_document_with_images complete: "
            f"doc_id={parent_doc_id} images={len(per_image_results)} "
            f"track_id={track_id}"
        )
        return track_id

    async def _attach_context_chunks_to_extracted_images(
        self,
        text_content: str,
        extracted_images: list[dict[str, Any]],
    ) -> None:
        """Attach nearby text chunks to extracted image metadata.

        The multimodal retrieval path already stores page-local context text,
        but for PDF figure recall we also want the *actual document chunks*
        surrounding that figure so the LLM can see the same chunk boundaries
        used by the text retrieval path.
        """
        if not text_content or not extracted_images:
            return

        def _normalize_match_text(text: Any) -> str:
            if text is None:
                return ""
            return re.sub(r"\s+", " ", str(text)).strip()

        def _extract_match_snippets(text: Any) -> list[str]:
            normalized = _normalize_match_text(text)
            if not normalized:
                return []

            snippets: list[str] = []
            raw_parts = re.split(r"[\r\n]+|(?<=[。！？.!?；;])", str(text))
            for part in raw_parts:
                part_norm = _normalize_match_text(part)
                if len(part_norm) >= 24:
                    snippets.append(part_norm[:160])

            if len(normalized) >= 48:
                snippets.extend(
                    [
                        normalized[:96],
                        normalized[max(0, len(normalized) // 2 - 48) : len(normalized) // 2 + 48],
                        normalized[-96:],
                    ]
                )
            else:
                snippets.append(normalized)

            unique_snippets: list[str] = []
            for snippet in snippets:
                cleaned = snippet.strip()
                if len(cleaned) < 16:
                    continue
                if cleaned not in unique_snippets:
                    unique_snippets.append(cleaned)
                if len(unique_snippets) >= 6:
                    break
            return unique_snippets

        def _score_chunk_match(chunk_text: str, snippets: list[str]) -> int:
            score = 0
            for snippet in snippets:
                if snippet and snippet in chunk_text:
                    score += len(snippet)
                elif len(snippet) >= 48:
                    half = snippet[: len(snippet) // 2].strip()
                    if half and half in chunk_text:
                        score += len(half)
            return score

        try:
            chunking_result = self.chunking_func(
                self.tokenizer,
                text_content,
                None,
                False,
                self.chunk_overlap_token_size,
                self.chunk_token_size,
            )
            if inspect.isawaitable(chunking_result):
                chunking_result = await chunking_result
        except Exception as e:
            logger.warning(
                f"[{self.workspace}] failed to derive text chunks for image context: "
                f"{type(e).__name__}: {e}"
            )
            return

        if not isinstance(chunking_result, (list, tuple)):
            logger.warning(
                f"[{self.workspace}] chunking_func returned unexpected type "
                f"{type(chunking_result)} while deriving image context chunks"
            )
            return

        text_chunks: list[dict[str, Any]] = []
        for fallback_index, chunk in enumerate(chunking_result):
            if not isinstance(chunk, dict):
                continue
            content = str(chunk.get("content") or "").strip()
            if not content:
                continue
            chunk_order_index = chunk.get("chunk_order_index", fallback_index)
            try:
                chunk_order_index = int(chunk_order_index)
            except (TypeError, ValueError):
                chunk_order_index = fallback_index
            text_chunks.append(
                {
                    "chunk_id": compute_mdhash_id(content, prefix="chunk-"),
                    "chunk_order_index": chunk_order_index,
                    "content": content,
                    "match_text": _normalize_match_text(content),
                }
            )

        if not text_chunks:
            return

        matched_count = 0
        for img_info in extracted_images:
            if img_info.get("context_chunks"):
                continue
            primary_anchor = img_info.get("page_text_excerpt") or ""
            fallback_anchor = img_info.get("context_text") or ""
            snippets = _extract_match_snippets(primary_anchor) or _extract_match_snippets(
                fallback_anchor
            )
            if not snippets:
                continue

            best_index = -1
            best_score = 0
            for index, chunk in enumerate(text_chunks):
                score = _score_chunk_match(chunk["match_text"], snippets)
                if score > best_score:
                    best_score = score
                    best_index = index

            if best_index < 0 or best_score <= 0:
                continue

            start = max(0, best_index - 1)
            end = min(len(text_chunks), best_index + 2)
            context_chunks = []
            context_chunk_ids = []
            for chunk in text_chunks[start:end]:
                excerpt = str(chunk.get("content") or "").strip()
                if len(excerpt) > 500:
                    excerpt = excerpt[:500].rstrip() + "…"
                context_chunks.append(
                    {
                        "chunk_id": chunk["chunk_id"],
                        "chunk_order_index": chunk["chunk_order_index"],
                        "content": excerpt,
                    }
                )
                context_chunk_ids.append(chunk["chunk_id"])

            if context_chunks:
                img_info["context_chunks"] = context_chunks
                img_info["context_chunk_ids"] = context_chunk_ids
                matched_count += 1

        if matched_count:
            logger.info(
                f"[{self.workspace}] attached nearby text chunks for "
                f"{matched_count}/{len(extracted_images)} extracted images"
            )

    async def _delete_image_side_resources(self, image_ids: list[str]) -> None:
        """Delete image-only resources without touching the text/KG side."""
        normalized_ids = [
            image_id for image_id in image_ids if isinstance(image_id, str) and image_id
        ]
        if not normalized_ids:
            return

        delete_tasks: list[Awaitable[Any]] = []
        if self.images_vdb is not None:
            delete_tasks.append(self.images_vdb.delete(normalized_ids))
        if self.image_metadata is not None:
            delete_tasks.append(self.image_metadata.delete(normalized_ids))
        if self.image_blob_store is not None:
            delete_tasks.extend(
                self.image_blob_store.delete(image_id) for image_id in normalized_ids
            )

        if delete_tasks:
            await asyncio.gather(*delete_tasks, return_exceptions=True)

        flush_tasks: list[Awaitable[Any]] = []
        if self.images_vdb is not None:
            flush_tasks.append(self.images_vdb.index_done_callback())
        if self.image_metadata is not None:
            flush_tasks.append(self.image_metadata.index_done_callback())
        if self.image_blob_store is not None:
            flush_tasks.append(self.image_blob_store.index_done_callback())
        if flush_tasks:
            await asyncio.gather(*flush_tasks, return_exceptions=True)

    async def _get_existing_image_ids_for_doc(self, doc_id: str) -> list[str]:
        """Best-effort recovery of image ids belonging to one document."""
        existing_status = await self.doc_status.get_by_id(doc_id)
        existing_meta = (
            (existing_status or {}).get("metadata", {})
            if isinstance(existing_status, dict)
            else {}
        ) or {}
        image_ids = _normalize_string_list(
            existing_meta.get("image_ids", []),
            context=f"doc {doc_id} metadata.image_ids",
        )
        if image_ids:
            if self.image_metadata is not None:
                try:
                    metadata_records = await self.image_metadata.get_by_ids(image_ids)
                    filtered_ids = [
                        image_id
                        for image_id, metadata_record in zip(
                            image_ids, metadata_records
                        )
                        if isinstance(metadata_record, dict)
                    ]
                    if filtered_ids:
                        if len(filtered_ids) != len(image_ids):
                            logger.warning(
                                f"[{self.workspace}] filtered "
                                f"{len(image_ids) - len(filtered_ids)} stale image ids "
                                f"from doc_status metadata for {doc_id}"
                            )
                        return filtered_ids
                except Exception as e:
                    logger.warning(
                        f"[{self.workspace}] failed to validate cached image ids "
                        f"for {doc_id}: {type(e).__name__}: {e}"
                    )
            return image_ids

        raw_image_metadata = getattr(self.image_metadata, "_data", None)
        if raw_image_metadata is None:
            return []

        try:
            if hasattr(raw_image_metadata, "_getvalue"):
                raw_image_metadata = raw_image_metadata._getvalue()
            metadata_items = raw_image_metadata.items()
        except Exception:
            return []

        recovered: list[tuple[int, int, str]] = []
        for blob_id, record in metadata_items:
            if not isinstance(record, dict):
                continue
            if record.get("source_doc_id") != doc_id:
                continue
            extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
            page_no = record.get("source_page", extra.get("source_page"))
            picture_index = record.get("picture_index", extra.get("picture_index"))
            try:
                page_sort = int(page_no) if page_no is not None else 10**9
            except (TypeError, ValueError):
                page_sort = 10**9
            try:
                picture_sort = (
                    int(picture_index) if picture_index is not None else 10**9
                )
            except (TypeError, ValueError):
                picture_sort = 10**9
            recovered.append((page_sort, picture_sort, str(blob_id)))

        recovered.sort()
        deduped = [blob_id for *_ignored, blob_id in recovered]
        if deduped:
            logger.info(
                f"[{self.workspace}] recovered {len(deduped)} image ids for "
                f"{doc_id} from image_metadata fallback"
            )
        return deduped

    async def areconstruct_document_multimodal_payload(
        self, doc_id: str
    ) -> tuple[str, list[dict[str, Any]]]:
        """Reconstruct multimodal inputs from already-stored assets."""
        if self.image_metadata is None or self.image_blob_store is None:
            raise RuntimeError(
                "areconstruct_document_multimodal_payload requires image storages"
            )

        full_doc = await self.full_docs.get_by_id(doc_id)
        if full_doc is None:
            raise FileNotFoundError(
                f"Cannot reconstruct multimodal payload: full_doc missing for {doc_id}"
            )
        text_content = _strip_appended_image_annotations(
            str(full_doc.get("content") or "")
        )

        image_ids = await self._get_existing_image_ids_for_doc(doc_id)
        if not image_ids:
            raise FileNotFoundError(
                f"Cannot reconstruct multimodal payload: no stored images found for {doc_id}"
            )

        metadata_records = await self.image_metadata.get_by_ids(image_ids)
        extracted_images: list[dict[str, Any]] = []
        for blob_id, metadata in zip(image_ids, metadata_records):
            record = metadata or {}
            extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
            image_bytes = await self.image_blob_store.get(blob_id)
            if not image_bytes:
                logger.warning(
                    f"[{self.workspace}] skipping missing image blob during "
                    f"reconstruct: {blob_id}"
                )
                continue

            extracted_images.append(
                {
                    "bytes": image_bytes,
                    "mime_type": record.get("content_type") or "image/png",
                    "page_no": record.get("source_page", extra.get("source_page")),
                    "source_printed_page": record.get(
                        "source_printed_page", extra.get("source_printed_page")
                    ),
                    "source_page_label": record.get(
                        "source_page_label", extra.get("source_page_label")
                    ),
                    "bbox": record.get("source_bbox", extra.get("source_bbox")),
                    "caption_hint": extra.get("caption_hint"),
                    "picture_index": record.get(
                        "picture_index", extra.get("picture_index")
                    ),
                    "page_picture_index": record.get(
                        "page_picture_index", extra.get("page_picture_index")
                    ),
                    "page_text_excerpt": extra.get("page_text_excerpt"),
                    "context_text": record.get("context_text")
                    or extra.get("context_text")
                    or extra.get("page_text_excerpt"),
                    "context_chunk_ids": record.get("context_chunk_ids")
                    or extra.get("context_chunk_ids"),
                    "context_chunks": record.get("context_chunks")
                    or extra.get("context_chunks"),
                    "extraction_mode": record.get("extraction_mode")
                    or extra.get("extraction_mode")
                    or "recovered_existing_assets",
                    "native_xref": record.get("native_xref")
                    or extra.get("native_xref"),
                    "merged_extraction_modes": record.get(
                        "merged_extraction_modes"
                    )
                    or extra.get("merged_extraction_modes"),
                }
            )

        extracted_images.sort(
            key=lambda item: (
                item.get("page_no") if item.get("page_no") is not None else 10**9,
                item.get("page_picture_index")
                if item.get("page_picture_index") is not None
                else item.get("picture_index", 0),
            )
        )
        for picture_index, item in enumerate(extracted_images):
            item["picture_index"] = picture_index

        logger.info(
            f"[{self.workspace}] reconstructed multimodal payload for {doc_id}: "
            f"text_len={len(text_content)} images={len(extracted_images)}"
        )
        return text_content, extracted_images

    async def arebuild_document_multimodal(
        self,
        doc_id: str,
        text_content: str,
        extracted_images: list[dict[str, Any]],
        *,
        file_path: str,
        track_id: str | None = None,
        reuse_existing_images: bool = True,
    ) -> str:
        """Rebuild a document's multimodal representation in place.

        The current text/KG representation is removed via ``adelete_by_doc_id``
        and then recreated from freshly extracted PDF text + images while
        preserving reusable image-side cache entries whenever possible.
        """
        if not doc_id:
            raise ValueError("arebuild_document_multimodal: doc_id must be non-empty")

        existing_status = await self.doc_status.get_by_id(doc_id)
        existing_full_doc = await self.full_docs.get_by_id(doc_id)
        if existing_status is None:
            raise ValueError(
                f"arebuild_document_multimodal: unknown document id {doc_id}"
            )

        if track_id is None:
            track_id = generate_track_id("rebuild_multimodal")

        existing_meta = existing_status.get("metadata", {}) or {}
        old_image_ids = _normalize_string_list(
            existing_meta.get("image_ids", []),
            context=f"rebuild doc {doc_id} metadata.image_ids",
        )
        new_image_ids = list(
            dict.fromkeys(
                compute_mdhash_id(img["bytes"], prefix="img-")
                for img in extracted_images
                if img.get("bytes")
            )
        )
        stale_image_ids = [img_id for img_id in old_image_ids if img_id not in new_image_ids]

        logger.info(
            f"[{self.workspace}] rebuilding multimodal doc_id={doc_id} "
            f"file_path={file_path} old_images={len(old_image_ids)} "
            f"new_images={len(new_image_ids)} reuse_cache={reuse_existing_images}"
        )

        delete_result = await self.adelete_by_doc_id(doc_id, delete_llm_cache=False)
        if delete_result.status not in {"success", "not_found"}:
            raise RuntimeError(delete_result.message)

        processing_placeholder = _build_multimodal_rebuild_status_record(
            existing_status,
            track_id=track_id,
            file_path=file_path,
            stage="indexing_multimodal_payload",
        )
        await self.doc_status.upsert({doc_id: processing_placeholder})
        await self.doc_status.index_done_callback()

        try:
            rebuild_track_id = await self.ainsert_document_with_images(
                text_content=text_content,
                extracted_images=extracted_images,
                file_path=file_path,
                ids=doc_id,
                track_id=track_id,
                reuse_existing_images=reuse_existing_images,
                allow_existing_doc_id=True,
                initial_doc_status=DocStatus.PROCESSING,
                initial_doc_metadata=processing_placeholder.get("metadata", {}),
            )
        except Exception as e:
            logger.error(
                f"[{self.workspace}] multimodal rebuild failed for {doc_id}: "
                f"{type(e).__name__}: {e}"
            )
            restore_meta = dict(existing_meta)
            restore_meta["multimodal_rebuild_failed"] = True
            restore_record = {
                **existing_status,
                "status": DocStatus.FAILED,
                "track_id": track_id,
                "chunks_count": 0,
                "chunks_list": [],
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "error_msg": f"Multimodal rebuild failed: {e}",
                "metadata": restore_meta,
            }
            try:
                await self.doc_status.upsert({doc_id: restore_record})
                if existing_full_doc is not None:
                    await self.full_docs.upsert({doc_id: existing_full_doc})
                await asyncio.gather(
                    self.doc_status.index_done_callback(),
                    self.full_docs.index_done_callback(),
                    return_exceptions=True,
                )
            except Exception as restore_error:
                logger.error(
                    f"[{self.workspace}] failed to restore rebuild failure state "
                    f"for {doc_id}: {type(restore_error).__name__}: {restore_error}"
                )
            raise

        if stale_image_ids:
            try:
                await self._delete_image_side_resources(stale_image_ids)
            except Exception as cleanup_error:
                logger.warning(
                    f"[{self.workspace}] stale multimodal image cleanup failed for "
                    f"{doc_id}: {type(cleanup_error).__name__}: {cleanup_error}"
                )

        return rebuild_track_id

    # TODO: deprecated, use insert instead
    def insert_custom_chunks(
        self,
        full_text: str,
        text_chunks: list[str],
        doc_id: str | list[str] | None = None,
    ) -> None:
        loop = always_get_an_event_loop()
        loop.run_until_complete(
            self.ainsert_custom_chunks(full_text, text_chunks, doc_id)
        )

    # TODO: deprecated, use ainsert instead
    async def ainsert_custom_chunks(
        self, full_text: str, text_chunks: list[str], doc_id: str | None = None
    ) -> None:
        update_storage = False
        try:
            # Clean input texts
            full_text = sanitize_text_for_encoding(full_text)
            text_chunks = [sanitize_text_for_encoding(chunk) for chunk in text_chunks]
            file_path = ""

            # Process cleaned texts
            if doc_id is None:
                doc_key = compute_mdhash_id(full_text, prefix="doc-")
            else:
                doc_key = doc_id
            new_docs = {doc_key: {"content": full_text, "file_path": file_path}}

            _add_doc_keys = await self.full_docs.filter_keys({doc_key})
            new_docs = {k: v for k, v in new_docs.items() if k in _add_doc_keys}
            if not len(new_docs):
                logger.warning("This document is already in the storage.")
                return

            update_storage = True
            logger.info(f"Inserting {len(new_docs)} docs")

            inserting_chunks: dict[str, Any] = {}
            for index, chunk_text in enumerate(text_chunks):
                chunk_key = compute_mdhash_id(chunk_text, prefix="chunk-")
                tokens = len(self.tokenizer.encode(chunk_text))
                inserting_chunks[chunk_key] = {
                    "content": chunk_text,
                    "full_doc_id": doc_key,
                    "tokens": tokens,
                    "chunk_order_index": index,
                    "file_path": file_path,
                }

            doc_ids = set(inserting_chunks.keys())
            add_chunk_keys = await self.text_chunks.filter_keys(doc_ids)
            inserting_chunks = {
                k: v for k, v in inserting_chunks.items() if k in add_chunk_keys
            }
            if not len(inserting_chunks):
                logger.warning("All chunks are already in the storage.")
                return

            tasks = [
                self.chunks_vdb.upsert(inserting_chunks),
                self._process_extract_entities(inserting_chunks),
                self.full_docs.upsert(new_docs),
                self.text_chunks.upsert(inserting_chunks),
            ]
            await asyncio.gather(*tasks)

        finally:
            if update_storage:
                await self._insert_done()

    async def apipeline_enqueue_documents(
        self,
        input: str | list[str],
        ids: list[str] | None = None,
        file_paths: str | list[str] | None = None,
        track_id: str | None = None,
        allow_existing_ids: bool = False,
        initial_status: DocStatus = DocStatus.PENDING,
        initial_metadata: dict[str, Any] | None = None,
    ) -> str:
        """
        Pipeline for Processing Documents

        1. Validate ids if provided or generate MD5 hash IDs and remove duplicate contents
        2. Generate document initial status
        3. Filter out already processed documents
        4. Enqueue document in status

        Args:
            input: Single document string or list of document strings
            ids: list of unique document IDs, if not provided, MD5 hash IDs will be generated
            file_paths: list of file paths corresponding to each document, used for citation
            track_id: tracking ID for monitoring processing status, if not provided, will be generated with "enqueue" prefix
            allow_existing_ids: when True, skip duplicate-ID filtering and
                overwrite existing doc/full_doc rows for the provided ids.
                Intended for controlled in-place rebuild flows.
            initial_status: initial status written into doc_status rows.
            initial_metadata: optional metadata merged into each initial
                doc_status record.

        Returns:
            str: tracking ID for monitoring processing status
        """
        # Generate track_id if not provided
        if track_id is None or track_id.strip() == "":
            track_id = generate_track_id("enqueue")
        if isinstance(input, str):
            input = [input]
        if isinstance(ids, str):
            ids = [ids]
        if isinstance(file_paths, str):
            file_paths = [file_paths]

        # If file_paths is provided, ensure it matches the number of documents
        if file_paths is not None:
            if isinstance(file_paths, str):
                file_paths = [file_paths]
            if len(file_paths) != len(input):
                raise ValueError(
                    "Number of file paths must match the number of documents"
                )
            file_paths = [
                path.strip() if isinstance(path, str) else "" for path in file_paths
            ]
            file_paths = [path if path else "unknown_source" for path in file_paths]
        else:
            # If no file paths provided, use placeholder
            file_paths = ["unknown_source"] * len(input)

        # 1. Validate ids if provided or generate MD5 hash IDs and remove duplicate contents
        if ids is not None:
            # Check if the number of IDs matches the number of documents
            if len(ids) != len(input):
                raise ValueError("Number of IDs must match the number of documents")

            # Check if IDs are unique
            if len(ids) != len(set(ids)):
                raise ValueError("IDs must be unique")

            # Generate contents dict and remove duplicates in one pass
            unique_contents = {}
            for id_, doc, path in zip(ids, input, file_paths):
                cleaned_content = sanitize_text_for_encoding(doc)
                if cleaned_content not in unique_contents:
                    unique_contents[cleaned_content] = (id_, path)

            # Reconstruct contents with unique content
            contents = {
                id_: {"content": content, "file_path": file_path}
                for content, (id_, file_path) in unique_contents.items()
            }
        else:
            # Clean input text and remove duplicates in one pass
            unique_content_with_paths = {}
            for doc, path in zip(input, file_paths):
                cleaned_content = sanitize_text_for_encoding(doc)
                if cleaned_content not in unique_content_with_paths:
                    unique_content_with_paths[cleaned_content] = path

            # Generate contents dict of MD5 hash IDs and documents with paths
            contents = {
                compute_mdhash_id(content, prefix="doc-"): {
                    "content": content,
                    "file_path": path,
                }
                for content, path in unique_content_with_paths.items()
            }

        # 2. Generate document initial status (without content)
        new_docs: dict[str, Any] = {
            id_: {
                "status": initial_status,
                "content_summary": get_content_summary(content_data["content"]),
                "content_length": len(content_data["content"]),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "file_path": content_data[
                    "file_path"
                ],  # Store file path in document status
                "track_id": track_id,  # Store track_id in document status
                "metadata": dict(initial_metadata or {}),
            }
            for id_, content_data in contents.items()
        }

        # 3. Filter out already processed documents
        # Get docs ids
        all_new_doc_ids = set(new_docs.keys())
        # Exclude IDs of documents that are already enqueued
        if allow_existing_ids:
            unique_new_doc_ids = all_new_doc_ids
        else:
            unique_new_doc_ids = await self.doc_status.filter_keys(all_new_doc_ids)

            # Handle duplicate documents - create trackable records with current track_id
            ignored_ids = list(all_new_doc_ids - unique_new_doc_ids)
            if ignored_ids:
                duplicate_docs: dict[str, Any] = {}
                for doc_id in ignored_ids:
                    file_path = (
                        new_docs.get(doc_id, {}).get("file_path") or "unknown_source"
                    )
                    logger.warning(f"Duplicate document detected: {doc_id} ({file_path})")

                    # Get existing document info for reference
                    existing_doc = await self.doc_status.get_by_id(doc_id)
                    existing_status = (
                        existing_doc.get("status", "unknown")
                        if existing_doc
                        else "unknown"
                    )
                    existing_track_id = (
                        existing_doc.get("track_id", "") if existing_doc else ""
                    )

                    # Create a new record with unique ID for this duplicate attempt
                    dup_record_id = compute_mdhash_id(
                        f"{doc_id}-{track_id}", prefix="dup-"
                    )
                    duplicate_docs[dup_record_id] = {
                        "status": DocStatus.FAILED,
                        "content_summary": f"[DUPLICATE] Original document: {doc_id}",
                        "content_length": new_docs.get(doc_id, {}).get(
                            "content_length", 0
                        ),
                        "chunks_count": 0,
                        "chunks_list": [],
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "file_path": file_path,
                        "track_id": track_id,  # Use current track_id for tracking
                        "error_msg": (
                            f"Content already exists. Original doc_id: {doc_id}, "
                            f"Status: {existing_status}"
                        ),
                        "metadata": {
                            "is_duplicate": True,
                            "original_doc_id": doc_id,
                            "original_track_id": existing_track_id,
                        },
                    }

                # Store duplicate records in doc_status
                if duplicate_docs:
                    await self.doc_status.upsert(duplicate_docs)
                    logger.info(
                        f"Created {len(duplicate_docs)} duplicate document records with track_id: {track_id}"
                    )

        # Filter new_docs to only include documents with unique IDs
        new_docs = {
            doc_id: new_docs[doc_id]
            for doc_id in unique_new_doc_ids
            if doc_id in new_docs
        }

        if not new_docs:
            logger.warning("No new unique documents were found.")
            return

        # 4. Store document content in full_docs and status in doc_status
        #    Store full document content separately
        full_docs_data = {
            doc_id: {
                "content": contents[doc_id]["content"],
                "file_path": contents[doc_id]["file_path"],
            }
            for doc_id in new_docs.keys()
        }
        await self.full_docs.upsert(full_docs_data)
        # Persist data to disk immediately
        await self.full_docs.index_done_callback()

        # Store document status (without content)
        await self.doc_status.upsert(new_docs)
        logger.debug(f"Stored {len(new_docs)} new unique documents")

        return track_id

    async def apipeline_enqueue_error_documents(
        self,
        error_files: list[dict[str, Any]],
        track_id: str | None = None,
    ) -> None:
        """
        Record file extraction errors in doc_status storage.

        This function creates error document entries in the doc_status storage for files
        that failed during the extraction process. Each error entry contains information
        about the failure to help with debugging and monitoring.

        Args:
            error_files: List of dictionaries containing error information for each failed file.
                Each dictionary should contain:
                - file_path: Original file name/path
                - error_description: Brief error description (for content_summary)
                - original_error: Full error message (for error_msg)
                - file_size: File size in bytes (for content_length, 0 if unknown)
            track_id: Optional tracking ID for grouping related operations

        Returns:
            None
        """
        if not error_files:
            logger.debug("No error files to record")
            return

        # Generate track_id if not provided
        if track_id is None or track_id.strip() == "":
            track_id = generate_track_id("error")

        error_docs: dict[str, Any] = {}
        current_time = datetime.now(timezone.utc).isoformat()

        for error_file in error_files:
            file_path = error_file.get("file_path", "unknown_file")
            error_description = error_file.get(
                "error_description", "File extraction failed"
            )
            original_error = error_file.get("original_error", "Unknown error")
            file_size = error_file.get("file_size", 0)

            # Generate unique doc_id with "error-" prefix
            doc_id_content = f"{file_path}-{error_description}"
            doc_id = compute_mdhash_id(doc_id_content, prefix="error-")

            error_docs[doc_id] = {
                "status": DocStatus.FAILED,
                "content_summary": error_description,
                "content_length": file_size,
                "error_msg": original_error,
                "chunks_count": 0,  # No chunks for failed files
                "chunks_list": [],
                "created_at": current_time,
                "updated_at": current_time,
                "file_path": file_path,
                "track_id": track_id,
                "metadata": {
                    "error_type": "file_extraction_error",
                },
            }

        # Store error documents in doc_status
        if error_docs:
            await self.doc_status.upsert(error_docs)
            # Log each error for debugging
            for doc_id, error_doc in error_docs.items():
                logger.error(
                    f"File processing error: - ID: {doc_id} {error_doc['file_path']}"
                )

    async def _validate_and_fix_document_consistency(
        self,
        to_process_docs: dict[str, DocProcessingStatus],
        pipeline_status: dict,
        pipeline_status_lock: asyncio.Lock,
    ) -> dict[str, DocProcessingStatus]:
        """Validate and fix document data consistency by deleting inconsistent entries, but preserve failed documents"""
        inconsistent_docs = []
        failed_docs_to_preserve = []
        successful_deletions = 0

        # Check each document's data consistency
        for doc_id, status_doc in to_process_docs.items():
            # Check if corresponding content exists in full_docs
            content_data = await self.full_docs.get_by_id(doc_id)
            if not content_data:
                # Check if this is a failed document that should be preserved
                if (
                    hasattr(status_doc, "status")
                    and status_doc.status == DocStatus.FAILED
                ):
                    failed_docs_to_preserve.append(doc_id)
                else:
                    inconsistent_docs.append(doc_id)

        # Log information about failed documents that will be preserved
        if failed_docs_to_preserve:
            async with pipeline_status_lock:
                preserve_message = f"Preserving {len(failed_docs_to_preserve)} failed document entries for manual review"
                logger.info(preserve_message)
                pipeline_status["latest_message"] = preserve_message
                pipeline_status["history_messages"].append(preserve_message)

            # Remove failed documents from processing list but keep them in doc_status
            for doc_id in failed_docs_to_preserve:
                to_process_docs.pop(doc_id, None)

        # Delete inconsistent document entries(excluding failed documents)
        if inconsistent_docs:
            async with pipeline_status_lock:
                summary_message = (
                    f"Inconsistent document entries found: {len(inconsistent_docs)}"
                )
                logger.info(summary_message)
                pipeline_status["latest_message"] = summary_message
                pipeline_status["history_messages"].append(summary_message)

            successful_deletions = 0
            for doc_id in inconsistent_docs:
                try:
                    status_doc = to_process_docs[doc_id]
                    file_path = _resolve_doc_file_path(status_doc=status_doc)

                    # Delete doc_status entry
                    await self.doc_status.delete([doc_id])
                    successful_deletions += 1

                    # Log successful deletion
                    async with pipeline_status_lock:
                        log_message = (
                            f"Deleted inconsistent entry: {doc_id} ({file_path})"
                        )
                        logger.info(log_message)
                        pipeline_status["latest_message"] = log_message
                        pipeline_status["history_messages"].append(log_message)

                    # Remove from processing list
                    to_process_docs.pop(doc_id, None)

                except Exception as e:
                    # Log deletion failure
                    async with pipeline_status_lock:
                        error_message = f"Failed to delete entry: {doc_id} - {str(e)}"
                        logger.error(error_message)
                        pipeline_status["latest_message"] = error_message
                        pipeline_status["history_messages"].append(error_message)

        # Final summary log
        # async with pipeline_status_lock:
        #     final_message = f"Successfully deleted {successful_deletions} inconsistent entries, preserved {len(failed_docs_to_preserve)} failed documents"
        #     logger.info(final_message)
        #     pipeline_status["latest_message"] = final_message
        #     pipeline_status["history_messages"].append(final_message)

        # Reset PROCESSING and FAILED documents that pass consistency checks to PENDING status
        docs_to_reset = {}
        reset_count = 0

        for doc_id, status_doc in to_process_docs.items():
            # Check if document has corresponding content in full_docs (consistency check)
            content_data = await self.full_docs.get_by_id(doc_id)
            if content_data:  # Document passes consistency check
                # Check if document is in PROCESSING or FAILED status
                if hasattr(status_doc, "status") and status_doc.status in [
                    DocStatus.PROCESSING,
                    DocStatus.FAILED,
                ]:
                    preserved_chunks_list, preserved_chunks_count = (
                        _chunk_fields_from_status_doc(status_doc)
                    )
                    resolved_file_path = _resolve_doc_file_path(
                        status_doc=status_doc,
                        content_data=content_data,
                    )
                    # Prepare document for status reset to PENDING
                    docs_to_reset[doc_id] = {
                        "status": DocStatus.PENDING,
                        "content_summary": status_doc.content_summary,
                        "content_length": status_doc.content_length,
                        "chunks_count": preserved_chunks_count,
                        "chunks_list": preserved_chunks_list,
                        "created_at": status_doc.created_at,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "file_path": resolved_file_path,
                        "track_id": getattr(status_doc, "track_id", ""),
                        # Clear any error messages and processing metadata
                        "error_msg": "",
                        "metadata": {},
                    }

                    # Update the status in to_process_docs as well
                    status_doc.status = DocStatus.PENDING
                    status_doc.file_path = resolved_file_path
                    reset_count += 1

        # Update doc_status storage if there are documents to reset
        if docs_to_reset:
            await self.doc_status.upsert(docs_to_reset)

            async with pipeline_status_lock:
                reset_message = f"Reset {reset_count} documents from PROCESSING/FAILED to PENDING status"
                logger.info(reset_message)
                pipeline_status["latest_message"] = reset_message
                pipeline_status["history_messages"].append(reset_message)

        return to_process_docs

    async def apipeline_process_enqueue_documents(
        self,
        split_by_character: str | None = None,
        split_by_character_only: bool = False,
    ) -> None:
        """
        Process pending documents by splitting them into chunks, processing
        each chunk for entity and relation extraction, and updating the
        document status.

        1. Get all pending, failed, and abnormally terminated processing documents.
        2. Validate document data consistency and fix any issues
        3. Split document content into chunks
        4. Process each chunk for entity and relation extraction
        5. Update the document status
        """

        # Get pipeline status shared data and lock
        pipeline_status = await get_namespace_data(
            "pipeline_status", workspace=self.workspace
        )
        pipeline_status_lock = get_namespace_lock(
            "pipeline_status", workspace=self.workspace
        )

        # Check if another process is already processing the queue
        async with pipeline_status_lock:
            # Ensure only one worker is processing documents
            if not pipeline_status.get("busy", False):
                to_process_docs: dict[
                    str, DocProcessingStatus
                ] = await self.doc_status.get_docs_by_statuses(
                    [DocStatus.PROCESSING, DocStatus.FAILED, DocStatus.PENDING]
                )

                if not to_process_docs:
                    logger.info("No documents to process")
                    return

                pipeline_status.update(
                    {
                        "busy": True,
                        "job_name": "Default Job",
                        "job_start": datetime.now(timezone.utc).isoformat(),
                        "docs": 0,
                        "batchs": 0,  # Total number of files to be processed
                        "cur_batch": 0,  # Number of files already processed
                        "total_chunks": 0,
                        "processed_chunks": 0,
                        "current_stage": "",
                        "current_stage_label": "",
                        "stage_unit": "",
                        "stage_total": 0,
                        "stage_processed": 0,
                        "stage_remaining": 0,
                        "stage_elapsed_seconds": 0,
                        "stage_eta_seconds": None,
                        "request_pending": False,  # Clear any previous request
                        "cancellation_requested": False,  # Initialize cancellation flag
                        "latest_message": "",
                    }
                )
                # Cleaning history_messages without breaking it as a shared list object
                del pipeline_status["history_messages"][:]
            else:
                # Another process is busy, just set request flag and return
                pipeline_status["request_pending"] = True
                logger.info(
                    "Another process is already processing the document queue. Request queued."
                )
                return

        try:
            # Process documents until no more documents or requests
            while True:
                # Check for cancellation request at the start of main loop
                async with pipeline_status_lock:
                    if pipeline_status.get("cancellation_requested", False):
                        # Clear pending request
                        pipeline_status["request_pending"] = False
                        # Celar cancellation flag
                        pipeline_status["cancellation_requested"] = False

                        log_message = "Pipeline cancelled by user"
                        logger.info(log_message)
                        pipeline_status["latest_message"] = log_message
                        pipeline_status["history_messages"].append(log_message)

                        # Exit directly, skipping request_pending check
                        return

                if not to_process_docs:
                    log_message = "All enqueued documents have been processed"
                    logger.info(log_message)
                    pipeline_status["latest_message"] = log_message
                    pipeline_status["history_messages"].append(log_message)
                    break

                # Validate document data consistency and fix any issues as part of the pipeline
                to_process_docs = await self._validate_and_fix_document_consistency(
                    to_process_docs, pipeline_status, pipeline_status_lock
                )

                if not to_process_docs:
                    log_message = (
                        "No valid documents to process after consistency check"
                    )
                    logger.info(log_message)
                    pipeline_status["latest_message"] = log_message
                    pipeline_status["history_messages"].append(log_message)
                    break

                log_message = f"Processing {len(to_process_docs)} document(s)"
                logger.info(log_message)

                # Update pipeline_status, batchs now represents the total number of files to be processed
                pipeline_status["docs"] = len(to_process_docs)
                pipeline_status["batchs"] = len(to_process_docs)
                pipeline_status["cur_batch"] = 0
                pipeline_status["total_chunks"] = 0
                pipeline_status["processed_chunks"] = 0
                pipeline_status["current_stage"] = "document_queue"
                pipeline_status["current_stage_label"] = "Preparing document queue"
                pipeline_status["stage_unit"] = "documents"
                pipeline_status["stage_total"] = len(to_process_docs)
                pipeline_status["stage_processed"] = 0
                pipeline_status["stage_remaining"] = len(to_process_docs)
                pipeline_status["stage_elapsed_seconds"] = 0
                pipeline_status["stage_eta_seconds"] = None
                pipeline_status["latest_message"] = log_message
                pipeline_status["history_messages"].append(log_message)

                # Get first document's file path and total count for job name
                first_doc_id, first_doc = next(iter(to_process_docs.items()))
                first_doc_path = first_doc.file_path

                # Handle cases where first_doc_path is None
                if first_doc_path:
                    path_prefix = first_doc_path[:20] + (
                        "..." if len(first_doc_path) > 20 else ""
                    )
                else:
                    path_prefix = "unknown_source"

                total_files = len(to_process_docs)
                job_name = f"{path_prefix}[{total_files} files]"
                pipeline_status["job_name"] = job_name

                # Create a counter to track the number of processed files
                processed_count = 0
                # Create a semaphore to limit the number of concurrent file processing
                semaphore = asyncio.Semaphore(self.max_parallel_insert)

                async def process_document(
                    doc_id: str,
                    status_doc: DocProcessingStatus,
                    split_by_character: str | None,
                    split_by_character_only: bool,
                    pipeline_status: dict,
                    pipeline_status_lock: asyncio.Lock,
                    semaphore: asyncio.Semaphore,
                ) -> None:
                    """Process single document"""
                    # Initialize variables at the start to prevent UnboundLocalError in error handling
                    file_path = _resolve_doc_file_path(status_doc=status_doc)
                    current_file_number = 0
                    file_extraction_stage_ok = False
                    processing_start_time = int(time.time())
                    first_stage_tasks = []
                    entity_relation_task = None
                    chunks: dict[str, Any] = {}
                    content_data: dict[str, Any] | None = None

                    def get_failed_chunk_snapshot() -> tuple[list[str], int]:
                        if chunks:
                            chunk_ids = list(chunks.keys())
                            return chunk_ids, len(chunk_ids)
                        return _chunk_fields_from_status_doc(status_doc)

                    async with semaphore:
                        nonlocal processed_count
                        # Initialize to prevent UnboundLocalError in error handling
                        first_stage_tasks = []
                        entity_relation_task = None
                        try:
                            # Resolve file_path from full_docs before honoring a queued
                            # cancellation so corrupted doc_status placeholders do not
                            # get written back again during retry/cancel flows.
                            content_data = await self.full_docs.get_by_id(doc_id)
                            if content_data:
                                file_path = _resolve_doc_file_path(
                                    status_doc=status_doc,
                                    content_data=content_data,
                                )
                                status_doc.file_path = file_path

                            # Check for cancellation before starting document processing.
                            # file_path is resolved before this check so queued documents
                            # do not lose their source path on early cancellation.
                            async with pipeline_status_lock:
                                if pipeline_status.get("cancellation_requested", False):
                                    raise PipelineCancelledException("User cancelled")

                            async with pipeline_status_lock:
                                # Update processed file count and save current file number
                                processed_count += 1
                                current_file_number = (
                                    processed_count  # Save the current file number
                                )
                                pipeline_status["cur_batch"] = processed_count

                                log_message = f"Extracting stage {current_file_number}/{total_files}: {file_path}"
                                logger.info(log_message)
                                pipeline_status["history_messages"].append(log_message)
                                log_message = f"Processing d-id: {doc_id}"
                                logger.info(log_message)
                                pipeline_status["latest_message"] = log_message
                                pipeline_status["history_messages"].append(log_message)

                                # Prevent memory growth: keep only latest 5000 messages when exceeding 10000
                                if len(pipeline_status["history_messages"]) > 10000:
                                    logger.info(
                                        f"Trimming pipeline history from {len(pipeline_status['history_messages'])} to 5000 messages"
                                    )
                                    # Trim in place so Manager.list-backed shared state
                                    # remains appendable and visible across processes.
                                    del pipeline_status["history_messages"][:-5000]

                            # Get document content from full_docs
                            if not content_data:
                                raise Exception(
                                    f"Document content not found in full_docs for doc_id: {doc_id}"
                                )
                            content = content_data["content"]

                            # Fire the LLM-backed document summary task in
                            # parallel with chunking. It runs through the
                            # LLM cache so duplicate content never
                            # re-invokes the provider, and we await it
                            # just before the final PROCESSED upsert
                            # (with a short timeout) so a slow LLM call
                            # falls back to the truncated prefix rather
                            # than blocking the pipeline.
                            doc_summary_task = asyncio.create_task(
                                self._agenerate_document_summary(content)
                            )

                            # Call chunking function, supporting both sync and async implementations
                            chunking_result = self.chunking_func(
                                self.tokenizer,
                                content,
                                split_by_character,
                                split_by_character_only,
                                self.chunk_overlap_token_size,
                                self.chunk_token_size,
                            )

                            # If result is awaitable, await to get actual result
                            if inspect.isawaitable(chunking_result):
                                chunking_result = await chunking_result

                            # Validate return type
                            if not isinstance(chunking_result, (list, tuple)):
                                raise TypeError(
                                    f"chunking_func must return a list or tuple of dicts, "
                                    f"got {type(chunking_result)}"
                                )

                            # Build chunks dictionary
                            chunks: dict[str, Any] = {
                                compute_mdhash_id(dp["content"], prefix="chunk-"): {
                                    **dp,
                                    "full_doc_id": doc_id,
                                    "file_path": file_path,  # Add file path to each chunk
                                    "llm_cache_list": [],  # Initialize empty LLM cache list for each chunk
                                }
                                for dp in chunking_result
                            }

                            if not chunks:
                                logger.warning("No document chunks to process")

                            # Record processing start time
                            processing_start_time = int(time.time())

                            async with pipeline_status_lock:
                                total_chunks_known = int(
                                    pipeline_status.get("total_chunks", 0) or 0
                                )
                                pipeline_status["total_chunks"] = (
                                    total_chunks_known + len(chunks)
                                )
                                pipeline_status["current_stage"] = "document_chunking"
                                pipeline_status["current_stage_label"] = (
                                    "Chunking documents"
                                )
                                pipeline_status["stage_unit"] = "chunks"
                                pipeline_status["stage_total"] = int(
                                    pipeline_status["total_chunks"]
                                )
                                pipeline_status["stage_processed"] = int(
                                    pipeline_status.get("processed_chunks", 0) or 0
                                )
                                pipeline_status["stage_remaining"] = max(
                                    int(pipeline_status["stage_total"])
                                    - int(pipeline_status["stage_processed"]),
                                    0,
                                )
                                pipeline_status["stage_elapsed_seconds"] = 0
                                pipeline_status["stage_eta_seconds"] = None

                            # Check for cancellation before entity extraction
                            async with pipeline_status_lock:
                                if pipeline_status.get("cancellation_requested", False):
                                    raise PipelineCancelledException("User cancelled")

                            # Per-doc cancel check (see _check_doc_cancelled).
                            # Operators can flip metadata.cancel_requested via the
                            # POST /documents/{id}/cancel endpoint while this doc
                            # is in flight; catching it here terminates only this
                            # task and leaves every sibling doc untouched.
                            if await self._check_doc_cancelled(doc_id):
                                raise DocumentCancelledException(doc_id)

                            # Process document in two stages
                            # Stage 1: Process text chunks and docs (parallel execution)
                            doc_status_task = asyncio.create_task(
                                self.doc_status.upsert(
                                    {
                                        doc_id: {
                                            "status": DocStatus.PROCESSING,
                                            "chunks_count": len(chunks),
                                            "chunks_list": list(
                                                chunks.keys()
                                            ),  # Save chunks list
                                            "content_summary": status_doc.content_summary,
                                            "content_length": status_doc.content_length,
                                            "created_at": status_doc.created_at,
                                            "updated_at": datetime.now(
                                                timezone.utc
                                            ).isoformat(),
                                            "file_path": file_path,
                                            "track_id": status_doc.track_id,  # Preserve existing track_id
                                            "metadata": {
                                                "processing_start_time": processing_start_time,
                                                "processing_stage": "entity_extraction_prepare",
                                                "processing_stage_label": "Preparing entity extraction",
                                                "processing_progress_percent": 0.0,
                                                "processing_items_processed": 0,
                                                "processing_items_total": len(chunks),
                                                "processing_items_remaining": len(chunks),
                                                "processing_elapsed_seconds": 0,
                                                "processing_eta_seconds": None,
                                            },
                                        }
                                    }
                                )
                            )
                            chunks_vdb_task = asyncio.create_task(
                                self.chunks_vdb.upsert(chunks)
                            )
                            text_chunks_task = asyncio.create_task(
                                self.text_chunks.upsert(chunks)
                            )

                            # First stage tasks (parallel execution)
                            first_stage_tasks = [
                                doc_status_task,
                                chunks_vdb_task,
                                text_chunks_task,
                            ]
                            entity_relation_task = None

                            # Execute first stage tasks
                            await asyncio.gather(*first_stage_tasks)

                            # Stage 2: Process entity relation graph (after text_chunks are saved)
                            entity_relation_task = asyncio.create_task(
                                self._process_extract_entities(
                                    chunks, pipeline_status, pipeline_status_lock
                                )
                            )
                            chunk_results = await entity_relation_task
                            file_extraction_stage_ok = True

                        except Exception as e:
                            # Check if this is a user cancellation
                            if isinstance(e, PipelineCancelledException):
                                # User cancellation - log brief message only, no traceback
                                error_msg = f"User cancelled {current_file_number}/{total_files}: {file_path}"
                                logger.warning(error_msg)
                                async with pipeline_status_lock:
                                    pipeline_status["latest_message"] = error_msg
                                    pipeline_status["history_messages"].append(
                                        error_msg
                                    )
                            else:
                                # Other exceptions - log with traceback
                                logger.error(traceback.format_exc())
                                error_msg = f"Failed to extract document {current_file_number}/{total_files}: {file_path}"
                                logger.error(error_msg)
                                async with pipeline_status_lock:
                                    pipeline_status["latest_message"] = error_msg
                                    pipeline_status["history_messages"].append(
                                        traceback.format_exc()
                                    )
                                    pipeline_status["history_messages"].append(
                                        error_msg
                                    )

                            # Cancel tasks that are not yet completed
                            all_tasks = first_stage_tasks + (
                                [entity_relation_task] if entity_relation_task else []
                            )
                            for task in all_tasks:
                                if task and not task.done():
                                    task.cancel()

                            # Persistent llm cache with error handling
                            if self.llm_response_cache:
                                try:
                                    await self.llm_response_cache.index_done_callback()
                                except Exception as persist_error:
                                    logger.error(
                                        f"Failed to persist LLM cache: {persist_error}"
                                    )

                            # Record processing end time for failed case
                            processing_end_time = int(time.time())
                            failed_chunks_list, failed_chunks_count = (
                                get_failed_chunk_snapshot()
                            )

                            # Update document status to failed
                            await self.doc_status.upsert(
                                {
                                    doc_id: {
                                        "status": DocStatus.FAILED,
                                        "error_msg": str(e),
                                        "chunks_count": failed_chunks_count,
                                        "chunks_list": failed_chunks_list,
                                        "content_summary": status_doc.content_summary,
                                        "content_length": status_doc.content_length,
                                        "created_at": status_doc.created_at,
                                        "updated_at": datetime.now(
                                            timezone.utc
                                        ).isoformat(),
                                        "file_path": file_path,
                                        "track_id": status_doc.track_id,  # Preserve existing track_id
                                        "metadata": {
                                            "processing_start_time": processing_start_time,
                                            "processing_end_time": processing_end_time,
                                        },
                                    }
                                }
                            )

                        # Concurrency is controlled by keyed lock for individual entities and relationships
                        if file_extraction_stage_ok:
                            try:
                                # Check for cancellation before merge
                                async with pipeline_status_lock:
                                    if pipeline_status.get(
                                        "cancellation_requested", False
                                    ):
                                        raise PipelineCancelledException(
                                            "User cancelled"
                                        )

                                # Per-doc cancel check before the graph-merge
                                # stage. Matches the check before entity
                                # extraction so a user cancel lands within one
                                # LLM round trip either way.
                                if await self._check_doc_cancelled(doc_id):
                                    raise DocumentCancelledException(doc_id)

                                # Use chunk_results from entity_relation_task
                                await merge_nodes_and_edges(
                                    chunk_results=chunk_results,  # result collected from entity_relation_task
                                    knowledge_graph_inst=self.chunk_entity_relation_graph,
                                    entity_vdb=self.entities_vdb,
                                    relationships_vdb=self.relationships_vdb,
                                    global_config=asdict(self),
                                    full_entities_storage=self.full_entities,
                                    full_relations_storage=self.full_relations,
                                    doc_id=doc_id,
                                    pipeline_status=pipeline_status,
                                    pipeline_status_lock=pipeline_status_lock,
                                    llm_response_cache=self.llm_response_cache,
                                    entity_chunks_storage=self.entity_chunks,
                                    relation_chunks_storage=self.relation_chunks,
                                    doc_status_storage=self.doc_status,
                                    current_file_number=current_file_number,
                                    total_files=total_files,
                                    file_path=file_path,
                                )

                                # Record processing end time
                                processing_end_time = int(time.time())

                                # Wait up to 10s for the LLM-backed
                                # document summary to arrive. The task
                                # was kicked off at the very start of
                                # process_document, so by the time we
                                # land here (post-extraction + merge)
                                # it's usually already resolved. If it
                                # isn't — slow LLM, transient error —
                                # fall back to the truncated prefix so
                                # the PROCESSED row always has a sane
                                # summary.
                                resolved_summary = status_doc.content_summary
                                try:
                                    llm_summary = await asyncio.wait_for(
                                        doc_summary_task, timeout=10
                                    )
                                    if llm_summary:
                                        resolved_summary = llm_summary
                                except (asyncio.TimeoutError, Exception):
                                    # Keep the prefix fallback. Any
                                    # actual LLM-call error was already
                                    # logged inside _agenerate_document_summary.
                                    if not doc_summary_task.done():
                                        doc_summary_task.cancel()

                                await self.doc_status.upsert(
                                    {
                                        doc_id: {
                                            "status": DocStatus.PROCESSED,
                                            "chunks_count": len(chunks),
                                            "chunks_list": list(chunks.keys()),
                                            "content_summary": resolved_summary,
                                            "content_length": status_doc.content_length,
                                            "created_at": status_doc.created_at,
                                            "updated_at": datetime.now(
                                                timezone.utc
                                            ).isoformat(),
                                            "file_path": file_path,
                                            "track_id": status_doc.track_id,  # Preserve existing track_id
                                            "metadata": {
                                                "processing_start_time": processing_start_time,
                                                "processing_end_time": processing_end_time,
                                            },
                                        }
                                    }
                                )

                                # Call _insert_done after processing each file
                                await self._insert_done()

                                async with pipeline_status_lock:
                                    log_message = f"Completed processing file {current_file_number}/{total_files}: {file_path}"
                                    logger.info(log_message)
                                    pipeline_status["latest_message"] = log_message
                                    pipeline_status["history_messages"].append(
                                        log_message
                                    )

                            except Exception as e:
                                # Check if this is a user cancellation
                                if isinstance(e, PipelineCancelledException):
                                    # User cancellation - log brief message only, no traceback
                                    error_msg = f"User cancelled during merge {current_file_number}/{total_files}: {file_path}"
                                    logger.warning(error_msg)
                                    async with pipeline_status_lock:
                                        pipeline_status["latest_message"] = error_msg
                                        pipeline_status["history_messages"].append(
                                            error_msg
                                        )
                                else:
                                    # Other exceptions - log with traceback
                                    logger.error(traceback.format_exc())
                                    error_msg = f"Merging stage failed in document {current_file_number}/{total_files}: {file_path}"
                                    logger.error(error_msg)
                                    async with pipeline_status_lock:
                                        pipeline_status["latest_message"] = error_msg
                                        pipeline_status["history_messages"].append(
                                            traceback.format_exc()
                                        )
                                        pipeline_status["history_messages"].append(
                                            error_msg
                                        )

                                # Persistent llm cache with error handling
                                if self.llm_response_cache:
                                    try:
                                        await self.llm_response_cache.index_done_callback()
                                    except Exception as persist_error:
                                        logger.error(
                                            f"Failed to persist LLM cache: {persist_error}"
                                        )

                                # Record processing end time for failed case
                                processing_end_time = int(time.time())
                                failed_chunks_list, failed_chunks_count = (
                                    get_failed_chunk_snapshot()
                                )

                                # Update document status to failed
                                await self.doc_status.upsert(
                                    {
                                        doc_id: {
                                            "status": DocStatus.FAILED,
                                            "error_msg": str(e),
                                            "chunks_count": failed_chunks_count,
                                            "chunks_list": failed_chunks_list,
                                            "content_summary": status_doc.content_summary,
                                            "content_length": status_doc.content_length,
                                            "created_at": status_doc.created_at,
                                            "updated_at": datetime.now(
                                                timezone.utc
                                            ).isoformat(),
                                            "file_path": file_path,
                                            "track_id": status_doc.track_id,  # Preserve existing track_id
                                            "metadata": {
                                                "processing_start_time": processing_start_time,
                                                "processing_end_time": processing_end_time,
                                            },
                                        }
                                    }
                                )

                # Create processing tasks for all documents
                doc_tasks = []
                for doc_id, status_doc in to_process_docs.items():
                    doc_tasks.append(
                        process_document(
                            doc_id,
                            status_doc,
                            split_by_character,
                            split_by_character_only,
                            pipeline_status,
                            pipeline_status_lock,
                            semaphore,
                        )
                    )

                # Wait for all document processing to complete
                try:
                    await asyncio.gather(*doc_tasks)
                except PipelineCancelledException:
                    # Cancel all remaining tasks
                    for task in doc_tasks:
                        if not task.done():
                            task.cancel()

                    # Wait for all tasks to complete cancellation
                    await asyncio.wait(doc_tasks, return_when=asyncio.ALL_COMPLETED)

                    # Exit directly (document statuses already updated in process_document)
                    return

                # Check if there's a pending request to process more documents (with lock)
                has_pending_request = False
                async with pipeline_status_lock:
                    has_pending_request = pipeline_status.get("request_pending", False)
                    if has_pending_request:
                        # Clear the request flag before checking for more documents
                        pipeline_status["request_pending"] = False

                if not has_pending_request:
                    break

                log_message = "Processing additional documents due to pending request"
                logger.info(log_message)
                pipeline_status["latest_message"] = log_message
                pipeline_status["history_messages"].append(log_message)

                # Check for pending documents again
                to_process_docs = await self.doc_status.get_docs_by_statuses(
                    [DocStatus.PROCESSING, DocStatus.FAILED, DocStatus.PENDING]
                )

        finally:
            log_message = "Enqueued document processing pipeline stopped"
            logger.info(log_message)
            # Always reset busy status and cancellation flag when done or if an exception occurs (with lock)
            async with pipeline_status_lock:
                pipeline_status["busy"] = False
                pipeline_status["cancellation_requested"] = (
                    False  # Always reset cancellation flag
                )
                pipeline_status["latest_message"] = log_message
                pipeline_status["history_messages"].append(log_message)

    async def _agenerate_document_summary(
        self, content: str, *, max_input_chars: int = 12000
    ) -> str | None:
        """LLM-generate a short document summary for the list view.

        Fire this at the start of ``process_document`` in parallel with
        chunking. It goes through the same ``use_llm_func_with_cache``
        path as entity/relation merging, so identical content hits the
        LLM cache and never re-invokes the provider.

        Returns ``None`` on any failure — callers should fall back to
        ``get_content_summary(content)`` (truncated prefix) so the
        doc-status row always has *something* in the summary column.

        ``max_input_chars`` caps how much of the document we hand to
        the LLM. ~12k chars is roughly 3k-4k tokens on Chinese / English
        mixed text, which is a reasonable bound for a one-sentence
        summary task; going higher just burns tokens without improving
        the output.
        """
        from lightrag.prompt import PROMPTS
        from lightrag.utils import use_llm_func_with_cache
        from lightrag.constants import DEFAULT_SUMMARY_LANGUAGE

        if not content or not content.strip():
            return None

        # Slice on character count rather than tokens — the LLM will
        # stop reading once the main thrust of the doc is clear anyway,
        # and we want this step to stay cheap.
        input_text = content.strip()
        if len(input_text) > max_input_chars:
            input_text = input_text[:max_input_chars]

        language = self.addon_params.get("language", DEFAULT_SUMMARY_LANGUAGE)
        prompt = PROMPTS["document_summary"].format(
            language=language,
            content=input_text,
        )

        # _priority=8 matches the entity/relation summary path so doc
        # summaries don't starve foreground retrieval calls.
        use_llm_func = partial(self.llm_model_func, _priority=8)

        try:
            summary, _ = await use_llm_func_with_cache(
                prompt,
                use_llm_func,
                llm_response_cache=self.llm_response_cache,
                cache_type="doc_summary",
            )
        except Exception as exc:
            logger.warning(
                "Document summary LLM call failed (%s); using truncated prefix fallback.",
                exc,
            )
            return None

        if not summary:
            return None

        # Hard cap the output to keep wide-screen table cells tidy even
        # if the model ignores the prompt's length hint.
        summary = summary.strip()
        if len(summary) > 200:
            summary = summary[:200].rstrip() + "…"
        return summary

    async def _check_doc_cancelled(self, doc_id: str) -> bool:
        """
        Return True when an operator has flipped
        ``doc_status.metadata.cancel_requested = True`` on this document.

        The pipeline calls this at natural checkpoints (after chunking,
        before entity extraction, before graph merge). A True return
        signals the caller to raise ``DocumentCancelledException`` so
        this single document is terminated without affecting siblings.
        """
        try:
            record = await self.doc_status.get_by_id(doc_id)
        except Exception:
            return False
        if not record:
            return False
        metadata = record.get("metadata") or {}
        return bool(metadata.get("cancel_requested"))

    async def _process_extract_entities(
        self, chunk: dict[str, Any], pipeline_status=None, pipeline_status_lock=None
    ) -> list:
        try:
            chunk_results = await extract_entities(
                chunk,
                global_config=asdict(self),
                pipeline_status=pipeline_status,
                pipeline_status_lock=pipeline_status_lock,
                llm_response_cache=self.llm_response_cache,
                text_chunks_storage=self.text_chunks,
                doc_status_storage=self.doc_status,
            )
            return chunk_results
        except Exception as e:
            error_msg = f"Failed to extract entities and relationships: {str(e)}"
            logger.error(error_msg)
            async with pipeline_status_lock:
                pipeline_status["latest_message"] = error_msg
                pipeline_status["history_messages"].append(error_msg)
            raise e

    async def _insert_done(
        self, pipeline_status=None, pipeline_status_lock=None
    ) -> None:
        tasks = [
            cast(StorageNameSpace, storage_inst).index_done_callback()
            for storage_inst in [  # type: ignore
                self.full_docs,
                self.doc_status,
                self.text_chunks,
                self.full_entities,
                self.full_relations,
                self.entity_chunks,
                self.relation_chunks,
                self.llm_response_cache,
                self.entities_vdb,
                self.relationships_vdb,
                self.chunks_vdb,
                self.chunk_entity_relation_graph,
            ]
            if storage_inst is not None
        ]
        await asyncio.gather(*tasks)

        log_message = "In memory DB persist to disk"
        logger.info(log_message)

        if pipeline_status is not None and pipeline_status_lock is not None:
            async with pipeline_status_lock:
                pipeline_status["latest_message"] = log_message
                pipeline_status["history_messages"].append(log_message)

    def insert_custom_kg(
        self, custom_kg: dict[str, Any], full_doc_id: str = None
    ) -> None:
        loop = always_get_an_event_loop()
        loop.run_until_complete(self.ainsert_custom_kg(custom_kg, full_doc_id))

    async def ainsert_custom_kg(
        self,
        custom_kg: dict[str, Any],
        full_doc_id: str = None,
    ) -> None:
        update_storage = False
        try:
            # Insert chunks into vector storage
            all_chunks_data: dict[str, dict[str, str]] = {}
            chunk_to_source_map: dict[str, str] = {}
            for chunk_data in custom_kg.get("chunks", []):
                chunk_content = sanitize_text_for_encoding(chunk_data["content"])
                source_id = chunk_data["source_id"]
                file_path = chunk_data.get("file_path", "custom_kg")
                tokens = len(self.tokenizer.encode(chunk_content))
                chunk_order_index = (
                    0
                    if "chunk_order_index" not in chunk_data.keys()
                    else chunk_data["chunk_order_index"]
                )
                chunk_id = compute_mdhash_id(chunk_content, prefix="chunk-")

                chunk_entry = {
                    "content": chunk_content,
                    "source_id": source_id,
                    "tokens": tokens,
                    "chunk_order_index": chunk_order_index,
                    "full_doc_id": full_doc_id
                    if full_doc_id is not None
                    else source_id,
                    "file_path": file_path,
                    "status": DocStatus.PROCESSED,
                }
                all_chunks_data[chunk_id] = chunk_entry
                chunk_to_source_map[source_id] = chunk_id
                update_storage = True

            if all_chunks_data:
                await asyncio.gather(
                    self.chunks_vdb.upsert(all_chunks_data),
                    self.text_chunks.upsert(all_chunks_data),
                )

            # Insert entities into knowledge graph
            all_entities_data: list[dict[str, str]] = []
            for entity_data in custom_kg.get("entities", []):
                entity_name = entity_data["entity_name"]
                entity_type = entity_data.get("entity_type", "UNKNOWN")
                description = entity_data.get("description", "No description provided")
                source_chunk_id = entity_data.get("source_id", "UNKNOWN")
                source_id = chunk_to_source_map.get(source_chunk_id, "UNKNOWN")
                file_path = entity_data.get("file_path", "custom_kg")

                # Log if source_id is UNKNOWN
                if source_id == "UNKNOWN":
                    logger.warning(
                        f"Entity '{entity_name}' has an UNKNOWN source_id. Please check the source mapping."
                    )

                # Prepare node data
                node_data: dict[str, str] = {
                    "entity_id": entity_name,
                    "entity_type": entity_type,
                    "description": description,
                    "source_id": source_id,
                    "file_path": file_path,
                    "created_at": int(time.time()),
                }
                # Insert node data into the knowledge graph
                await self.chunk_entity_relation_graph.upsert_node(
                    entity_name, node_data=node_data
                )
                node_data["entity_name"] = entity_name
                all_entities_data.append(node_data)
                update_storage = True

            # Insert relationships into knowledge graph
            all_relationships_data: list[dict[str, str]] = []
            for relationship_data in custom_kg.get("relationships", []):
                src_id = relationship_data["src_id"]
                tgt_id = relationship_data["tgt_id"]
                description = relationship_data["description"]
                keywords = relationship_data["keywords"]
                weight = relationship_data.get("weight", 1.0)
                source_chunk_id = relationship_data.get("source_id", "UNKNOWN")
                source_id = chunk_to_source_map.get(source_chunk_id, "UNKNOWN")
                file_path = relationship_data.get("file_path", "custom_kg")

                # Log if source_id is UNKNOWN
                if source_id == "UNKNOWN":
                    logger.warning(
                        f"Relationship from '{src_id}' to '{tgt_id}' has an UNKNOWN source_id. Please check the source mapping."
                    )

                # Check if nodes exist in the knowledge graph
                for need_insert_id in [src_id, tgt_id]:
                    if not (
                        await self.chunk_entity_relation_graph.has_node(need_insert_id)
                    ):
                        await self.chunk_entity_relation_graph.upsert_node(
                            need_insert_id,
                            node_data={
                                "entity_id": need_insert_id,
                                "source_id": source_id,
                                "description": "UNKNOWN",
                                "entity_type": "UNKNOWN",
                                "file_path": file_path,
                                "created_at": int(time.time()),
                            },
                        )

                # Insert edge into the knowledge graph
                await self.chunk_entity_relation_graph.upsert_edge(
                    src_id,
                    tgt_id,
                    edge_data={
                        "weight": weight,
                        "description": description,
                        "keywords": keywords,
                        "source_id": source_id,
                        "file_path": file_path,
                        "created_at": int(time.time()),
                    },
                )

                edge_data: dict[str, str] = {
                    "src_id": src_id,
                    "tgt_id": tgt_id,
                    "description": description,
                    "keywords": keywords,
                    "source_id": source_id,
                    "weight": weight,
                    "file_path": file_path,
                    "created_at": int(time.time()),
                }
                all_relationships_data.append(edge_data)
                update_storage = True

            # Insert entities into vector storage with consistent format
            data_for_vdb = {
                compute_mdhash_id(dp["entity_name"], prefix="ent-"): {
                    "content": dp["entity_name"] + "\n" + dp["description"],
                    "entity_name": dp["entity_name"],
                    "source_id": dp["source_id"],
                    "description": dp["description"],
                    "entity_type": dp["entity_type"],
                    "file_path": dp.get("file_path", "custom_kg"),
                }
                for dp in all_entities_data
            }
            await self.entities_vdb.upsert(data_for_vdb)

            # Insert relationships into vector storage with consistent format
            data_for_vdb = {
                compute_mdhash_id(dp["src_id"] + dp["tgt_id"], prefix="rel-"): {
                    "src_id": dp["src_id"],
                    "tgt_id": dp["tgt_id"],
                    "source_id": dp["source_id"],
                    "content": f"{dp['keywords']}\t{dp['src_id']}\n{dp['tgt_id']}\n{dp['description']}",
                    "keywords": dp["keywords"],
                    "description": dp["description"],
                    "weight": dp["weight"],
                    "file_path": dp.get("file_path", "custom_kg"),
                }
                for dp in all_relationships_data
            }
            await self.relationships_vdb.upsert(data_for_vdb)

        except Exception as e:
            logger.error(f"Error in ainsert_custom_kg: {e}")
            raise
        finally:
            if update_storage:
                await self._insert_done()

    def query(
        self,
        query: str,
        param: QueryParam = QueryParam(),
        system_prompt: str | None = None,
    ) -> str | Iterator[str]:
        """
        Perform a sync query.

        Args:
            query (str): The query to be executed.
            param (QueryParam): Configuration parameters for query execution.
            prompt (Optional[str]): Custom prompts for fine-tuned control over the system's behavior. Defaults to None, which uses PROMPTS["rag_response"].

        Returns:
            str: The result of the query execution.
        """
        loop = always_get_an_event_loop()

        return loop.run_until_complete(self.aquery(query, param, system_prompt))  # type: ignore

    async def aquery(
        self,
        query: str,
        param: QueryParam = QueryParam(),
        system_prompt: str | None = None,
    ) -> str | AsyncIterator[str]:
        """
        Perform a async query (backward compatibility wrapper).

        This function is now a wrapper around aquery_llm that maintains backward compatibility
        by returning only the LLM response content in the original format.

        Args:
            query (str): The query to be executed.
            param (QueryParam): Configuration parameters for query execution.
                If param.model_func is provided, it will be used instead of the global model.
            system_prompt (Optional[str]): Custom prompts for fine-tuned control over the system's behavior. Defaults to None, which uses PROMPTS["rag_response"].

        Returns:
            str | AsyncIterator[str]: The LLM response content.
                - Non-streaming: Returns str
                - Streaming: Returns AsyncIterator[str]
        """
        # Call the new aquery_llm function to get complete results
        result = await self.aquery_llm(query, param, system_prompt)

        # Extract and return only the LLM response for backward compatibility
        llm_response = result.get("llm_response", {})

        if llm_response.get("is_streaming"):
            return llm_response.get("response_iterator")
        else:
            return llm_response.get("content", "")

    def query_data(
        self,
        query: str,
        param: QueryParam = QueryParam(),
    ) -> dict[str, Any]:
        """
        Synchronous data retrieval API: returns structured retrieval results without LLM generation.

        This function is the synchronous version of aquery_data, providing the same functionality
        for users who prefer synchronous interfaces.

        Args:
            query: Query text for retrieval.
            param: Query parameters controlling retrieval behavior (same as aquery).

        Returns:
            dict[str, Any]: Same structured data result as aquery_data.
        """
        loop = always_get_an_event_loop()
        return loop.run_until_complete(self.aquery_data(query, param))

    async def aquery_data(
        self,
        query: str,
        param: QueryParam = QueryParam(),
    ) -> dict[str, Any]:
        """
        Asynchronous data retrieval API: returns structured retrieval results without LLM generation.

        This function reuses the same logic as aquery but stops before LLM generation,
        returning the final processed entities, relationships, and chunks data that would be sent to LLM.

        Args:
            query: Query text for retrieval.
            param: Query parameters controlling retrieval behavior (same as aquery).

        Returns:
            dict[str, Any]: Structured data result in the following format:

            **Success Response:**
            ```python
            {
                "status": "success",
                "message": "Query executed successfully",
                "data": {
                    "entities": [
                        {
                            "entity_name": str,      # Entity identifier
                            "entity_type": str,      # Entity category/type
                            "description": str,      # Entity description
                            "source_id": str,        # Source chunk references
                            "file_path": str,        # Origin file path
                            "created_at": str,       # Creation timestamp
                            "reference_id": str      # Reference identifier for citations
                        }
                    ],
                    "relationships": [
                        {
                            "src_id": str,           # Source entity name
                            "tgt_id": str,           # Target entity name
                            "description": str,      # Relationship description
                            "keywords": str,         # Relationship keywords
                            "weight": float,         # Relationship strength
                            "source_id": str,        # Source chunk references
                            "file_path": str,        # Origin file path
                            "created_at": str,       # Creation timestamp
                            "reference_id": str      # Reference identifier for citations
                        }
                    ],
                    "chunks": [
                        {
                            "content": str,          # Document chunk content
                            "file_path": str,        # Origin file path
                            "chunk_id": str,         # Unique chunk identifier
                            "reference_id": str      # Reference identifier for citations
                        }
                    ],
                    "references": [
                        {
                            "reference_id": str,     # Reference identifier
                            "file_path": str         # Corresponding file path
                        }
                    ]
                },
                "metadata": {
                    "query_mode": str,           # Query mode used ("local", "global", "hybrid", "mix", "naive", "bypass")
                    "keywords": {
                        "high_level": List[str], # High-level keywords extracted
                        "low_level": List[str]   # Low-level keywords extracted
                    },
                    "processing_info": {
                        "total_entities_found": int,        # Total entities before truncation
                        "total_relations_found": int,       # Total relations before truncation
                        "entities_after_truncation": int,   # Entities after token truncation
                        "relations_after_truncation": int,  # Relations after token truncation
                        "merged_chunks_count": int,          # Chunks before final processing
                        "final_chunks_count": int            # Final chunks in result
                    }
                }
            }
            ```

            **Query Mode Differences:**
            - **local**: Focuses on entities and their related chunks based on low-level keywords
            - **global**: Focuses on relationships and their connected entities based on high-level keywords
            - **hybrid**: Combines local and global results using round-robin merging
            - **mix**: Includes knowledge graph data plus vector-retrieved document chunks
            - **naive**: Only vector-retrieved chunks, entities and relationships arrays are empty
            - **bypass**: All data arrays are empty, used for direct LLM queries

            ** processing_info is optional and may not be present in all responses, especially when query result is empty**

            **Failure Response:**
            ```python
            {
                "status": "failure",
                "message": str,  # Error description
                "data": {}       # Empty data object
            }
            ```

            **Common Failure Cases:**
            - Empty query string
            - Both high-level and low-level keywords are empty
            - Query returns empty dataset
            - Missing tokenizer or system configuration errors

        Note:
            The function adapts to the new data format from convert_to_user_format where
            actual data is nested under the 'data' field, with 'status' and 'message'
            fields at the top level.
        """
        global_config = asdict(self)

        # Create a copy of param to avoid modifying the original
        data_param = QueryParam(
            mode=param.mode,
            only_need_context=True,  # Skip LLM generation, only get context and data
            only_need_prompt=False,
            response_type=param.response_type,
            stream=False,  # Data retrieval doesn't need streaming
            top_k=param.top_k,
            chunk_top_k=param.chunk_top_k,
            max_entity_tokens=param.max_entity_tokens,
            max_relation_tokens=param.max_relation_tokens,
            max_total_tokens=param.max_total_tokens,
            hl_keywords=param.hl_keywords,
            ll_keywords=param.ll_keywords,
            conversation_history=param.conversation_history,
            history_turns=param.history_turns,
            model_func=param.model_func,
            user_prompt=param.user_prompt,
            enable_rerank=param.enable_rerank,
        )

        query_result = None

        if data_param.mode in ["local", "global", "hybrid", "mix"]:
            logger.debug(f"[aquery_data] Using kg_query for mode: {data_param.mode}")
            query_result = await kg_query(
                query.strip(),
                self.chunk_entity_relation_graph,
                self.entities_vdb,
                self.relationships_vdb,
                self.text_chunks,
                data_param,  # Use data_param with only_need_context=True
                global_config,
                hashing_kv=self.llm_response_cache,
                system_prompt=None,
                chunks_vdb=self.chunks_vdb,
                # Multimodal: these are None when the multimodal pipeline
                # is not configured, and kg_query falls back to pure text
                # retrieval without any change in behavior.
                images_vdb=self.images_vdb,
                image_metadata=self.image_metadata,
            )
        elif data_param.mode == "naive":
            logger.debug(f"[aquery_data] Using naive_query for mode: {data_param.mode}")
            query_result = await naive_query(
                query.strip(),
                self.chunks_vdb,
                data_param,  # Use data_param with only_need_context=True
                global_config,
                hashing_kv=self.llm_response_cache,
                system_prompt=None,
                images_vdb=self.images_vdb,
                image_metadata=self.image_metadata,
            )
        elif data_param.mode == "bypass":
            logger.debug("[aquery_data] Using bypass mode")
            # bypass mode returns empty data using convert_to_user_format
            empty_raw_data = convert_to_user_format(
                [],  # no entities
                [],  # no relationships
                [],  # no chunks
                [],  # no references
                "bypass",
            )
            query_result = QueryResult(content="", raw_data=empty_raw_data)
        else:
            raise ValueError(f"Unknown mode {data_param.mode}")

        if query_result is None:
            no_result_message = "Query returned no results"
            if data_param.mode == "naive":
                no_result_message = "No relevant document chunks found."
            final_data: dict[str, Any] = {
                "status": "failure",
                "message": no_result_message,
                "data": {},
                "metadata": {
                    "failure_reason": "no_results",
                    "mode": data_param.mode,
                },
            }
            logger.info("[aquery_data] Query returned no results.")
        else:
            # Extract raw_data from QueryResult
            final_data = query_result.raw_data or {}

            # Log final result counts - adapt to new data format from convert_to_user_format
            if final_data and "data" in final_data:
                data_section = final_data["data"]
                entities_count = len(data_section.get("entities", []))
                relationships_count = len(data_section.get("relationships", []))
                chunks_count = len(data_section.get("chunks", []))
                logger.debug(
                    f"[aquery_data] Final result: {entities_count} entities, {relationships_count} relationships, {chunks_count} chunks"
                )
            else:
                logger.warning("[aquery_data] No data section found in query result")

        await self._query_done()
        return final_data

    async def aquery_llm(
        self,
        query: str,
        param: QueryParam = QueryParam(),
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        """
        Asynchronous complete query API: returns structured retrieval results with LLM generation.

        This function performs a single query operation and returns both structured data and LLM response,
        based on the original aquery logic to avoid duplicate calls.

        Args:
            query: Query text for retrieval and LLM generation.
            param: Query parameters controlling retrieval and LLM behavior.
            system_prompt: Optional custom system prompt for LLM generation.

        Returns:
            dict[str, Any]: Complete response with structured data and LLM response.
        """
        logger.debug(f"[aquery_llm] Query param: {param}")

        global_config = asdict(self)

        try:
            query_result = None

            if param.mode in ["local", "global", "hybrid", "mix"]:
                query_result = await kg_query(
                    query.strip(),
                    self.chunk_entity_relation_graph,
                    self.entities_vdb,
                    self.relationships_vdb,
                    self.text_chunks,
                    param,
                    global_config,
                    hashing_kv=self.llm_response_cache,
                    system_prompt=system_prompt,
                    chunks_vdb=self.chunks_vdb,
                    # Multimodal: None when not configured -> pure text retrieval.
                    images_vdb=self.images_vdb,
                    image_metadata=self.image_metadata,
                )
            elif param.mode == "naive":
                query_result = await naive_query(
                    query.strip(),
                    self.chunks_vdb,
                    param,
                    global_config,
                    hashing_kv=self.llm_response_cache,
                    system_prompt=system_prompt,
                    images_vdb=self.images_vdb,
                    image_metadata=self.image_metadata,
                )
            elif param.mode == "bypass":
                # Bypass mode: directly use LLM without knowledge retrieval
                use_llm_func = param.model_func or global_config["llm_model_func"]
                # Apply higher priority (8) to entity/relation summary tasks
                use_llm_func = partial(use_llm_func, _priority=8)

                param.stream = True if param.stream is None else param.stream
                response = await use_llm_func(
                    query.strip(),
                    system_prompt=system_prompt,
                    history_messages=param.conversation_history,
                    enable_cot=True,
                    stream=param.stream,
                )
                if type(response) is str:
                    return {
                        "status": "success",
                        "message": "Bypass mode LLM non streaming response",
                        "data": {},
                        "metadata": {},
                        "llm_response": {
                            "content": response,
                            "response_iterator": None,
                            "is_streaming": False,
                        },
                    }
                else:
                    return {
                        "status": "success",
                        "message": "Bypass mode LLM streaming response",
                        "data": {},
                        "metadata": {},
                        "llm_response": {
                            "content": None,
                            "response_iterator": response,
                            "is_streaming": True,
                        },
                    }
            else:
                raise ValueError(f"Unknown mode {param.mode}")

            await self._query_done()

            # Check if query_result is None
            if query_result is None:
                return {
                    "status": "failure",
                    "message": "Query returned no results",
                    "data": {},
                    "metadata": {
                        "failure_reason": "no_results",
                        "mode": param.mode,
                    },
                    "llm_response": {
                        "content": PROMPTS["fail_response"],
                        "response_iterator": None,
                        "is_streaming": False,
                    },
                }

            # Extract structured data from query result
            raw_data = query_result.raw_data or {}
            raw_data["llm_response"] = {
                "content": query_result.content
                if not query_result.is_streaming
                else None,
                "response_iterator": query_result.response_iterator
                if query_result.is_streaming
                else None,
                "is_streaming": query_result.is_streaming,
            }

            return raw_data

        except Exception as e:
            logger.error(f"Query failed: {e}")
            # Return error response
            return {
                "status": "failure",
                "message": f"Query failed: {str(e)}",
                "data": {},
                "metadata": {},
                "llm_response": {
                    "content": None,
                    "response_iterator": None,
                    "is_streaming": False,
                },
            }

    def query_llm(
        self,
        query: str,
        param: QueryParam = QueryParam(),
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        """
        Synchronous complete query API: returns structured retrieval results with LLM generation.

        This function is the synchronous version of aquery_llm, providing the same functionality
        for users who prefer synchronous interfaces.

        Args:
            query: Query text for retrieval and LLM generation.
            param: Query parameters controlling retrieval and LLM behavior.
            system_prompt: Optional custom system prompt for LLM generation.

        Returns:
            dict[str, Any]: Same complete response format as aquery_llm.
        """
        loop = always_get_an_event_loop()
        return loop.run_until_complete(self.aquery_llm(query, param, system_prompt))

    async def _query_done(self):
        await self.llm_response_cache.index_done_callback()

    async def _update_delete_retry_state(
        self,
        doc_id: str,
        doc_status_data: dict[str, Any],
        *,
        deletion_stage: str,
        doc_llm_cache_ids: list[str],
        error_message: str | None = None,
        failed: bool,
    ) -> dict[str, Any]:
        """Persist deletion retry metadata and return the updated status record."""
        metadata = doc_status_data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        backup_cache_ids = _normalize_string_list(
            metadata.get("deletion_llm_cache_ids", []),
            context=f"doc {doc_id} metadata.deletion_llm_cache_ids",
        )
        retry_cache_ids = doc_llm_cache_ids or backup_cache_ids

        updated_metadata = dict(metadata)
        if retry_cache_ids:
            updated_metadata["deletion_llm_cache_ids"] = retry_cache_ids
        updated_metadata["last_deletion_attempt_at"] = datetime.now(
            timezone.utc
        ).isoformat()

        if failed:
            updated_metadata["deletion_failed"] = True
            updated_metadata["deletion_failure_stage"] = deletion_stage
        else:
            updated_metadata.pop("deletion_failed", None)
            updated_metadata.pop("deletion_failure_stage", None)

        updated_status_data = {
            **doc_status_data,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "metadata": updated_metadata,
            "error_msg": error_message if failed else "",
        }

        await self.doc_status.upsert({doc_id: updated_status_data})
        return updated_status_data

    async def _get_existing_llm_cache_ids(self, cache_ids: list[str]) -> list[str]:
        """Return cache IDs that still exist in cache storage.

        Some KV storage backends only log delete failures and return without
        raising, so callers must verify which records still exist after delete.

        Returns an empty list immediately if cache storage is unavailable.
        Callers must check storage availability independently before treating
        an empty result as a confirmed deletion.
        """
        if not self.llm_response_cache or not cache_ids:
            return []

        try:
            existing_records = await self.llm_response_cache.get_by_ids(cache_ids)
        except Exception as verification_error:
            raise Exception(
                f"Failed to verify LLM cache deletion "
                f"(delete may have succeeded): {verification_error}"
            ) from verification_error
        return [
            cache_id
            for cache_id, record in zip(cache_ids, existing_records)
            if record is not None
        ]

    async def aclear_cache(self) -> None:
        """Clear all cache data from the LLM response cache storage.

        This method clears all cached LLM responses regardless of mode.

        Example:
            # Clear all cache
            await rag.aclear_cache()
        """
        if not self.llm_response_cache:
            logger.warning("No cache storage configured")
            return

        try:
            # Clear all cache using drop method
            success = await self.llm_response_cache.drop()
            if success:
                logger.info("Cleared all cache")
            else:
                logger.warning("Failed to clear all cache")

            await self.llm_response_cache.index_done_callback()

        except Exception as e:
            logger.error(f"Error while clearing cache: {e}")

    def clear_cache(self) -> None:
        """Synchronous version of aclear_cache."""
        return always_get_an_event_loop().run_until_complete(self.aclear_cache())

    async def get_docs_by_status(
        self, status: DocStatus
    ) -> dict[str, DocProcessingStatus]:
        """Get documents by status

        Returns:
            Dict with document id is keys and document status is values
        """
        return await self.doc_status.get_docs_by_status(status)

    async def aget_docs_by_ids(
        self, ids: str | list[str]
    ) -> dict[str, DocProcessingStatus]:
        """Retrieves the processing status for one or more documents by their IDs.

        Args:
            ids: A single document ID (string) or a list of document IDs (list of strings).

        Returns:
            A dictionary where keys are the document IDs for which a status was found,
            and values are the corresponding DocProcessingStatus objects. IDs that
            are not found in the storage will be omitted from the result dictionary.
        """
        if isinstance(ids, str):
            # Ensure input is always a list of IDs for uniform processing
            id_list = [ids]
        elif (
            ids is None
        ):  # Handle potential None input gracefully, although type hint suggests str/list
            logger.warning(
                "aget_docs_by_ids called with None input, returning empty dict."
            )
            return {}
        else:
            # Assume input is already a list if not a string
            id_list = ids

        # Return early if the final list of IDs is empty
        if not id_list:
            logger.debug("aget_docs_by_ids called with an empty list of IDs.")
            return {}

        # Create tasks to fetch document statuses concurrently using the doc_status storage
        tasks = [self.doc_status.get_by_id(doc_id) for doc_id in id_list]
        # Execute tasks concurrently and gather the results. Results maintain order.
        # Type hint indicates results can be DocProcessingStatus or None if not found.
        results_list: list[Optional[DocProcessingStatus]] = await asyncio.gather(*tasks)

        # Build the result dictionary, mapping found IDs to their statuses
        found_statuses: dict[str, DocProcessingStatus] = {}
        # Keep track of IDs for which no status was found (for logging purposes)
        not_found_ids: list[str] = []

        # Iterate through the results, correlating them back to the original IDs
        for i, status_obj in enumerate(results_list):
            doc_id = id_list[
                i
            ]  # Get the original ID corresponding to this result index
            if status_obj:
                # If a status object was returned (not None), add it to the result dict
                found_statuses[doc_id] = status_obj
            else:
                # If status_obj is None, the document ID was not found in storage
                not_found_ids.append(doc_id)

        # Log a warning if any of the requested document IDs were not found
        if not_found_ids:
            logger.warning(
                f"Document statuses not found for the following IDs: {not_found_ids}"
            )

        # Return the dictionary containing statuses only for the found document IDs
        return found_statuses

    async def adelete_by_doc_id(
        self, doc_id: str, delete_llm_cache: bool = False
    ) -> DeletionResult:
        """Delete a document and all its related data, including chunks, graph elements.

        This method orchestrates a comprehensive deletion process for a given document ID.
        It ensures that not only the document itself but also all its derived and associated
        data across different storage layers are removed or rebuiled. If entities or relationships
        are partially affected, they will be rebuilded using LLM cached from remaining documents.

        **Concurrency Control Design:**

        This function implements a pipeline-based concurrency control to prevent data corruption:

        1. **Single Document Deletion** (when WE acquire pipeline):
           - Sets job_name to "Single document deletion" (NOT starting with "deleting")
           - Prevents other adelete_by_doc_id calls from running concurrently
           - Ensures exclusive access to graph operations for this deletion

        2. **Batch Document Deletion** (when background_delete_documents acquires pipeline):
           - Sets job_name to "Deleting {N} Documents" (starts with "deleting")
           - Allows multiple adelete_by_doc_id calls to join the deletion queue
           - Each call validates the job name to ensure it's part of a deletion operation

        The validation logic `if not job_name.startswith("deleting") or "document" not in job_name`
        ensures that:
        - adelete_by_doc_id can only run when pipeline is idle OR during batch deletion
        - Prevents concurrent single deletions that could cause race conditions
        - Rejects operations when pipeline is busy with non-deletion tasks

        Args:
            doc_id (str): The unique identifier of the document to be deleted.
            delete_llm_cache (bool): Whether to delete cached LLM extraction results
                associated with the document. Defaults to False.

        Returns:
            DeletionResult: An object containing the outcome of the deletion process.
                - `status` (str): "success", "not_found", "not_allowed", or "fail".
                - `doc_id` (str): The ID of the document attempted to be deleted.
                - `message` (str): A summary of the operation's result.
                - `status_code` (int): HTTP status code (e.g., 200, 404, 403, 500).
                - `file_path` (str | None): The file path of the deleted document, if available.
        """
        # Get pipeline status shared data and lock for validation
        pipeline_status = await get_namespace_data(
            "pipeline_status", workspace=self.workspace
        )
        pipeline_status_lock = get_namespace_lock(
            "pipeline_status", workspace=self.workspace
        )

        # Track whether WE acquired the pipeline
        we_acquired_pipeline = False

        # Check and acquire pipeline if needed
        async with pipeline_status_lock:
            if not pipeline_status.get("busy", False):
                # Pipeline is idle - WE acquire it for this deletion
                we_acquired_pipeline = True
                pipeline_status.update(
                    {
                        "busy": True,
                        "job_name": "Single document deletion",
                        "job_start": datetime.now(timezone.utc).isoformat(),
                        "docs": 1,
                        "batchs": 1,
                        "cur_batch": 0,
                        "total_chunks": 0,
                        "processed_chunks": 0,
                        "current_stage": "document_deletion",
                        "current_stage_label": "Deleting single document",
                        "stage_unit": "documents",
                        "stage_total": 1,
                        "stage_processed": 0,
                        "stage_remaining": 1,
                        "stage_elapsed_seconds": 0,
                        "stage_eta_seconds": None,
                        "request_pending": False,
                        "cancellation_requested": False,
                        "latest_message": f"Starting deletion for document: {doc_id}",
                    }
                )
                # Initialize history messages
                pipeline_status["history_messages"][:] = [
                    f"Starting deletion for document: {doc_id}"
                ]
            else:
                # Pipeline already busy - verify it's a deletion job
                job_name = pipeline_status.get("job_name", "").lower()
                if not job_name.startswith("deleting") or "document" not in job_name:
                    return DeletionResult(
                        status="not_allowed",
                        doc_id=doc_id,
                        message=f"Deletion not allowed: current job '{pipeline_status.get('job_name')}' is not a document deletion job",
                        status_code=403,
                        file_path=None,
                    )
                # Pipeline is busy with deletion - proceed without acquiring

        deletion_operations_started = False
        deletion_fully_completed = False
        in_final_delete_stage = False
        original_exception = None
        doc_llm_cache_ids: list[str] = []
        deletion_stage = "initializing"
        doc_status_data: dict[str, Any] | None = None
        file_path: str | None = None

        async with pipeline_status_lock:
            log_message = f"Starting deletion process for document {doc_id}"
            logger.info(log_message)
            pipeline_status["latest_message"] = log_message
            pipeline_status["history_messages"].append(log_message)

        try:
            # 1. Get the document status and related data
            doc_status_data = await self.doc_status.get_by_id(doc_id)
            file_path = doc_status_data.get("file_path") if doc_status_data else None
            if not doc_status_data:
                logger.warning(f"Document {doc_id} not found")
                return DeletionResult(
                    status="not_found",
                    doc_id=doc_id,
                    message=f"Document {doc_id} not found.",
                    status_code=404,
                    file_path="",
                )

            # Check document status and log warning for non-completed documents
            raw_status = doc_status_data.get("status")
            try:
                doc_status = DocStatus(raw_status)
            except ValueError:
                doc_status = raw_status

            if doc_status != DocStatus.PROCESSED:
                if doc_status == DocStatus.PENDING:
                    warning_msg = (
                        f"Deleting {doc_id} {file_path}(previous status: PENDING)"
                    )
                elif doc_status == DocStatus.PROCESSING:
                    warning_msg = (
                        f"Deleting {doc_id} {file_path}(previous status: PROCESSING)"
                    )
                elif doc_status == DocStatus.PREPROCESSED:
                    warning_msg = (
                        f"Deleting {doc_id} {file_path}(previous status: PREPROCESSED)"
                    )
                elif doc_status == DocStatus.FAILED:
                    warning_msg = (
                        f"Deleting {doc_id} {file_path}(previous status: FAILED)"
                    )
                else:
                    status_text = (
                        doc_status.value
                        if isinstance(doc_status, DocStatus)
                        else str(doc_status)
                    )
                    warning_msg = (
                        f"Deleting {doc_id} {file_path}(previous status: {status_text})"
                    )
                logger.info(warning_msg)
                # Update pipeline status for monitoring
                async with pipeline_status_lock:
                    pipeline_status["latest_message"] = warning_msg
                    pipeline_status["history_messages"].append(warning_msg)

            # 2. Get chunk IDs from document status
            metadata = doc_status_data.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            metadata_cache_ids = _normalize_string_list(
                metadata.get("deletion_llm_cache_ids", []),
                context=f"doc {doc_id} metadata.deletion_llm_cache_ids",
            )
            chunk_ids = set(
                _normalize_string_list(
                    doc_status_data.get("chunks_list", []),
                    context=f"doc {doc_id} chunks_list",
                )
            )

            if not chunk_ids:
                logger.warning(f"No chunks found for document {doc_id}")
                # Mark that deletion operations have started
                deletion_operations_started = True

                # A prior failed deletion may have collected LLM cache IDs before the
                # chunks were removed. If delete_llm_cache is requested and persisted IDs
                # exist, clean them up now before removing the doc/status entries.
                if delete_llm_cache and metadata_cache_ids:
                    if not self.llm_response_cache:
                        no_cache_msg = (
                            f"Cannot delete LLM cache for document {doc_id}: "
                            "cache storage is unavailable"
                        )
                        logger.error(no_cache_msg)
                        async with pipeline_status_lock:
                            pipeline_status["latest_message"] = no_cache_msg
                            pipeline_status["history_messages"].append(no_cache_msg)
                        raise Exception(no_cache_msg)
                    try:
                        deletion_stage = "delete_llm_cache"
                        await self.llm_response_cache.delete(metadata_cache_ids)
                        remaining_cache_ids = await self._get_existing_llm_cache_ids(
                            metadata_cache_ids
                        )
                        if remaining_cache_ids:
                            raise Exception(
                                f"{len(remaining_cache_ids)} LLM cache entries still exist after delete"
                            )
                        logger.info(
                            "Cleaned up %d LLM cache entries from prior attempt for document %s",
                            len(metadata_cache_ids),
                            doc_id,
                        )
                    except Exception as cache_err:
                        raise Exception(
                            f"Failed to delete LLM cache for document {doc_id}: {cache_err}"
                        ) from cache_err

                try:
                    # Still need to delete the doc status and full doc.
                    # Delete doc_status first: if full_docs.delete fails on retry, the
                    # doc_status record is already gone so the retry finds no record and
                    # treats the document as already deleted rather than creating a zombie.
                    deletion_stage = "delete_doc_entries"
                    await self.doc_status.delete([doc_id])
                    await self.full_docs.delete([doc_id])
                except Exception as e:
                    logger.error(
                        f"Failed to delete document {doc_id} with no chunks: {e}"
                    )
                    raise Exception(f"Failed to delete document entry: {e}") from e

                async with pipeline_status_lock:
                    log_message = (
                        f"Document deleted without associated chunks: {doc_id}"
                    )
                    logger.info(log_message)
                    pipeline_status["latest_message"] = log_message
                    pipeline_status["history_messages"].append(log_message)

                deletion_fully_completed = True
                return DeletionResult(
                    status="success",
                    doc_id=doc_id,
                    message=log_message,
                    status_code=200,
                    file_path=file_path,
                )

            # Mark that deletion operations have started
            deletion_operations_started = True

            if chunk_ids:
                # Always collect/persist cache IDs for chunk-backed documents, even when
                # this call does not request cache deletion. If a delete fails after the
                # chunks/graph have already been removed, a later retry may turn on
                # delete_llm_cache=True, and doc_status metadata is then the only durable
                # place left to recover the cache keys for cleanup.
                deletion_stage = "collect_llm_cache"
                doc_llm_cache_ids = list(metadata_cache_ids)
                if not self.text_chunks:
                    logger.info(
                        "Skipping LLM cache id collection for document %s because text chunk storage is unavailable",
                        doc_id,
                    )
                else:
                    try:
                        chunk_data_list = await self.text_chunks.get_by_ids(
                            list(chunk_ids)
                        )
                        seen_cache_ids: set[str] = set(doc_llm_cache_ids)
                        for chunk_data in chunk_data_list:
                            if not chunk_data or not isinstance(chunk_data, dict):
                                continue
                            cache_ids = chunk_data.get("llm_cache_list", [])
                            if not isinstance(cache_ids, list):
                                continue
                            for cache_id in cache_ids:
                                if (
                                    isinstance(cache_id, str)
                                    and cache_id
                                    and cache_id not in seen_cache_ids
                                ):
                                    doc_llm_cache_ids.append(cache_id)
                                    seen_cache_ids.add(cache_id)
                    except Exception as cache_collect_error:
                        logger.error(
                            "Failed to collect LLM cache ids for document %s: %s",
                            doc_id,
                            cache_collect_error,
                        )
                        raise Exception(
                            f"Failed to collect LLM cache ids for document {doc_id}: {cache_collect_error}"
                        ) from cache_collect_error

                if doc_llm_cache_ids:
                    try:
                        doc_status_data = await self._update_delete_retry_state(
                            doc_id,
                            doc_status_data,
                            deletion_stage=deletion_stage,
                            doc_llm_cache_ids=doc_llm_cache_ids,
                            failed=False,
                        )
                    except Exception as status_write_error:
                        logger.error(
                            "Failed to persist LLM cache IDs for document %s to retry state: %s",
                            doc_id,
                            status_write_error,
                        )
                        # Describe whether this is a fresh attempt or a retry so
                        # operators can tell whether prior partial deletions exist.
                        attempt_context = (
                            "retry — prior partial deletions may exist"
                            if metadata_cache_ids
                            else "deletion not yet started"
                        )
                        raise Exception(
                            f"Failed to persist LLM cache IDs for document {doc_id} "
                            f"({attempt_context}): {status_write_error}"
                        ) from status_write_error
                    logger.info(
                        "Collected %d LLM cache entries for document %s",
                        len(doc_llm_cache_ids),
                        doc_id,
                    )
                else:
                    logger.info("No LLM cache entries found for document %s", doc_id)

            # 4. Analyze entities and relationships that will be affected
            entities_to_delete = set()
            entities_to_rebuild = {}  # entity_name -> remaining chunk id list
            relationships_to_delete = set()
            relationships_to_rebuild = {}  # (src, tgt) -> remaining chunk id list
            entity_chunk_updates: dict[str, list[str]] = {}
            relation_chunk_updates: dict[tuple[str, str], list[str]] = {}

            try:
                deletion_stage = "analyze_graph_dependencies"
                # Get affected entities and relations from full_entities and full_relations storage
                doc_entities_data = await self.full_entities.get_by_id(doc_id)
                doc_relations_data = await self.full_relations.get_by_id(doc_id)

                affected_nodes = []
                affected_edges = []

                # Get entity data from graph storage using entity names from full_entities
                if doc_entities_data and "entity_names" in doc_entities_data:
                    entity_names = doc_entities_data["entity_names"]
                    # get_nodes_batch returns dict[str, dict], need to convert to list[dict]
                    nodes_dict = await self.chunk_entity_relation_graph.get_nodes_batch(
                        entity_names
                    )
                    for entity_name in entity_names:
                        node_data = nodes_dict.get(entity_name)
                        if node_data:
                            # Ensure compatibility with existing logic that expects "id" field
                            if "id" not in node_data:
                                node_data["id"] = entity_name
                            affected_nodes.append(node_data)

                # Get relation data from graph storage using relation pairs from full_relations
                if doc_relations_data and "relation_pairs" in doc_relations_data:
                    relation_pairs = doc_relations_data["relation_pairs"]
                    edge_pairs_dicts = [
                        {"src": pair[0], "tgt": pair[1]} for pair in relation_pairs
                    ]
                    # get_edges_batch returns dict[tuple[str, str], dict], need to convert to list[dict]
                    edges_dict = await self.chunk_entity_relation_graph.get_edges_batch(
                        edge_pairs_dicts
                    )

                    for pair in relation_pairs:
                        src, tgt = pair[0], pair[1]
                        edge_key = (src, tgt)
                        edge_data = edges_dict.get(edge_key)
                        if edge_data:
                            # Ensure compatibility with existing logic that expects "source" and "target" fields
                            if "source" not in edge_data:
                                edge_data["source"] = src
                            if "target" not in edge_data:
                                edge_data["target"] = tgt
                            affected_edges.append(edge_data)

            except Exception as e:
                logger.error(f"Failed to analyze affected graph elements: {e}")
                raise Exception(f"Failed to analyze graph dependencies: {e}") from e

            try:
                # Process entities
                for node_data in affected_nodes:
                    node_label = node_data.get("entity_id")
                    if not node_label:
                        continue

                    existing_sources: list[str] = []
                    graph_sources: list[str] = []
                    if self.entity_chunks:
                        stored_chunks = await self.entity_chunks.get_by_id(node_label)
                        if stored_chunks and isinstance(stored_chunks, dict):
                            existing_sources = [
                                chunk_id
                                for chunk_id in stored_chunks.get("chunk_ids", [])
                                if chunk_id
                            ]

                    if node_data.get("source_id"):
                        graph_sources = [
                            chunk_id
                            for chunk_id in node_data["source_id"].split(
                                GRAPH_FIELD_SEP
                            )
                            if chunk_id
                        ]

                    if not existing_sources:
                        existing_sources = graph_sources

                    if not existing_sources:
                        # No chunk references means this entity should be deleted
                        entities_to_delete.add(node_label)
                        entity_chunk_updates[node_label] = []
                        continue

                    remaining_sources = subtract_source_ids(existing_sources, chunk_ids)
                    # `existing_sources` comes from chunk-tracking storage when available, but
                    # graph `source_id` can still be stale after a failed prior delete. If the
                    # graph still references any chunk being deleted in this attempt, force a
                    # rebuild/delete so the graph metadata gets synchronized instead of being
                    # left untouched with orphaned source references.
                    graph_references_deleted_chunks = bool(
                        graph_sources and set(graph_sources) & chunk_ids
                    )

                    if not remaining_sources:
                        entities_to_delete.add(node_label)
                        entity_chunk_updates[node_label] = []
                    elif (
                        remaining_sources != existing_sources
                        or graph_references_deleted_chunks
                    ):
                        entities_to_rebuild[node_label] = remaining_sources
                        entity_chunk_updates[node_label] = remaining_sources
                    else:
                        logger.info(f"Untouch entity: {node_label}")

                async with pipeline_status_lock:
                    log_message = f"Found {len(entities_to_rebuild)} affected entities"
                    logger.info(log_message)
                    pipeline_status["latest_message"] = log_message
                    pipeline_status["history_messages"].append(log_message)

                # Process relationships
                for edge_data in affected_edges:
                    # source target is not in normalize order in graph db property
                    src = edge_data.get("source")
                    tgt = edge_data.get("target")

                    if not src or not tgt or "source_id" not in edge_data:
                        continue

                    edge_tuple = tuple(sorted((src, tgt)))
                    if (
                        edge_tuple in relationships_to_delete
                        or edge_tuple in relationships_to_rebuild
                    ):
                        continue

                    existing_sources: list[str] = []
                    graph_sources: list[str] = []
                    if self.relation_chunks:
                        storage_key = make_relation_chunk_key(src, tgt)
                        stored_chunks = await self.relation_chunks.get_by_id(
                            storage_key
                        )
                        if stored_chunks and isinstance(stored_chunks, dict):
                            existing_sources = [
                                chunk_id
                                for chunk_id in stored_chunks.get("chunk_ids", [])
                                if chunk_id
                            ]

                    if edge_data.get("source_id"):
                        graph_sources = [
                            chunk_id
                            for chunk_id in edge_data["source_id"].split(
                                GRAPH_FIELD_SEP
                            )
                            if chunk_id
                        ]

                    if not existing_sources:
                        existing_sources = graph_sources

                    if not existing_sources:
                        # No chunk references means this relationship should be deleted
                        relationships_to_delete.add(edge_tuple)
                        relation_chunk_updates[edge_tuple] = []
                        continue

                    remaining_sources = subtract_source_ids(existing_sources, chunk_ids)
                    # Same as the entity path above: even when relation chunk-tracking is already
                    # correct, the graph edge may still carry a stale `source_id` that mentions a
                    # chunk deleted in this attempt. Treat that as an affected relation so retry
                    # deletion can repair the graph metadata rather than skipping it as "untouched".
                    graph_references_deleted_chunks = bool(
                        graph_sources and set(graph_sources) & chunk_ids
                    )

                    if not remaining_sources:
                        relationships_to_delete.add(edge_tuple)
                        relation_chunk_updates[edge_tuple] = []
                    elif (
                        remaining_sources != existing_sources
                        or graph_references_deleted_chunks
                    ):
                        relationships_to_rebuild[edge_tuple] = remaining_sources
                        relation_chunk_updates[edge_tuple] = remaining_sources
                    else:
                        logger.info(f"Untouch relation: {edge_tuple}")

                async with pipeline_status_lock:
                    log_message = (
                        f"Found {len(relationships_to_rebuild)} affected relations"
                    )
                    logger.info(log_message)
                    pipeline_status["latest_message"] = log_message
                    pipeline_status["history_messages"].append(log_message)

                current_time = int(time.time())
                deletion_stage = "update_chunk_tracking"

                if entity_chunk_updates and self.entity_chunks:
                    entity_upsert_payload = {}
                    for entity_name, remaining in entity_chunk_updates.items():
                        if not remaining:
                            # Empty entities are deleted alongside graph nodes later
                            continue
                        entity_upsert_payload[entity_name] = {
                            "chunk_ids": remaining,
                            "count": len(remaining),
                            "updated_at": current_time,
                        }
                    if entity_upsert_payload:
                        await self.entity_chunks.upsert(entity_upsert_payload)

                if relation_chunk_updates and self.relation_chunks:
                    relation_upsert_payload = {}
                    for edge_tuple, remaining in relation_chunk_updates.items():
                        if not remaining:
                            # Empty relations are deleted alongside graph edges later
                            continue
                        storage_key = make_relation_chunk_key(*edge_tuple)
                        relation_upsert_payload[storage_key] = {
                            "chunk_ids": remaining,
                            "count": len(remaining),
                            "updated_at": current_time,
                        }

                    if relation_upsert_payload:
                        await self.relation_chunks.upsert(relation_upsert_payload)

            except Exception as e:
                logger.error(f"Failed to process graph analysis results: {e}")
                raise Exception(f"Failed to process graph dependencies: {e}") from e

            # Data integrity is ensured by allowing only one process to hold pipeline at a time（no graph db lock is needed anymore)

            # 5. Delete chunks from storage
            if chunk_ids:
                try:
                    deletion_stage = "delete_chunks"
                    await self.chunks_vdb.delete(chunk_ids)
                    await self.text_chunks.delete(chunk_ids)

                    async with pipeline_status_lock:
                        log_message = (
                            f"Successfully deleted {len(chunk_ids)} chunks from storage"
                        )
                        logger.info(log_message)
                        pipeline_status["latest_message"] = log_message
                        pipeline_status["history_messages"].append(log_message)

                except Exception as e:
                    logger.error(f"Failed to delete chunks: {e}")
                    raise Exception(f"Failed to delete document chunks: {e}") from e

            # 6. Delete relationships that have no remaining sources
            if relationships_to_delete:
                try:
                    deletion_stage = "delete_relationships"
                    # Delete from relation vdb
                    rel_ids_to_delete = []
                    for src, tgt in relationships_to_delete:
                        rel_ids_to_delete.extend(
                            [
                                compute_mdhash_id(src + tgt, prefix="rel-"),
                                compute_mdhash_id(tgt + src, prefix="rel-"),
                            ]
                        )
                    await self.relationships_vdb.delete(rel_ids_to_delete)

                    # Delete from graph
                    await self.chunk_entity_relation_graph.remove_edges(
                        list(relationships_to_delete)
                    )

                    # Delete from relation_chunks storage
                    if self.relation_chunks:
                        relation_storage_keys = [
                            make_relation_chunk_key(src, tgt)
                            for src, tgt in relationships_to_delete
                        ]
                        await self.relation_chunks.delete(relation_storage_keys)

                    async with pipeline_status_lock:
                        log_message = f"Successfully deleted {len(relationships_to_delete)} relations"
                        logger.info(log_message)
                        pipeline_status["latest_message"] = log_message
                        pipeline_status["history_messages"].append(log_message)

                except Exception as e:
                    logger.error(f"Failed to delete relationships: {e}")
                    raise Exception(f"Failed to delete relationships: {e}") from e

            # 7. Delete entities that have no remaining sources
            if entities_to_delete:
                try:
                    deletion_stage = "delete_entities"
                    # Batch get all edges for entities to avoid N+1 query problem
                    nodes_edges_dict = (
                        await self.chunk_entity_relation_graph.get_nodes_edges_batch(
                            list(entities_to_delete)
                        )
                    )

                    # Debug: Check and log all edges before deleting nodes
                    edges_to_delete = set()
                    edges_still_exist = 0

                    for entity, edges in nodes_edges_dict.items():
                        if edges:
                            for src, tgt in edges:
                                # Normalize edge representation (sorted for consistency)
                                edge_tuple = tuple(sorted((src, tgt)))
                                edges_to_delete.add(edge_tuple)

                                if (
                                    src in entities_to_delete
                                    and tgt in entities_to_delete
                                ):
                                    logger.warning(
                                        f"Edge still exists: {src} <-> {tgt}"
                                    )
                                elif src in entities_to_delete:
                                    logger.warning(
                                        f"Edge still exists: {src} --> {tgt}"
                                    )
                                else:
                                    logger.warning(
                                        f"Edge still exists: {src} <-- {tgt}"
                                    )
                            edges_still_exist += 1

                    if edges_still_exist:
                        logger.warning(
                            f"⚠️ {edges_still_exist} entities still has edges before deletion"
                        )

                    # Clean residual edges from VDB and storage before deleting nodes
                    if edges_to_delete:
                        # Delete from relationships_vdb
                        rel_ids_to_delete = []
                        for src, tgt in edges_to_delete:
                            rel_ids_to_delete.extend(
                                [
                                    compute_mdhash_id(src + tgt, prefix="rel-"),
                                    compute_mdhash_id(tgt + src, prefix="rel-"),
                                ]
                            )
                        await self.relationships_vdb.delete(rel_ids_to_delete)

                        # Delete from relation_chunks storage
                        if self.relation_chunks:
                            relation_storage_keys = [
                                make_relation_chunk_key(src, tgt)
                                for src, tgt in edges_to_delete
                            ]
                            await self.relation_chunks.delete(relation_storage_keys)

                        logger.info(
                            f"Cleaned {len(edges_to_delete)} residual edges from VDB and chunk-tracking storage"
                        )

                    # Delete from graph (edges will be auto-deleted with nodes)
                    await self.chunk_entity_relation_graph.remove_nodes(
                        list(entities_to_delete)
                    )

                    # Delete from vector vdb
                    entity_vdb_ids = [
                        compute_mdhash_id(entity, prefix="ent-")
                        for entity in entities_to_delete
                    ]
                    await self.entities_vdb.delete(entity_vdb_ids)

                    # Delete from entity_chunks storage
                    if self.entity_chunks:
                        await self.entity_chunks.delete(list(entities_to_delete))

                    async with pipeline_status_lock:
                        log_message = (
                            f"Successfully deleted {len(entities_to_delete)} entities"
                        )
                        logger.info(log_message)
                        pipeline_status["latest_message"] = log_message
                        pipeline_status["history_messages"].append(log_message)

                except Exception as e:
                    logger.error(f"Failed to delete entities: {e}")
                    raise Exception(f"Failed to delete entities: {e}") from e

            # Persist changes to graph database before entity and relationship rebuild
            try:
                deletion_stage = "persist_pre_rebuild_changes"
                await self._insert_done()
            except Exception as e:
                logger.error(f"Failed to persist pre-rebuild changes: {e}")
                raise Exception(f"Failed to persist pre-rebuild changes: {e}") from e

            # 8. Rebuild entities and relationships from remaining chunks
            if entities_to_rebuild or relationships_to_rebuild:
                try:
                    deletion_stage = "rebuild_knowledge_graph"
                    await rebuild_knowledge_from_chunks(
                        entities_to_rebuild=entities_to_rebuild,
                        relationships_to_rebuild=relationships_to_rebuild,
                        knowledge_graph_inst=self.chunk_entity_relation_graph,
                        entities_vdb=self.entities_vdb,
                        relationships_vdb=self.relationships_vdb,
                        text_chunks_storage=self.text_chunks,
                        llm_response_cache=self.llm_response_cache,
                        global_config=asdict(self),
                        pipeline_status=pipeline_status,
                        pipeline_status_lock=pipeline_status_lock,
                        entity_chunks_storage=self.entity_chunks,
                        relation_chunks_storage=self.relation_chunks,
                    )

                except Exception as e:
                    logger.error(f"Failed to rebuild knowledge from chunks: {e}")
                    raise Exception(f"Failed to rebuild knowledge graph: {e}") from e

            # 9. Delete LLM cache while the document status still exists so a failure
            # remains retryable via the same doc_id.
            log_message = f"Document {doc_id} successfully deleted"
            if delete_llm_cache and doc_llm_cache_ids:
                if not self.llm_response_cache:
                    log_message = (
                        f"Cannot delete LLM cache for document {doc_id}: "
                        "cache storage is unavailable"
                    )
                    logger.error(log_message)
                    async with pipeline_status_lock:
                        pipeline_status["latest_message"] = log_message
                        pipeline_status["history_messages"].append(log_message)
                    raise Exception(log_message)
                try:
                    deletion_stage = "delete_llm_cache"
                    await self.llm_response_cache.delete(doc_llm_cache_ids)
                    # Some storage implementations do not raise on delete errors and
                    # instead only log internally, so confirm the cache entries are
                    # actually gone before deleting the document/status records.
                    remaining_cache_ids = await self._get_existing_llm_cache_ids(
                        doc_llm_cache_ids
                    )
                    if remaining_cache_ids:
                        doc_llm_cache_ids = remaining_cache_ids
                        raise Exception(
                            f"{len(remaining_cache_ids)} LLM cache entries still exist after delete"
                        )
                    cache_log_message = f"Successfully deleted {len(doc_llm_cache_ids)} LLM cache entries for document {doc_id}"
                    logger.info(cache_log_message)
                    async with pipeline_status_lock:
                        pipeline_status["latest_message"] = cache_log_message
                        pipeline_status["history_messages"].append(cache_log_message)
                    log_message = cache_log_message
                except Exception as cache_delete_error:
                    log_message = (
                        f"Failed to delete LLM cache for document {doc_id}: "
                        f"{cache_delete_error}"
                    )
                    logger.error(log_message)
                    logger.error(traceback.format_exc())
                    async with pipeline_status_lock:
                        pipeline_status["latest_message"] = log_message
                        pipeline_status["history_messages"].append(log_message)
                    raise Exception(log_message) from cache_delete_error

            # 10. Delete from full_entities and full_relations storage
            try:
                deletion_stage = "delete_doc_graph_metadata"
                await self.full_entities.delete([doc_id])
                await self.full_relations.delete([doc_id])
            except Exception as e:
                logger.error(f"Failed to delete from full_entities/full_relations: {e}")
                raise Exception(
                    f"Failed to delete from full_entities/full_relations: {e}"
                ) from e

            # 11. Delete original document and status.
            # doc_status is deleted first so that if full_docs.delete fails, a retry
            # finds no doc_status record and treats the document as already gone,
            # rather than finding a doc_status that points to a missing full_docs entry.
            try:
                deletion_stage = "delete_doc_entries"
                in_final_delete_stage = True
                await self.doc_status.delete([doc_id])
                await self.full_docs.delete([doc_id])
            except Exception as e:
                logger.error(f"Failed to delete document and status: {e}")
                raise Exception(f"Failed to delete document and status: {e}") from e

            deletion_fully_completed = True
            return DeletionResult(
                status="success",
                doc_id=doc_id,
                message=log_message,
                status_code=200,
                file_path=file_path,
            )

        except Exception as e:
            original_exception = e
            error_message = f"Error while deleting document {doc_id}: {e}"
            logger.error(error_message)
            logger.error(traceback.format_exc())
            try:
                # Do not attempt to write retry state if doc_status was already deleted:
                # upsert would re-create the record as a zombie. All earlier stages still
                # have doc_status intact and can safely update it, even if some chunk/graph
                # data has already been removed.
                if doc_status_data is not None and not in_final_delete_stage:
                    doc_status_data = await self._update_delete_retry_state(
                        doc_id,
                        doc_status_data,
                        deletion_stage=deletion_stage,
                        doc_llm_cache_ids=doc_llm_cache_ids,
                        error_message=error_message,
                        failed=True,
                    )
            except Exception as status_update_error:
                logger.error(
                    "Failed to update deletion retry state for document %s: %s",
                    doc_id,
                    status_update_error,
                )
                logger.error(traceback.format_exc())
                error_message = (
                    f"{error_message}. Additionally, failed to persist retry state: "
                    f"{status_update_error}. Manual cleanup may be required."
                )
            return DeletionResult(
                status="fail",
                doc_id=doc_id,
                message=error_message,
                status_code=500,
                file_path=file_path,
            )

        finally:
            # ALWAYS ensure persistence if any deletion operations were started
            if deletion_operations_started:
                try:
                    await self._insert_done()
                except Exception as persistence_error:
                    persistence_error_msg = f"Failed to persist data after deletion attempt for {doc_id}: {persistence_error}"
                    logger.error(persistence_error_msg)
                    logger.error(traceback.format_exc())

                    if deletion_fully_completed:
                        # All deletion stages succeeded; the flush error is a post-cleanup
                        # concern. Do not override the success result already returned.
                        logger.error(
                            "Post-deletion persistence flush failed for %s, "
                            "but deletion completed successfully: %s",
                            doc_id,
                            persistence_error,
                        )
                    elif original_exception is None:
                        # Deletion stages were in-flight but the try-block return was never
                        # reached; treat the persistence failure as the primary error.
                        return DeletionResult(
                            status="fail",
                            doc_id=doc_id,
                            message=f"Deletion completed but failed to persist changes: {persistence_error}",
                            status_code=500,
                            file_path=file_path,
                        )
                    # If there was an original exception, log the persistence error but
                    # don't override it — the original error result was already returned.
            else:
                logger.debug(
                    f"No deletion operations were started for document {doc_id}, skipping persistence"
                )

            # Release pipeline only if WE acquired it
            if we_acquired_pipeline:
                async with pipeline_status_lock:
                    pipeline_status["busy"] = False
                    pipeline_status["cancellation_requested"] = False
                    completion_msg = (
                        f"Deletion process completed for document: {doc_id}"
                    )
                    pipeline_status["latest_message"] = completion_msg
                    pipeline_status["history_messages"].append(completion_msg)
                    logger.info(completion_msg)

    async def adelete_by_entity(self, entity_name: str) -> DeletionResult:
        """Asynchronously delete an entity and all its relationships.

        Args:
            entity_name: Name of the entity to delete.

        Returns:
            DeletionResult: An object containing the outcome of the deletion process.
        """
        from lightrag.utils_graph import adelete_by_entity

        return await adelete_by_entity(
            self.chunk_entity_relation_graph,
            self.entities_vdb,
            self.relationships_vdb,
            entity_name,
        )

    def delete_by_entity(self, entity_name: str) -> DeletionResult:
        """Synchronously delete an entity and all its relationships.

        Args:
            entity_name: Name of the entity to delete.

        Returns:
            DeletionResult: An object containing the outcome of the deletion process.
        """
        loop = always_get_an_event_loop()
        return loop.run_until_complete(self.adelete_by_entity(entity_name))

    async def adelete_by_relation(
        self, source_entity: str, target_entity: str
    ) -> DeletionResult:
        """Asynchronously delete a relation between two entities.

        Args:
            source_entity: Name of the source entity.
            target_entity: Name of the target entity.

        Returns:
            DeletionResult: An object containing the outcome of the deletion process.
        """
        from lightrag.utils_graph import adelete_by_relation

        return await adelete_by_relation(
            self.chunk_entity_relation_graph,
            self.relationships_vdb,
            source_entity,
            target_entity,
        )

    def delete_by_relation(
        self, source_entity: str, target_entity: str
    ) -> DeletionResult:
        """Synchronously delete a relation between two entities.

        Args:
            source_entity: Name of the source entity.
            target_entity: Name of the target entity.

        Returns:
            DeletionResult: An object containing the outcome of the deletion process.
        """
        loop = always_get_an_event_loop()
        return loop.run_until_complete(
            self.adelete_by_relation(source_entity, target_entity)
        )

    async def get_processing_status(self) -> dict[str, int]:
        """Get current document processing status counts

        Returns:
            Dict with counts for each status
        """
        return await self.doc_status.get_status_counts()

    async def aget_docs_by_track_id(
        self, track_id: str
    ) -> dict[str, DocProcessingStatus]:
        """Get documents by track_id

        Args:
            track_id: The tracking ID to search for

        Returns:
            Dict with document id as keys and document status as values
        """
        return await self.doc_status.get_docs_by_track_id(track_id)

    async def get_entity_info(
        self, entity_name: str, include_vector_data: bool = False
    ) -> dict[str, str | None | dict[str, str]]:
        """Get detailed information of an entity"""
        from lightrag.utils_graph import get_entity_info

        return await get_entity_info(
            self.chunk_entity_relation_graph,
            self.entities_vdb,
            entity_name,
            include_vector_data,
        )

    async def get_relation_info(
        self, src_entity: str, tgt_entity: str, include_vector_data: bool = False
    ) -> dict[str, str | None | dict[str, str]]:
        """Get detailed information of a relationship"""
        from lightrag.utils_graph import get_relation_info

        return await get_relation_info(
            self.chunk_entity_relation_graph,
            self.relationships_vdb,
            src_entity,
            tgt_entity,
            include_vector_data,
        )

    async def aedit_entity(
        self,
        entity_name: str,
        updated_data: dict[str, str],
        allow_rename: bool = True,
        allow_merge: bool = False,
    ) -> dict[str, Any]:
        """Asynchronously edit entity information.

        Updates entity information in the knowledge graph and re-embeds the entity in the vector database.
        Also synchronizes entity_chunks_storage and relation_chunks_storage to track chunk references.

        Args:
            entity_name: Name of the entity to edit
            updated_data: Dictionary containing updated attributes, e.g. {"description": "new description", "entity_type": "new type"}
            allow_rename: Whether to allow entity renaming, defaults to True
            allow_merge: Whether to merge into an existing entity when renaming to an existing name

        Returns:
            Dictionary containing updated entity information
        """
        from lightrag.utils_graph import aedit_entity

        return await aedit_entity(
            self.chunk_entity_relation_graph,
            self.entities_vdb,
            self.relationships_vdb,
            entity_name,
            updated_data,
            allow_rename,
            allow_merge,
            self.entity_chunks,
            self.relation_chunks,
        )

    def edit_entity(
        self,
        entity_name: str,
        updated_data: dict[str, str],
        allow_rename: bool = True,
        allow_merge: bool = False,
    ) -> dict[str, Any]:
        loop = always_get_an_event_loop()
        return loop.run_until_complete(
            self.aedit_entity(entity_name, updated_data, allow_rename, allow_merge)
        )

    async def aedit_relation(
        self, source_entity: str, target_entity: str, updated_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Asynchronously edit relation information.

        Updates relation (edge) information in the knowledge graph and re-embeds the relation in the vector database.
        Also synchronizes the relation_chunks_storage to track which chunks reference this relation.

        Args:
            source_entity: Name of the source entity
            target_entity: Name of the target entity
            updated_data: Dictionary containing updated attributes, e.g. {"description": "new description", "keywords": "new keywords"}

        Returns:
            Dictionary containing updated relation information
        """
        from lightrag.utils_graph import aedit_relation

        return await aedit_relation(
            self.chunk_entity_relation_graph,
            self.entities_vdb,
            self.relationships_vdb,
            source_entity,
            target_entity,
            updated_data,
            self.relation_chunks,
        )

    def edit_relation(
        self, source_entity: str, target_entity: str, updated_data: dict[str, Any]
    ) -> dict[str, Any]:
        loop = always_get_an_event_loop()
        return loop.run_until_complete(
            self.aedit_relation(source_entity, target_entity, updated_data)
        )

    async def acreate_entity(
        self, entity_name: str, entity_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Asynchronously create a new entity.

        Creates a new entity in the knowledge graph and adds it to the vector database.

        Args:
            entity_name: Name of the new entity
            entity_data: Dictionary containing entity attributes, e.g. {"description": "description", "entity_type": "type"}

        Returns:
            Dictionary containing created entity information
        """
        from lightrag.utils_graph import acreate_entity

        return await acreate_entity(
            self.chunk_entity_relation_graph,
            self.entities_vdb,
            self.relationships_vdb,
            entity_name,
            entity_data,
        )

    def create_entity(
        self, entity_name: str, entity_data: dict[str, Any]
    ) -> dict[str, Any]:
        loop = always_get_an_event_loop()
        return loop.run_until_complete(self.acreate_entity(entity_name, entity_data))

    async def acreate_relation(
        self, source_entity: str, target_entity: str, relation_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Asynchronously create a new relation between entities.

        Creates a new relation (edge) in the knowledge graph and adds it to the vector database.

        Args:
            source_entity: Name of the source entity
            target_entity: Name of the target entity
            relation_data: Dictionary containing relation attributes, e.g. {"description": "description", "keywords": "keywords"}

        Returns:
            Dictionary containing created relation information
        """
        from lightrag.utils_graph import acreate_relation

        return await acreate_relation(
            self.chunk_entity_relation_graph,
            self.entities_vdb,
            self.relationships_vdb,
            source_entity,
            target_entity,
            relation_data,
        )

    def create_relation(
        self, source_entity: str, target_entity: str, relation_data: dict[str, Any]
    ) -> dict[str, Any]:
        loop = always_get_an_event_loop()
        return loop.run_until_complete(
            self.acreate_relation(source_entity, target_entity, relation_data)
        )

    async def amerge_entities(
        self,
        source_entities: list[str],
        target_entity: str,
        merge_strategy: dict[str, str] = None,
        target_entity_data: dict[str, Any] = None,
    ) -> dict[str, Any]:
        """Asynchronously merge multiple entities into one entity.

        Merges multiple source entities into a target entity, handling all relationships,
        and updating both the knowledge graph and vector database.

        Args:
            source_entities: List of source entity names to merge
            target_entity: Name of the target entity after merging
            merge_strategy: Merge strategy configuration, e.g. {"description": "concatenate", "entity_type": "keep_first"}
                Supported strategies:
                - "concatenate": Concatenate all values (for text fields)
                - "keep_first": Keep the first non-empty value
                - "keep_last": Keep the last non-empty value
                - "join_unique": Join all unique values (for fields separated by delimiter)
            target_entity_data: Dictionary of specific values to set for the target entity,
                overriding any merged values, e.g. {"description": "custom description", "entity_type": "PERSON"}

        Returns:
            Dictionary containing the merged entity information
        """
        from lightrag.utils_graph import amerge_entities

        return await amerge_entities(
            self.chunk_entity_relation_graph,
            self.entities_vdb,
            self.relationships_vdb,
            source_entities,
            target_entity,
            merge_strategy,
            target_entity_data,
            self.entity_chunks,
            self.relation_chunks,
        )

    def merge_entities(
        self,
        source_entities: list[str],
        target_entity: str,
        merge_strategy: dict[str, str] = None,
        target_entity_data: dict[str, Any] = None,
    ) -> dict[str, Any]:
        loop = always_get_an_event_loop()
        return loop.run_until_complete(
            self.amerge_entities(
                source_entities, target_entity, merge_strategy, target_entity_data
            )
        )

    async def aexport_data(
        self,
        output_path: str,
        file_format: Literal["csv", "excel", "md", "txt"] = "csv",
        include_vector_data: bool = False,
    ) -> None:
        """
        Asynchronously exports all entities, relations, and relationships to various formats.
        Args:
            output_path: The path to the output file (including extension).
            file_format: Output format - "csv", "excel", "md", "txt".
                - csv: Comma-separated values file
                - excel: Microsoft Excel file with multiple sheets
                - md: Markdown tables
                - txt: Plain text formatted output
                - table: Print formatted tables to console
            include_vector_data: Whether to include data from the vector database.
        """
        from lightrag.utils import aexport_data as utils_aexport_data

        await utils_aexport_data(
            self.chunk_entity_relation_graph,
            self.entities_vdb,
            self.relationships_vdb,
            output_path,
            file_format,
            include_vector_data,
        )

    def export_data(
        self,
        output_path: str,
        file_format: Literal["csv", "excel", "md", "txt"] = "csv",
        include_vector_data: bool = False,
    ) -> None:
        """
        Synchronously exports all entities, relations, and relationships to various formats.
        Args:
            output_path: The path to the output file (including extension).
            file_format: Output format - "csv", "excel", "md", "txt".
                - csv: Comma-separated values file
                - excel: Microsoft Excel file with multiple sheets
                - md: Markdown tables
                - txt: Plain text formatted output
                - table: Print formatted tables to console
            include_vector_data: Whether to include data from the vector database.
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        loop.run_until_complete(
            self.aexport_data(output_path, file_format, include_vector_data)
        )
