"""Phase 2 end-to-end integration test with real multimodal APIs.

This is the bridge validation between Phase 2 and Phase 3:

    1. Instantiate a real LightRAG with:
         - text LLM       : stub (not used)
         - text embedding : real DashScope text-embedding-v4 (via openai_embed)
         - vision LLM     : real DashScope qwen3-vl-plus
         - image embedding: real tongyi-embedding-vision-plus (via dashscope SDK)

    2. initialize_storages() — exercises both the existing text stack AND the
       new multimodal stack wired in Phase 2 (blob store + image metadata KV).

    3. For each Phase 1 test PDF:
         - rasterize page 1 via pymupdf
         - rag.image_blob_store.put()      — persist raw bytes + sidecar
         - vision_model_func(image_data=…) — caption JSON
         - image_embedding_func.image_encode() — 1152-d vector
         - rag.image_metadata.upsert()     — sidecar metadata + caption

    4. Verify:
         - Each blob and its sidecar are on disk under {wd}/blobs/{ws}/image_blobs/
         - rag.image_blob_store.get() round-trips exact bytes
         - rag.image_metadata.get_by_id() returns the upserted record
         - After index_done_callback(), the KV JSON on disk contains the keys
         - The physical layout matches the documented Phase 2 convention

    5. Cleanup (finalize_storages, optional rmtree).

Nothing in this test touches chunks_vdb / entities_vdb / graph / doc_status,
so no insert/query/entity-extraction runs. This isolates Phase 2 wiring.

Usage::

    uv run python scripts/verify_phase2_integration.py [--keep]

    --keep   do not delete the temp working_dir after the test; lets you
             inspect {tmpdir}/blobs/{ws}/image_blobs/ and the sidecar JSONs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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

# UTF-8 stdout on Windows (same trick as verify_phase1_pdfs.py)
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
from lightrag.base import BaseBlobStorage  # noqa: E402
from lightrag.llm.openai import openai_complete_if_cache, openai_embed  # noqa: E402
from lightrag.llm.tongyi import tongyi_multimodal_embedding  # noqa: E402
from lightrag.utils import EmbeddingFunc  # noqa: E402


# The three PDFs validated during Phase 1.
PDFS = [
    r"C:\Users\yidhar\Downloads\93545a35-0153-4dfd-8b6a-7e06281e841e.pdf",
    r"C:\Users\yidhar\Downloads\W020201029539475435520.pdf",
    r"C:\Users\yidhar\Downloads\RCArtBook.pdf",
]


CAPTION_SYSTEM_PROMPT = (
    "你是工程与施工领域的图像理解专家。分析图像并返回一个 JSON 对象,仅含字段:"
    " image_category, sub_type, caption, detailed_description,"
    " detected_entities, key_attributes。只返回 JSON,不要 markdown 围栏。"
)
CAPTION_USER_PROMPT = "请按系统提示的 JSON 格式分析这张图片并输出。"


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"[verify_phase2_integration] Missing env var: {name}\n")
        sys.exit(2)
    return val


def _check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    tag = f"\033[92m{mark}\033[0m" if condition else f"\033[91m{mark}\033[0m"
    print(f"  [{tag}] {label}" + (f"  — {detail}" if detail else ""))
    if not condition:
        raise SystemExit(1)


def rasterize_page_1(pdf_path: Path, dpi: int = 144) -> bytes:
    """Return page 1 of the PDF as PNG bytes."""
    doc = fitz.open(pdf_path)
    try:
        if doc.page_count == 0:
            raise RuntimeError(f"{pdf_path.name} has 0 pages")
        page = doc.load_page(0)
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def blob_id_for(data: bytes) -> str:
    """Content-addressed blob id."""
    return "img-" + hashlib.md5(data).hexdigest()[:16]


async def stub_llm(*_args: Any, **_kwargs: Any) -> str:
    """Stand-in for llm_model_func — never actually called in this test."""
    return "stub"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Preserve the temp working_dir after the test for inspection.",
    )
    args = parser.parse_args()

    # --- Pull config from .env ---
    llm_host = _require("LLM_BINDING_HOST")
    llm_key = _require("LLM_BINDING_API_KEY")

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

    # --- Build real embedding funcs / vision func ---
    text_embedding_func = EmbeddingFunc(
        embedding_dim=text_emb_dim,
        max_token_size=8192,
        send_dimensions=True,
        # Use .func to access the underlying async function — ``openai_embed``
        # is already an EmbeddingFunc (see CLAUDE.md "Nested Embedding Functions").
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

    # --- Temp working_dir with a distinct workspace so we don't touch user's data ---
    tmpdir = tempfile.mkdtemp(prefix="lightrag_phase2_integration_")
    workspace = "phase2_test"
    print(f"Temp working_dir: {tmpdir}")
    print(f"Workspace       : {workspace}")
    print()

    try:
        # --- Stage 1: instantiate LightRAG with multimodal wiring ---
        print("== [Stage 1] Instantiate LightRAG with real multimodal funcs ==")
        rag = LightRAG(
            working_dir=tmpdir,
            workspace=workspace,
            llm_model_func=stub_llm,
            # llm_model_name left as default; we never call llm_model_func.
            embedding_func=text_embedding_func,
            image_embedding_func=image_embedding_func,
            vision_model_func=vision_model_func,
        )
        _check("image_blob_store instantiated", rag.image_blob_store is not None)
        _check(
            "image_blob_store isinstance BaseBlobStorage",
            isinstance(rag.image_blob_store, BaseBlobStorage),
        )
        _check("image_metadata KV instantiated", rag.image_metadata is not None)
        _check(
            "image_embedding_func forwarded",
            rag.image_embedding_func is image_embedding_func,
        )
        _check(
            "vision_model_func forwarded",
            rag.vision_model_func is vision_model_func,
        )

        print("\n== [Stage 2] initialize_storages() ==")
        t0 = time.time()
        await rag.initialize_storages()
        print(f"  initialize_storages() took {time.time()-t0:.2f}s")
        _check(
            "blob store root on disk",
            os.path.isdir(rag.image_blob_store._root),
            rag.image_blob_store._root,
        )
        expected_root = os.path.join(tmpdir, "blobs", workspace, "image_blobs")
        _check(
            "blob store root follows convention",
            os.path.abspath(rag.image_blob_store._root) == os.path.abspath(expected_root),
        )

        # --- Stage 3: process each PDF ---
        print("\n== [Stage 3] Process PDFs through the Phase 2 storage layer ==")
        processed: list[dict[str, Any]] = []
        for pdf_str in PDFS:
            pdf_path = Path(pdf_str)
            if not pdf_path.exists():
                print(f"  [skip] {pdf_path.name} — not found")
                continue

            print(f"\n  --- {pdf_path.name} ---")
            t0 = time.time()
            png_bytes = rasterize_page_1(pdf_path, dpi=144)
            print(f"  rasterized page 1: {len(png_bytes):,} bytes in {time.time()-t0:.2f}s")

            blob_id = blob_id_for(png_bytes)
            print(f"  blob_id          : {blob_id}")

            # 3a. Put the blob
            t0 = time.time()
            blob_ref = await rag.image_blob_store.put(
                blob_id,
                png_bytes,
                content_type="image/png",
                metadata={
                    "source_pdf": pdf_path.name,
                    "source_page": 1,
                    "total_pages": fitz.open(pdf_path).page_count,
                    "dpi": 144,
                },
            )
            print(f"  blob_store.put() : OK in {time.time()-t0:.2f}s")
            print(f"  blob_ref         : {blob_ref}")
            _check(
                f"{pdf_path.name}: blob file exists on disk",
                os.path.isfile(blob_ref),
            )

            # 3b. Vision caption
            t0 = time.time()
            try:
                caption_raw = await vision_model_func(
                    prompt=CAPTION_USER_PROMPT,
                    system_prompt=CAPTION_SYSTEM_PROMPT,
                    image_data=png_bytes,
                    image_mime_type="image/png",
                )
                print(f"  vision caption   : OK in {time.time()-t0:.2f}s")
            except Exception as e:
                print(f"  vision caption   : FAILED in {time.time()-t0:.2f}s: {e}")
                caption_raw = None

            caption_json: dict[str, Any] | None = None
            if caption_raw:
                try:
                    caption_json = json.loads(caption_raw)
                    cat = caption_json.get("image_category", "?")
                    sub = caption_json.get("sub_type", "?")
                    print(f"  caption summary  : {cat} / {sub}")
                except json.JSONDecodeError:
                    print(f"  caption JSON parse failed; keeping raw text")
                    caption_json = {"raw": caption_raw}

            # 3c. Image embedding
            t0 = time.time()
            try:
                vec = await image_embedding_func.image_encode([png_bytes])
                print(
                    f"  image_encode     : OK in {time.time()-t0:.2f}s, "
                    f"shape={vec.shape}"
                )
                _check(
                    f"{pdf_path.name}: image vector shape correct",
                    vec.shape == (1, mm_dim),
                )
            except Exception as e:
                print(f"  image_encode     : FAILED in {time.time()-t0:.2f}s: {e}")
                vec = None

            # 3d. Sidecar metadata KV
            await rag.image_metadata.upsert(
                {
                    blob_id: {
                        "blob_ref": blob_ref,
                        "content_type": "image/png",
                        "caption_json": caption_json,
                        "source_pdf": pdf_path.name,
                        "source_page": 1,
                        "image_vector_preview": (
                            vec[0][:6].tolist() if vec is not None else None
                        ),
                    }
                }
            )
            print(f"  image_metadata   : upserted")

            processed.append(
                {
                    "blob_id": blob_id,
                    "blob_ref": blob_ref,
                    "size": len(png_bytes),
                    "pdf": pdf_path.name,
                    "caption_json": caption_json,
                    "image_vector": vec,
                    "image_bytes": png_bytes,
                }
            )

        print(f"\n  total processed: {len(processed)} / {len(PDFS)} PDFs")
        _check("at least one PDF processed", len(processed) > 0)

        # --- Stage 4: verify persistence of Phase 2 stores ---
        print("\n== [Stage 4] Verify Phase 2 storage persistence ==")

        # 4a. Blob round-trips
        for p in processed:
            got = await rag.image_blob_store.get(p["blob_id"])
            _check(
                f"{p['pdf']}: blob get() round-trip",
                got == p["image_bytes"],
                f"{len(got or b''):,} bytes",
            )
            ref = await rag.image_blob_store.get_reference(p["blob_id"])
            _check(
                f"{p['pdf']}: get_reference() -> abs path",
                ref is not None and os.path.isabs(ref),
            )
            meta = await rag.image_blob_store.get_metadata(p["blob_id"])
            _check(
                f"{p['pdf']}: sidecar has source_pdf in extra",
                meta is not None and meta["extra"].get("source_pdf") == p["pdf"],
            )
            _check(
                f"{p['pdf']}: sidecar size matches payload",
                meta["size"] == p["size"],
            )

        # 4b. Image metadata KV round-trips
        for p in processed:
            rec = await rag.image_metadata.get_by_id(p["blob_id"])
            _check(
                f"{p['pdf']}: image_metadata KV get_by_id returns record",
                rec is not None,
            )
            _check(
                f"{p['pdf']}: KV record has caption_json",
                "caption_json" in rec,
            )
            _check(
                f"{p['pdf']}: KV record blob_ref matches",
                rec.get("blob_ref") == p["blob_ref"],
            )

        # 4c. index_done_callback → KV JSON hits disk
        print()
        print("  Forcing index_done_callback() on image_metadata ...")
        await rag.image_metadata.index_done_callback()

        kv_json_path = os.path.join(
            tmpdir, workspace, "kv_store_image_metadata.json"
        )
        _check(
            "image_metadata JSON file exists on disk",
            os.path.isfile(kv_json_path),
            kv_json_path,
        )
        with open(kv_json_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        for p in processed:
            _check(
                f"{p['pdf']}: blob_id present in on-disk KV JSON",
                p["blob_id"] in on_disk,
            )

        # 4d. Layout inspection
        print("\n  Physical layout under blob store root:")
        root = rag.image_blob_store._root
        for dirpath, dirnames, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, tmpdir)
            indent = "    " * (rel.count(os.sep))
            print(f"    {indent}{os.path.basename(dirpath) or rel}/")
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                sz = os.path.getsize(full)
                print(f"    {indent}  {fn}  ({sz:,} bytes)")

        # 4e. Cross-document image similarity sanity (Phase 1 validated this
        #     with raw bytes; we re-check using vectors pulled from the real
        #     in-memory upserts to prove we can rank them post-storage)
        vecs = [p["image_vector"] for p in processed if p["image_vector"] is not None]
        if len(vecs) >= 2:
            print("\n  Cross-document image-image similarity (post-upsert):")
            for i, a in enumerate(vecs):
                row = []
                for j, b in enumerate(vecs):
                    av = a[0] / (np.linalg.norm(a[0]) + 1e-9)
                    bv = b[0] / (np.linalg.norm(b[0]) + 1e-9)
                    sim = float(np.dot(av, bv))
                    row.append(f"{sim:+.3f}")
                pdf_name = processed[i]["pdf"][:14]
                print(f"    {i+1}. {pdf_name:<14}  " + "  ".join(row))

        # --- Stage 5: finalize cleanly ---
        print("\n== [Stage 5] finalize_storages() ==")
        t0 = time.time()
        await rag.finalize_storages()
        print(f"  finalize_storages() took {time.time()-t0:.2f}s")

        print("\n\033[92mPhase 2 integration smoke test: ALL PASS\033[0m")

    finally:
        if args.keep:
            print(f"\n[--keep] leaving temp working_dir for inspection: {tmpdir}")
        elif os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
            print(f"\n(cleanup) removed temp working_dir")


if __name__ == "__main__":
    asyncio.run(main())
