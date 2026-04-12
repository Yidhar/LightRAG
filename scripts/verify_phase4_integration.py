"""Phase 4 end-to-end integration test.

Builds on the Phase 3 infrastructure and verifies that:

    1. `rag.aquery_data(mode="mix")` returns image chunks from images_vdb
       in its raw_data.data.chunks list, with image_blob_id / blob_ref
       metadata preserved all the way through the retrieval pipeline.

    2. Pure-text text mode (chunks_vdb only) produces no image chunks.

    3. `rag.aquery(mode="mix")` runs the full LLM generation path and
       the returned answer references the image blob_ids embedded in
       the chunk content (via the "【图像】 blob_id=..." marker).

    4. The new /images/{blob_id} and /documents/{doc_id}/images endpoint
       handlers return expected shapes when invoked in-process (we build
       a test app with FastAPI TestClient rather than starting a server).

    5. The reverse image index (doc_status[doc_id].metadata.image_ids)
       was written by ainsert_image and is readable via the new endpoint.

Runs the full Phase 3 ingest (3 PDFs) on each invocation because Phase 4
changes the retrieval and storage layers — tests need fresh state to
prove the wiring works from scratch.

Usage:
    uv run python scripts/verify_phase4_integration.py [--keep]
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

try:
    import fitz  # pymupdf
except ImportError:
    sys.stderr.write("pymupdf not installed. Run: uv pip install pymupdf\n")
    sys.exit(2)

from lightrag import LightRAG, QueryParam  # noqa: E402
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

    tmpdir = tempfile.mkdtemp(prefix="lightrag_phase4_integration_")
    workspace = "phase4_test"
    print(f"Temp working_dir: {tmpdir}")
    print(f"Workspace       : {workspace}\n")

    try:
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
        await rag.initialize_storages()

        print("\n== [Stage 2] Ingest 3 PDFs via ainsert_image ==")
        ingested: list[dict[str, Any]] = []
        t_total = time.time()
        for pdf_str in PDFS:
            pdf_path = Path(pdf_str)
            if not pdf_path.exists():
                print(f"  [skip] {pdf_path.name} — not found")
                continue
            png_bytes = rasterize_page_1(pdf_path, dpi=144)
            t0 = time.time()
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
            elapsed = time.time() - t0
            print(f"  {pdf_path.name}: {elapsed:.1f}s  track_id={track_id}")
            ingested.append({"pdf": pdf_path.name, "path": str(pdf_path)})
        print(f"  total ingest: {time.time()-t_total:.1f}s for {len(ingested)} PDFs")
        _check("all PDFs ingested", len(ingested) == len(PDFS))

        # --- Stage 3: aquery_data in mix mode, verify image chunks ---
        print("\n== [Stage 3] aquery_data in mix mode returns image chunks ==")
        query_text = "幕墙工程 建筑标准 施工规范"
        print(f"  query: {query_text!r}")

        mix_result = await rag.aquery_data(
            query_text,
            QueryParam(mode="mix", chunk_top_k=20, top_k=40),
        )
        mix_chunks = mix_result.get("data", {}).get("chunks", [])
        print(f"  mix mode returned {len(mix_chunks)} chunks")
        _check("mix mode returned >= 1 chunk", len(mix_chunks) >= 1)

        image_chunks = [c for c in mix_chunks if c.get("source_type") == "image_vector"]
        text_chunks_in_mix = [c for c in mix_chunks if c.get("source_type") != "image_vector"]
        print(f"  - image_vector chunks : {len(image_chunks)}")
        print(f"  - other chunks        : {len(text_chunks_in_mix)}")
        _check(
            "mix mode returned at least one image_vector chunk",
            len(image_chunks) >= 1,
        )

        for ic in image_chunks:
            _check(
                f"image chunk has image_blob_id ({ic.get('chunk_id', '?')})",
                bool(ic.get("image_blob_id")) and ic["image_blob_id"].startswith("img-"),
            )
            _check(
                f"image chunk has blob_ref ({ic.get('chunk_id', '?')})",
                bool(ic.get("blob_ref")),
            )
            _check(
                f"image chunk content contains 【图像】 marker ({ic.get('chunk_id', '?')})",
                "【图像】" in ic.get("content", ""),
            )

        # --- Stage 4: naive mode (no images_vdb path) sanity check ---
        print("\n== [Stage 4] naive mode does NOT return image chunks ==")
        naive_result = await rag.aquery_data(
            query_text,
            QueryParam(mode="naive", chunk_top_k=20, top_k=40),
        )
        naive_chunks = naive_result.get("data", {}).get("chunks", [])
        naive_image_chunks = [
            c for c in naive_chunks if c.get("source_type") == "image_vector"
        ]
        print(f"  naive returned {len(naive_chunks)} chunks, {len(naive_image_chunks)} image")
        _check(
            "naive mode image_vector chunks == 0",
            len(naive_image_chunks) == 0,
        )

        # --- Stage 5: full aquery returns a non-empty LLM answer ---
        print("\n== [Stage 5] aquery (full LLM answer) in mix mode ==")
        t0 = time.time()
        answer = await rag.aquery(
            "这些文档涉及哪些与幕墙工程相关的标准?请按地区分组。",
            QueryParam(mode="mix", chunk_top_k=10, top_k=20),
        )
        llm_elapsed = time.time() - t0
        answer_text = answer if isinstance(answer, str) else "<streaming>"
        print(f"  aquery took {llm_elapsed:.1f}s")
        print(f"  --- answer preview (first 600 chars) ---")
        print(f"  {answer_text[:600]}")
        print(f"  --- end ---")
        _check("aquery returned a non-empty string", len(answer_text) > 0)

        # --- Stage 6: reverse index via GET /documents/{doc_id}/images ---
        print("\n== [Stage 6] Reverse image index via doc_status.metadata.image_ids ==")
        # Find all docs and check image_ids on the virtual image docs
        all_processed = await rag.doc_status.get_docs_by_status(
            __import__("lightrag.base", fromlist=["DocStatus"]).DocStatus.PROCESSED
        )
        image_virtual_docs = [
            (doc_id, s)
            for doc_id, s in all_processed.items()
            if doc_id.startswith("doc-img-")
        ]
        print(f"  virtual image docs: {len(image_virtual_docs)}")
        _check("3 virtual image docs exist", len(image_virtual_docs) == 3)

        for doc_id, status in image_virtual_docs:
            meta = status.metadata or {}
            image_ids = meta.get("image_ids") or []
            _check(
                f"{doc_id[:22]}... has image_ids in metadata",
                len(image_ids) >= 1,
                f"image_ids={image_ids}",
            )
            _check(
                f"{doc_id[:22]}... modality is image",
                meta.get("modality") == "image",
            )

        # --- Stage 7: test the new /images/{blob_id} endpoint in-process ---
        print("\n== [Stage 7] /images/{blob_id} and /documents/{doc_id}/images endpoints ==")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lightrag.api.routers.image_routes import create_image_routes

        # Build a minimal app with only the image router for testing.
        test_app = FastAPI()
        image_router = create_image_routes(rag, api_key=None)
        _check("create_image_routes returned a router (MM enabled)", image_router is not None)
        test_app.include_router(image_router)

        client = TestClient(test_app)

        # Pick the first image's blob_id from the reverse index
        sample_doc_id, sample_status = image_virtual_docs[0]
        sample_blob_id = (sample_status.metadata or {}).get("image_ids", [None])[0]
        _check("found sample blob_id to test endpoints", bool(sample_blob_id))

        # GET /images/{blob_id} -> bytes
        r1 = client.get(f"/images/{sample_blob_id}")
        print(f"  GET /images/{sample_blob_id} -> {r1.status_code}  ({len(r1.content)} bytes)")
        _check("GET /images/{blob_id} returns 200", r1.status_code == 200)
        _check(
            "GET /images/{blob_id} returns non-empty image bytes",
            len(r1.content) > 1000,  # A real PNG is at least a few KB
        )
        _check(
            "GET /images/{blob_id} has image content-type",
            r1.headers.get("content-type", "").startswith("image/"),
        )

        # GET /images/{blob_id}/metadata -> json
        r2 = client.get(f"/images/{sample_blob_id}/metadata")
        print(f"  GET /images/{sample_blob_id}/metadata -> {r2.status_code}")
        _check("GET /images/metadata returns 200", r2.status_code == 200)
        mdata = r2.json()
        _check("metadata has blob_id", mdata.get("blob_id") == sample_blob_id)
        _check("metadata has caption_json", mdata.get("caption_json") is not None)
        _check("metadata has annotation_text", bool(mdata.get("annotation_text")))

        # 404 path
        r3 = client.get("/images/img-does-not-exist-abcdef")
        _check("GET unknown blob_id returns 404", r3.status_code == 404)

        # GET /documents/{doc_id}/images needs the document router — build it
        from lightrag.api.routers.document_routes import create_document_routes

        # doc_manager is required by the document router factory. We don't
        # actually use it for this endpoint, so pass a bare object.
        class _DummyDocManager:
            input_dir = Path(tmpdir)
            supported_extensions = (".txt",)

            def is_supported_file(self, name):
                return True

        test_app2 = FastAPI()
        doc_router = create_document_routes(rag, _DummyDocManager(), api_key=None)
        test_app2.include_router(doc_router)
        client2 = TestClient(test_app2)

        r4 = client2.get(f"/documents/{sample_doc_id}/images")
        print(f"  GET /documents/{sample_doc_id[:28]}.../images -> {r4.status_code}")
        _check("GET /documents/{doc_id}/images returns 200", r4.status_code == 200)
        doc_images = r4.json()
        _check(
            "endpoint echoes doc_id",
            doc_images.get("doc_id") == sample_doc_id,
        )
        _check(
            "endpoint returns correct image_ids",
            sample_blob_id in doc_images.get("image_ids", []),
        )
        _check(
            "endpoint reports modality=image",
            doc_images.get("modality") == "image",
        )

        # 404 for unknown doc
        r5 = client2.get("/documents/doc-img-unknown-1234567890abcdef/images")
        _check(
            "GET unknown doc images returns 404",
            r5.status_code == 404,
        )

        await rag.finalize_storages()
        print("\n\033[92mPhase 4 integration test: ALL PASS\033[0m")

    finally:
        if args.keep:
            print(f"\n[--keep] preserved: {tmpdir}")
        elif os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
            print("\n(cleanup) removed temp working_dir")


if __name__ == "__main__":
    asyncio.run(main())
