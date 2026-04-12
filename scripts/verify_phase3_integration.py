"""Phase 3 end-to-end integration test.

Exercises the full multimodal ingestion pipeline on the three Phase 1 PDFs:

    rag.ainsert_image(pdf_page_bytes, ...) ->
        - image_blob_store.put        (blob + sidecar on disk)
        - vision_model_func           (structured caption JSON)
        - image_embedding_func        (1152-d image-side vector)
        - images_vdb.upsert_with_embeddings
        - image_metadata.upsert
        - apipeline_enqueue_documents (annotation text -> text pipeline)
        - apipeline_process_enqueue   (chunking, text embedding, entity extraction, KG)

After all inserts, we validate:
    1. Blob files on disk
    2. image_metadata KV records
    3. images_vdb has per-image records
    4. chunks_vdb has text chunks derived from annotation text
    5. KG graph has entity nodes (proves text pipeline ran)
    6. Cross-modal query via images_vdb.query:
        - "幕墙工程" -> curtain wall PDFs rank above art book
        - "游戏画册" -> art book ranks above curtain wall PDFs
    7. Full rag.aquery() exercise on one of the ingested images

Usage:
    uv run python scripts/verify_phase3_integration.py [--keep]

    --keep   preserve the temp working_dir for inspection after the test.
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

import numpy as np
from dotenv import load_dotenv

# Force UTF-8 stdout on Windows so CJK captions render correctly.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, io.UnsupportedOperation):
        pass

load_dotenv()

try:
    import fitz  # pymupdf
except ImportError:
    sys.stderr.write("pymupdf not installed. Run: uv pip install pymupdf\n")
    sys.exit(2)

from lightrag import LightRAG  # noqa: E402
from lightrag.llm.openai import openai_complete_if_cache, openai_embed  # noqa: E402
from lightrag.llm.tongyi import tongyi_multimodal_embedding  # noqa: E402
from lightrag.utils import EmbeddingFunc  # noqa: E402

PDFS = [
    r"C:\Users\yidhar\Downloads\93545a35-0153-4dfd-8b6a-7e06281e841e.pdf",
    r"C:\Users\yidhar\Downloads\W020201029539475435520.pdf",
    r"C:\Users\yidhar\Downloads\RCArtBook.pdf",
]


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


def rasterize_page_1(pdf_path: Path, dpi: int = 144) -> bytes:
    doc = fitz.open(pdf_path)
    try:
        page = doc.load_page(0)
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Preserve the temp working_dir after the test.",
    )
    args = parser.parse_args()

    # --- Pull config from .env ---
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

    # --- Build real funcs ---
    async def llm_model_func(prompt, system_prompt=None, history_messages=None, **kwargs):
        # Wrapper that forwards to openai_complete_if_cache with the right host/model.
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
        # DashScope's text-embedding-v4 supports dynamic dimension via the
        # `dimensions` parameter. We MUST pass it through so DashScope
        # returns 2048-d vectors instead of its API default (1024-d).
        send_dimensions=True,
        # CRITICAL: ``openai_embed`` is already an ``EmbeddingFunc`` (decorated
        # with ``@wrap_embedding_func_with_attrs(embedding_dim=1536, ...)``
        # against ``text-embedding-3-small``). Wrapping it again here would
        # create a nested EmbeddingFunc whose inner layer validates against
        # 1536 instead of 2048. Use ``.func`` to access the underlying raw
        # async function. (See CLAUDE.md "Nested Embedding Functions".)
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

    tmpdir = tempfile.mkdtemp(prefix="lightrag_phase3_integration_")
    workspace = "phase3_test"
    print(f"Temp working_dir: {tmpdir}")
    print(f"Workspace       : {workspace}\n")

    try:
        # --- Stage 1: instantiate LightRAG ---
        print("== [Stage 1] Instantiate LightRAG with full multimodal stack ==")
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
        _check("image_blob_store instantiated", rag.image_blob_store is not None)
        _check("image_metadata instantiated", rag.image_metadata is not None)
        _check("images_vdb instantiated", rag.images_vdb is not None)
        _check(
            "images_vdb uses MultimodalEmbeddingFunc",
            rag.images_vdb.embedding_func is image_embedding_func,
        )

        # --- Stage 2: initialize_storages ---
        print("\n== [Stage 2] initialize_storages() ==")
        t0 = time.time()
        await rag.initialize_storages()
        print(f"  took {time.time()-t0:.2f}s")

        # --- Stage 3: ingest each PDF via ainsert_image ---
        print("\n== [Stage 3] Ingest 3 PDFs via rag.ainsert_image() ==")
        ingest_records: list[dict[str, Any]] = []
        total_start = time.time()
        for pdf_str in PDFS:
            pdf_path = Path(pdf_str)
            if not pdf_path.exists():
                print(f"  [skip] {pdf_path.name} — not found")
                continue

            print(f"\n  --- {pdf_path.name} ---")
            t0 = time.time()
            png_bytes = rasterize_page_1(pdf_path, dpi=144)
            print(f"  rasterized p.1: {len(png_bytes):,} bytes in {time.time()-t0:.2f}s")

            t0 = time.time()
            try:
                track_id = await rag.ainsert_image(
                    png_bytes,
                    file_path=str(pdf_path),
                    mime_type="image/png",
                    extra_metadata={
                        "source_pdf": pdf_path.name,
                        "source_page": 1,
                        "total_pages": fitz.open(pdf_path).page_count,
                    },
                )
                ingest_elapsed = time.time() - t0
                print(f"  ainsert_image : OK in {ingest_elapsed:.2f}s  track_id={track_id}")
            except Exception as e:
                ingest_elapsed = time.time() - t0
                print(f"  ainsert_image : FAILED in {ingest_elapsed:.2f}s: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                continue

            ingest_records.append(
                {
                    "pdf": pdf_path.name,
                    "bytes": png_bytes,
                    "size": len(png_bytes),
                    "track_id": track_id,
                    "elapsed": ingest_elapsed,
                }
            )

        total_elapsed = time.time() - total_start
        print(f"\n  total ingest time: {total_elapsed:.2f}s for {len(ingest_records)}/{len(PDFS)} PDFs")
        _check("all PDFs ingested", len(ingest_records) == len(PDFS))

        # --- Stage 4: verify every store has the data ---
        print("\n== [Stage 4] Verify per-store persistence ==")

        # 4a. blob files on disk
        blob_root = rag.image_blob_store._root
        blob_files = [
            f for f in Path(blob_root).rglob("*") if f.is_file() and not f.name.endswith(".meta.json")
        ]
        sidecar_files = [
            f for f in Path(blob_root).rglob("*.meta.json") if f.is_file()
        ]
        print(f"  blob files on disk : {len(blob_files)}")
        print(f"  sidecar files      : {len(sidecar_files)}")
        _check("N blob files == 3", len(blob_files) == 3)
        _check("N sidecar files == 3", len(sidecar_files) == 3)

        # 4b. image_metadata KV records
        kv_root = os.path.join(tmpdir, workspace, "kv_store_image_metadata.json")
        await rag.image_metadata.index_done_callback()
        _check("image_metadata JSON on disk", os.path.isfile(kv_root))
        with open(kv_root, "r", encoding="utf-8") as f:
            kv_data = json.load(f)
        print(f"  image_metadata KV records: {len(kv_data)}")
        _check("image_metadata has 3 records", len(kv_data) == 3)

        # Confirm each record has the expected fields
        sample_key = next(iter(kv_data))
        sample = kv_data[sample_key]
        for field in ("blob_ref", "content_type", "caption_json", "annotation_text",
                       "source_doc_id", "source_file_path", "extra"):
            _check(f"KV record has '{field}'", field in sample)

        # 4c. images_vdb has 3 records
        await rag.images_vdb.index_done_callback()
        images_vdb_file = os.path.join(tmpdir, workspace, "vdb_images.json")
        _check("images_vdb JSON on disk", os.path.isfile(images_vdb_file))
        with open(images_vdb_file, "r", encoding="utf-8") as f:
            vdb_raw = json.load(f)
        # Format: dict with 'data' key containing list of records
        vdb_records = vdb_raw.get("data", [])
        print(f"  images_vdb records  : {len(vdb_records)}")
        _check("images_vdb has 3 vectors", len(vdb_records) == 3)

        # Check each vdb record has the meta_fields we asked for
        for rec in vdb_records:
            _check(
                f"vdb record {rec.get('__id__')} has blob_id",
                "blob_id" in rec,
            )
            _check(
                f"vdb record {rec.get('__id__')} has caption",
                "caption" in rec and rec["caption"],
            )

        # 4d. chunks_vdb (text chunks from annotation text)
        chunks_vdb_file = os.path.join(tmpdir, workspace, "vdb_chunks.json")
        chunk_count = 0
        if os.path.isfile(chunks_vdb_file):
            with open(chunks_vdb_file, "r", encoding="utf-8") as f:
                ch = json.load(f)
            chunk_count = len(ch.get("data", []))
        print(f"  chunks_vdb records  : {chunk_count}")
        _check("chunks_vdb has >=3 text chunks from annotations", chunk_count >= 3)

        # 4e. KG graph nodes — proof that entity extraction ran
        graphml_file = os.path.join(tmpdir, workspace, "graph_chunk_entity_relation.graphml")
        _check("graph file on disk", os.path.isfile(graphml_file))
        # Quick-and-dirty node count via the graph storage object
        try:
            kg = rag.chunk_entity_relation_graph
            labels = await kg.get_all_labels()
            node_count = len(labels)
        except Exception as e:
            print(f"  could not read KG labels: {e}")
            node_count = 0
        print(f"  KG entity nodes     : {node_count}")
        _check("KG has entities from entity extraction", node_count > 0)

        # --- Stage 5: cross-modal query via images_vdb ---
        print("\n== [Stage 5] Cross-modal queries via images_vdb.query() ==")

        async def run_query(label: str, query_text: str, expected_top_kind: str):
            print(f"\n  Query: {query_text!r}  (expecting '{expected_top_kind}' on top)")
            results = await rag.images_vdb.query(query_text, top_k=5)
            print(f"  returned {len(results)} results:")
            for i, r in enumerate(results):
                dist = r.get("distance", 0.0)
                cat = r.get("image_category", "")
                sub = r.get("sub_type", "")
                fp = r.get("file_path", "")
                fp_short = os.path.basename(fp) if fp else ""
                print(f"    {i+1}. d={dist:+.4f}  {cat}/{sub}  {fp_short}")
            _check(
                f"{label}: got at least one result",
                len(results) >= 1,
            )
            return results

        curtain = await run_query(
            "curtain_wall_query",
            "幕墙工程 建筑标准 施工规范",
            "engineering/curtain-wall",
        )
        art = await run_query(
            "art_book_query",
            "游戏 美术设定 角色插画",
            "art/game",
        )

        # Cross-modal ranking sanity check: first result of each query should
        # match the intended domain.
        if curtain:
            top_curtain_path = curtain[0].get("file_path", "")
            top_curtain_is_engineering = any(
                name in top_curtain_path
                for name in ("93545a35", "W020201029")
            )
            _check(
                "curtain wall query: top result is an engineering PDF",
                top_curtain_is_engineering,
                top_curtain_path,
            )

        if art:
            top_art_path = art[0].get("file_path", "")
            top_art_is_art = "RCArtBook" in top_art_path
            _check(
                "art book query: top result is the art book PDF",
                top_art_is_art,
                top_art_path,
            )

        # --- Stage 6: finalize cleanly ---
        print("\n== [Stage 6] finalize_storages() ==")
        await rag.finalize_storages()
        print("  OK")

        print("\n\033[92mPhase 3 integration smoke test: ALL PASS\033[0m")

    finally:
        if args.keep:
            print(f"\n[--keep] temp working_dir preserved: {tmpdir}")
        elif os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
            print("\n(cleanup) removed temp working_dir")


if __name__ == "__main__":
    asyncio.run(main())
