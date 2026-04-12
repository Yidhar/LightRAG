"""Image retrieval routes for the LightRAG multimodal pipeline.

These endpoints expose the contents of the ``image_blob_store`` and
``image_metadata`` storages that Phases 2–3 added to LightRAG. They let
WebUI / downstream consumers:

- Fetch the original bytes of any image ingested via ``ainsert_image``
  (the LLM context emits ``[img-<hash>]`` citations; the front-end calls
  ``GET /images/{blob_id}`` to render them).
- Read the structured caption JSON + source-doc backlink for a blob.

The endpoints are only registered when the multimodal pipeline is enabled
on the LightRAG instance (``rag.image_blob_store is not None``). On
text-only deployments the router is a no-op — its ``create_image_routes``
factory returns ``None`` so callers can gate inclusion.
"""

from __future__ import annotations

import mimetypes
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from lightrag import LightRAG
from lightrag.api.utils_api import get_combined_auth_dependency
from lightrag.utils import logger


# ----------------------------- response models -----------------------------


class ImageMetadataResponse(BaseModel):
    """Sidecar record for a single image blob."""

    blob_id: str
    blob_ref: Optional[str] = None
    content_type: Optional[str] = None
    source_doc_id: Optional[str] = None
    source_file_path: Optional[str] = None
    source_page: Optional[int] = None
    source_printed_page: Optional[int] = None
    source_page_label: Optional[str] = None
    source_bbox: Optional[dict[str, Any]] = None
    picture_index: Optional[int] = None
    page_picture_index: Optional[int] = None
    context_text: Optional[str] = None
    context_chunk_ids: Optional[list[str]] = None
    context_chunks: Optional[list[dict[str, Any]]] = None
    extraction_mode: Optional[str] = None
    native_xref: Optional[int] = None
    merged_extraction_modes: Optional[list[str]] = None
    annotation_text: Optional[str] = None
    caption_json: Optional[dict[str, Any]] = None
    extra: Optional[dict[str, Any]] = None


# --------------------------------- router ---------------------------------


def create_image_routes(
    rag: LightRAG, api_key: Optional[str] = None
) -> Optional[APIRouter]:
    """Build the ``/images`` router.

    Returns ``None`` when the multimodal pipeline is not configured on the
    given ``rag`` instance, so the caller can skip registering it without
    special-casing.
    """
    if rag.image_blob_store is None or rag.image_metadata is None:
        logger.debug(
            "Skipping image routes: multimodal pipeline not enabled on this "
            "LightRAG instance (image_embedding_func is None)."
        )
        return None

    router = APIRouter(prefix="/images", tags=["images"])
    combined_auth = get_combined_auth_dependency(api_key)

    @router.get("/{blob_id}", dependencies=[Depends(combined_auth)])
    async def get_image_blob(blob_id: str):
        """Return the original bytes of an image blob.

        The response ``Content-Type`` is taken from the sidecar metadata
        (falling back to ``application/octet-stream``). Filesystem blob
        storage serves the file via FastAPI's ``FileResponse`` so large
        binaries do not need to load into Python memory.
        """
        if not blob_id.startswith("img-"):
            raise HTTPException(
                status_code=400,
                detail="blob_id must start with 'img-' (content-hashed image id)",
            )

        # Try to read sidecar first to get the content type.
        sidecar = await rag.image_blob_store.get_metadata(blob_id)
        content_type = (
            sidecar.get("content_type")
            if sidecar
            else "application/octet-stream"
        )

        # Fast path: filesystem backend — serve via FileResponse so FastAPI
        # streams the file with sendfile/zero-copy.
        ref = await rag.image_blob_store.get_reference(blob_id)
        if ref and os.path.isfile(ref):
            filename = os.path.basename(ref)
            return FileResponse(
                ref,
                media_type=content_type or mimetypes.guess_type(ref)[0] or "application/octet-stream",
                filename=filename,
            )

        # Generic fallback: load bytes into memory and return them.
        data = await rag.image_blob_store.get(blob_id)
        if data is None:
            raise HTTPException(
                status_code=404,
                detail=f"Image blob not found: {blob_id}",
            )
        return Response(
            content=data,
            media_type=content_type or "application/octet-stream",
        )

    @router.get(
        "/{blob_id}/metadata",
        response_model=ImageMetadataResponse,
        dependencies=[Depends(combined_auth)],
    )
    async def get_image_metadata(blob_id: str) -> ImageMetadataResponse:
        """Return the sidecar record for an image blob.

        The record includes the structured caption JSON produced by the
        vision model during ``ainsert_image``, the source-document backlink
        (for PDF-extracted images — Phase 5), and any user-supplied extras
        passed via ``extra_metadata``.
        """
        if not blob_id.startswith("img-"):
            raise HTTPException(
                status_code=400,
                detail="blob_id must start with 'img-' (content-hashed image id)",
            )

        # The image_metadata KV store holds the semantic record written by
        # ainsert_image (annotation_text, caption_json, source_doc_id, ...).
        kv_record = await rag.image_metadata.get_by_id(blob_id)
        if kv_record is None:
            # Fall back to blob_store's sidecar-only metadata if the KV
            # record is missing (stale state, partial restore, etc).
            fs_side = await rag.image_blob_store.get_metadata(blob_id)
            if fs_side is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Image metadata not found: {blob_id}",
                )
            fs_extra = fs_side.get("extra") or {}
            return ImageMetadataResponse(
                blob_id=blob_id,
                content_type=fs_side.get("content_type"),
                source_page=fs_extra.get("source_page"),
                source_printed_page=fs_extra.get("source_printed_page"),
                source_page_label=fs_extra.get("source_page_label"),
                source_bbox=fs_extra.get("source_bbox"),
                picture_index=fs_extra.get("picture_index"),
                page_picture_index=fs_extra.get("page_picture_index"),
                context_text=fs_extra.get("context_text"),
                context_chunk_ids=fs_extra.get("context_chunk_ids"),
                context_chunks=fs_extra.get("context_chunks"),
                extraction_mode=fs_extra.get("extraction_mode"),
                native_xref=fs_extra.get("native_xref"),
                merged_extraction_modes=fs_extra.get("merged_extraction_modes"),
                extra=fs_extra,
            )

        extra = kv_record.get("extra") or {}
        return ImageMetadataResponse(
            blob_id=blob_id,
            blob_ref=kv_record.get("blob_ref"),
            content_type=kv_record.get("content_type"),
            source_doc_id=kv_record.get("source_doc_id"),
            source_file_path=kv_record.get("source_file_path"),
            source_page=kv_record.get("source_page", extra.get("source_page")),
            source_printed_page=kv_record.get(
                "source_printed_page", extra.get("source_printed_page")
            ),
            source_page_label=kv_record.get(
                "source_page_label", extra.get("source_page_label")
            ),
            source_bbox=kv_record.get("source_bbox", extra.get("source_bbox")),
            picture_index=kv_record.get("picture_index", extra.get("picture_index")),
            page_picture_index=kv_record.get(
                "page_picture_index", extra.get("page_picture_index")
            ),
            context_text=kv_record.get("context_text")
            or extra.get("context_text")
            or extra.get("page_text_excerpt"),
            context_chunk_ids=kv_record.get("context_chunk_ids")
            or extra.get("context_chunk_ids"),
            context_chunks=kv_record.get("context_chunks")
            or extra.get("context_chunks"),
            extraction_mode=kv_record.get("extraction_mode")
            or extra.get("extraction_mode"),
            native_xref=kv_record.get("native_xref") or extra.get("native_xref"),
            merged_extraction_modes=kv_record.get("merged_extraction_modes")
            or extra.get("merged_extraction_modes"),
            annotation_text=kv_record.get("annotation_text"),
            caption_json=kv_record.get("caption_json"),
            extra=extra,
        )

    return router
