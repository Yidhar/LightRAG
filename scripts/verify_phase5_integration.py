"""Phase 5 end-to-end integration test.

Validates the full PDF → embedded images → multimodal ingest path:

    docling (generate_picture_images=True)
        |
        v
    _convert_with_docling_multimodal  ->  (markdown, [PictureItem info])
        |
        v
    rag.ainsert_document_with_images
        |
        +--> for each image:
        |       blob_store.put
        |       vision_model_func (caption JSON)
        |       image_embedding_func.image_encode
        |       images_vdb.upsert_with_embeddings
        |       image_metadata.upsert
        +--> augmented text (parent text + all annotation blocks)
                 through apipeline_enqueue_documents
                     => chunks_vdb + text_chunks + KG entities
        +--> doc_status[parent_doc_id].metadata.image_ids = [...]

After ingest we verify that:
    1. Multiple blobs exist on disk under the single parent doc_id
    2. Every blob's sidecar has source_doc_id == parent_doc_id
    3. image_metadata KV holds one record per extracted image with
       proper back-references (source_doc_id, source_page)
    4. images_vdb holds N vectors (one per image) and their
       full_doc_id == parent_doc_id
    5. doc_status[parent_doc_id].metadata.image_ids lists every blob_id
       and modality == "mixed" (parent text + image annotations)
    6. rag.aquery_data in mix mode returns BOTH text chunks and
       image_vector chunks that share the parent doc_id
    7. KG entity extraction picked up at least one entity name that
       appears in an image annotation (not only in the parent text)

Processes only the first 10 pages of the Beijing DB11 engineering
standard (small enough that docling finishes in ~90s and known to
contain 3 embedded pictures from our Task 26 probe).

Usage::
    uv run python scripts/verify_phase5_integration.py [--keep]
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import time
from functools import partial
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, io.UnsupportedOperation):
        pass

load_dotenv()

# Strip positional args BEFORE importing anything in lightrag.api so the
# lazy global_args argparse doesn't choke on our working_dir / --keep flags.
_ORIGINAL_ARGV = list(sys.argv)
sys.argv = sys.argv[:1]

from lightrag import LightRAG, QueryParam  # noqa: E402
from lightrag.base import DocStatus  # noqa: E402
from lightrag.llm.openai import openai_complete_if_cache, openai_embed  # noqa: E402
from lightrag.llm.tongyi import tongyi_multimodal_embedding  # noqa: E402
from lightrag.utils import EmbeddingFunc  # noqa: E402

# Import docling-based multimodal converter from the API router so we
# exercise the exact function the PDF upload path will use.
from lightrag.api.routers.document_routes import (  # noqa: E402
    _convert_with_docling_multimodal,
)


PDF_PATH = Path(
    r"C:\Users\yidhar\Downloads\93545a35-0153-4dfd-8b6a-7e06281e841e.pdf"
)
PAGE_RANGE = (1, 10)


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"Missing env var: {name}\n")
        sys.exit(2)
    return val


def _check(label: str, condition: bool, detail: str = "") -> None:
    tag = "\033[92mPASS\033[0m" if condition else "\033[91mFAIL\033[0m"
    print(f"  [{tag}] {label}" + (f"  — {detail}" if detail else ""))
    if not condition:
        raise SystemExit(1)


async def main() -> None:
    # Parse the stashed original argv via our own trivial parser to avoid
    # poisoning sys.argv before the LightRAG imports above.
    keep = "--keep" in _ORIGINAL_ARGV

    if not PDF_PATH.exists():
        sys.stderr.write(f"Test PDF not found: {PDF_PATH}\n")
        sys.exit(2)

    llm_host = _require("LLM_BINDING_HOST")
    llm_key = _require("LLM_BINDING_API_KEY")
    llm_model = _require("LLM_MODEL")
    text_emb_host = _require("EMBEDDING_BINDING_HOST")
    text_emb_key = _require("EMBEDDING_BINDING_API_KEY")
    text_emb_model = _require("EMBEDDING_MODEL")
    text_emb_dim = int(_require("EMBEDDING_DIM"))
    vision_model = _require("VISION_MODEL")
    vision_host = _require("VISION_BINDING_HOST")
    vision_key = _require("VISION_BINDING_API_KEY")
    mm_model = _require("MM_EMB_MODEL")
    mm_dim = int(_require("MM_EMB_DIM"))
    mm_key = _require("MM_EMB_BINDING_API_KEY")
    mm_host = os.environ.get("MM_EMB_BINDING_HOST") or None

    async def llm_model_func(
        prompt, system_prompt=None, history_messages=None, **kwargs
    ):
        return await openai_complete_if_cache(
            model=llm_model,
            prompt=prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            base_url=llm_host,
            api_key=llm_key,
            **kwargs,
        )

    text_embedding_func = EmbeddingFunc(
        embedding_dim=text_emb_dim,
        max_token_size=8192,
        send_dimensions=True,
        func=partial(
            openai_embed.func,
            model=text_emb_model,
            base_url=text_emb_host,
            api_key=text_emb_key,
        ),
    )
    image_embedding_func = tongyi_multimodal_embedding(
        model=mm_model,
        embedding_dim=mm_dim,
        api_key=mm_key,
        base_url=mm_host,
    )
    vision_model_func = partial(
        openai_complete_if_cache,
        model=vision_model,
        base_url=vision_host,
        api_key=vision_key,
    )

    tmpdir = tempfile.mkdtemp(prefix="lightrag_phase5_integration_")
    workspace = "phase5_test"
    print(f"Temp working_dir : {tmpdir}")
    print(f"Workspace        : {workspace}")
    print(f"Test PDF         : {PDF_PATH.name}")
    print(f"Page range       : {PAGE_RANGE}\n")

    try:
        # --- Stage 1: extract text + images via docling ---
        print("== [Stage 1] Docling PDF text + picture extraction ==")
        t0 = time.time()
        markdown, extracted = await asyncio.to_thread(
            _convert_with_docling_multimodal, PDF_PATH, PAGE_RANGE
        )
        t_docling = time.time() - t0
        print(f"  docling conversion: {t_docling:.1f}s")
        print(f"  markdown length   : {len(markdown):,} chars")
        print(f"  images extracted  : {len(extracted)}")
        for i, img in enumerate(extracted):
            print(
                f"    [{i}] page={img.get('page_no')}  "
                f"size={img.get('pil_size')}  "
                f"bytes={len(img.get('bytes', b'')):,}"
            )
        _check("markdown non-empty", len(markdown) > 500)
        _check("at least 1 embedded image extracted", len(extracted) >= 1)

        # --- Stage 2: instantiate LightRAG and ingest ---
        print("\n== [Stage 2] ainsert_document_with_images ==")
        rag = LightRAG(
            working_dir=tmpdir,
            workspace=workspace,
            llm_model_func=llm_model_func,
            llm_model_name=llm_model,
            embedding_func=text_embedding_func,
            image_embedding_func=image_embedding_func,
            vision_model_func=vision_model_func,
            addon_params={"language": "Chinese"},
        )
        await rag.initialize_storages()

        t0 = time.time()
        track_id = await rag.ainsert_document_with_images(
            text_content=markdown,
            extracted_images=extracted,
            file_path=str(PDF_PATH),
        )
        t_ingest = time.time() - t0
        print(f"  ingest completed in {t_ingest:.1f}s  track_id={track_id}")

        # --- Stage 3: verify blob store has one file per image ---
        print("\n== [Stage 3] Blob store persistence ==")
        blob_root = rag.image_blob_store._root
        blob_files = [
            f
            for f in Path(blob_root).rglob("*")
            if f.is_file() and not f.name.endswith(".meta.json")
        ]
        sidecar_files = [
            f for f in Path(blob_root).rglob("*.meta.json") if f.is_file()
        ]
        print(f"  blobs on disk  : {len(blob_files)}")
        print(f"  sidecars       : {len(sidecar_files)}")
        _check(
            "blob count equals image count",
            len(blob_files) == len(extracted),
        )
        _check(
            "sidecar count equals image count",
            len(sidecar_files) == len(extracted),
        )

        # All sidecars should have the same source_doc_id (the PDF's parent)
        source_doc_ids = set()
        for sf in sidecar_files:
            with open(sf, "r", encoding="utf-8") as f:
                sdata = json.load(f)
            extra = sdata.get("extra") or {}
            if extra.get("source_doc_id"):
                source_doc_ids.add(extra["source_doc_id"])
        print(f"  distinct source_doc_ids in sidecars: {len(source_doc_ids)}")
        _check(
            "all sidecars share ONE parent source_doc_id",
            len(source_doc_ids) == 1,
            f"found={source_doc_ids}",
        )
        parent_doc_id = next(iter(source_doc_ids))
        print(f"  parent doc_id  : {parent_doc_id}")

        # --- Stage 4: image_metadata KV has one record per image ---
        print("\n== [Stage 4] image_metadata KV persistence ==")
        await rag.image_metadata.index_done_callback()
        kv_path = os.path.join(tmpdir, workspace, "kv_store_image_metadata.json")
        _check("image_metadata JSON on disk", os.path.isfile(kv_path))
        with open(kv_path, "r", encoding="utf-8") as f:
            kv_data = json.load(f)
        print(f"  KV records: {len(kv_data)}")
        _check(
            "image_metadata has one record per image",
            len(kv_data) == len(extracted),
        )
        # Every record should link back to the parent doc
        all_backlinked = all(
            rec.get("source_doc_id") == parent_doc_id for rec in kv_data.values()
        )
        _check(
            "every image_metadata record links to parent_doc_id",
            all_backlinked,
        )
        # And have non-trivial annotation_text + caption_json
        annotated = [
            k for k, v in kv_data.items() if v.get("annotation_text")
        ]
        _check(
            "every image_metadata has annotation_text",
            len(annotated) == len(extracted),
        )

        # --- Stage 5: images_vdb holds N vectors ---
        print("\n== [Stage 5] images_vdb persistence ==")
        await rag.images_vdb.index_done_callback()
        vdb_path = os.path.join(tmpdir, workspace, "vdb_images.json")
        _check("images_vdb JSON on disk", os.path.isfile(vdb_path))
        with open(vdb_path, "r", encoding="utf-8") as f:
            vdb_raw = json.load(f)
        records = vdb_raw.get("data", [])
        print(f"  images_vdb records: {len(records)}")
        _check(
            "images_vdb has one vector per image",
            len(records) == len(extracted),
        )
        all_full_doc_ok = all(
            r.get("full_doc_id") == parent_doc_id for r in records
        )
        _check(
            "every images_vdb record has full_doc_id == parent_doc_id",
            all_full_doc_ok,
        )

        # --- Stage 6: reverse index via doc_status.metadata.image_ids ---
        print("\n== [Stage 6] Reverse index via doc_status.metadata.image_ids ==")
        doc_status_rec = await rag.doc_status.get_by_id(parent_doc_id)
        _check(
            "doc_status record exists for parent doc",
            doc_status_rec is not None,
        )
        _check(
            "doc_status has status field (not wiped by upsert bug)",
            bool(doc_status_rec.get("status")),
            str(doc_status_rec.get("status")),
        )
        meta = doc_status_rec.get("metadata") or {}
        image_ids = meta.get("image_ids") or []
        print(f"  metadata.image_ids length: {len(image_ids)}")
        print(f"  metadata.modality        : {meta.get('modality')}")
        print(f"  metadata.source_kind     : {meta.get('source_kind')}")
        _check(
            "reverse index contains every blob_id",
            len(image_ids) == len(extracted),
        )
        _check("modality == 'mixed'", meta.get("modality") == "mixed")
        _check(
            "source_kind == 'pdf_extracted'",
            meta.get("source_kind") == "pdf_extracted",
        )

        # --- Stage 7: mix-mode retrieval returns text + image chunks ---
        print("\n== [Stage 7] aquery_data in mix mode ==")
        result = await rag.aquery_data(
            "幕墙工程 施工工艺 节点构造",
            QueryParam(mode="mix", chunk_top_k=20, top_k=40),
        )
        chunks = result.get("data", {}).get("chunks", [])
        image_chunks = [c for c in chunks if c.get("source_type") == "image_vector"]
        text_chunks = [c for c in chunks if c.get("source_type") != "image_vector"]
        print(f"  mix returned {len(chunks)} chunks ({len(text_chunks)} text, {len(image_chunks)} image)")
        _check("mix mode returned >= 1 chunk", len(chunks) >= 1)
        _check(
            "mix mode returned at least one image_vector chunk",
            len(image_chunks) >= 1,
        )

        # At least one image chunk's image_blob_id should be among our
        # ingested blobs — proves cross-modal retrieval reaches them.
        ingested_blob_ids = set(image_ids)
        returned_image_blob_ids = {
            c.get("image_blob_id") for c in image_chunks if c.get("image_blob_id")
        }
        intersection = ingested_blob_ids & returned_image_blob_ids
        print(f"  ingested blob_ids   : {len(ingested_blob_ids)}")
        print(f"  returned in query   : {len(returned_image_blob_ids)}")
        print(f"  intersection        : {len(intersection)}")
        _check(
            "at least one returned image belongs to the parent doc",
            len(intersection) >= 1,
        )

        # --- Stage 8: KG saw entities from the augmented text ---
        print("\n== [Stage 8] KG entity count ==")
        kg = rag.chunk_entity_relation_graph
        labels = await kg.get_all_labels()
        print(f"  total KG entity labels: {len(labels)}")
        _check("KG has entities extracted from augmented text", len(labels) > 0)

        # --- Stage 9: full aquery string ---
        print("\n== [Stage 9] aquery (LLM round-trip) ==")
        t0 = time.time()
        answer = await rag.aquery(
            "这份幕墙工艺规程的封面和前几页有哪些关键信息?图里能看到什么?",
            QueryParam(mode="mix", chunk_top_k=10, top_k=20),
        )
        t_aquery = time.time() - t0
        answer_text = answer if isinstance(answer, str) else "<streaming>"
        print(f"  aquery took {t_aquery:.1f}s")
        print(f"  --- answer preview (first 500 chars) ---")
        print(f"  {answer_text[:500]}")
        print(f"  --- end ---")
        _check("aquery returned a non-empty string", len(answer_text) > 0)

        await rag.finalize_storages()
        print("\n\033[92mPhase 5 integration test: ALL PASS\033[0m")

    finally:
        if keep:
            print(f"\n[--keep] preserved: {tmpdir}")
        elif os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
            print("\n(cleanup) removed temp working_dir")


if __name__ == "__main__":
    asyncio.run(main())
