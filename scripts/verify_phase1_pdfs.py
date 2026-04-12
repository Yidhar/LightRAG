"""Phase 1 end-to-end test driven by PDF inputs.

Rasterizes one or more pages of each PDF to PNG bytes (via pymupdf) and
feeds them to the same two components that ``verify_phase1.py`` exercises:

  1. ``openai_complete_if_cache`` with ``image_data`` (vision LLM captioning)
  2. ``tongyi_multimodal_embedding`` (shared text+image semantic space)

This is a *forward-looking* validation: LightRAG's current PDF ingestion
path only extracts text (see ``_convert_with_docling`` / ``_extract_pdf_pypdf``
in ``lightrag/api/routers/document_routes.py``); embedded images are
discarded. Phase 5 will wire PDF-embedded images into the multimodal
pipeline. This script previews what the vision + embedding halves of that
pipeline will produce on your actual documents, with zero storage setup.

Usage::

    uv run python scripts/verify_phase1_pdfs.py <pdf1> [<pdf2> ...] \
        [--pages 1]  [--query "your search text"]

All configuration is read from ``.env``.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from dotenv import load_dotenv

# Force UTF-8 stdout/stderr on Windows so CJK captions print correctly
# regardless of the terminal's active codepage (cp936/gbk by default).
# This does NOT affect the production library — it's a test-harness fix.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, io.UnsupportedOperation):
        pass

load_dotenv()

from lightrag.llm.openai import openai_complete_if_cache  # noqa: E402
from lightrag.llm.tongyi import tongyi_multimodal_embedding  # noqa: E402

try:
    import pymupdf  # noqa: F401
    import fitz  # type: ignore  # noqa: E402
except ImportError:
    sys.stderr.write(
        "[verify_phase1_pdfs] pymupdf not installed. "
        "Install with: uv pip install pymupdf\n"
    )
    sys.exit(2)


CAPTION_SYSTEM_PROMPT = (
    "你是工程、建筑与施工领域的图像理解专家,精通工程图纸识读、施工现场判读"
    "和设备材料辨识。分析提供的图像,返回一个严格的 JSON 对象,仅包含这些字段:"
    " image_category, sub_type, caption, detailed_description,"
    " detected_entities, key_attributes。"
    "image_category 取值: Engineering Drawing | Construction Photo |"
    " Equipment Closeup | Material Closeup | Document Page | Other。"
    "仅返回 JSON,不要 markdown 围栏,不要任何其它文字。"
)

CAPTION_USER_PROMPT = (
    "请按系统提示的 JSON 格式分析这张图片并输出。如果这是一张扫描文档页,"
    "使用 image_category='Document Page',在 detailed_description 中概述文档内容。"
)


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"[verify_phase1_pdfs] Missing env var: {name}\n")
        sys.exit(2)
    return val


def rasterize_pdf_page(pdf_path: Path, page_index: int, dpi: int = 144) -> bytes:
    """Render a single PDF page to PNG bytes at the requested DPI."""
    doc = fitz.open(pdf_path)
    try:
        if page_index >= doc.page_count:
            raise IndexError(
                f"{pdf_path.name} has {doc.page_count} pages, "
                f"requested index {page_index}"
            )
        page = doc.load_page(page_index)
        # 72 is the PDF base DPI; scale to reach requested DPI.
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(
        np.dot(
            a / (np.linalg.norm(a) + 1e-9),
            b / (np.linalg.norm(b) + 1e-9),
        )
    )


async def test_one_image(
    label: str,
    image_bytes: bytes,
    *,
    vision_model: str,
    vision_host: str,
    vision_key: str,
    mm_emb: Any,
    query: str,
) -> dict[str, Any]:
    print(f"\n{'=' * 78}")
    print(f" {label}   ({len(image_bytes):,} bytes)")
    print("=" * 78)

    # ---- vision caption ----
    t0 = time.time()
    try:
        caption = await openai_complete_if_cache(
            model=vision_model,
            prompt=CAPTION_USER_PROMPT,
            system_prompt=CAPTION_SYSTEM_PROMPT,
            base_url=vision_host,
            api_key=vision_key,
            image_data=image_bytes,
            image_mime_type="image/png",
        )
        t_vision = time.time() - t0
        print(f"\n[vision]  OK in {t_vision:.2f}s")
        # Pretty-print first ~1500 chars of the caption
        print("--- caption (truncated) ---")
        preview = caption.strip()
        if len(preview) > 1500:
            preview = preview[:1500] + "\n  ... [truncated]"
        print(preview)
    except Exception as e:
        t_vision = time.time() - t0
        print(f"\n[vision]  FAILED in {t_vision:.2f}s: {type(e).__name__}: {e}")
        caption = None

    # ---- multimodal embedding ----
    t0 = time.time()
    try:
        img_vec = await mm_emb.image_encode([image_bytes])
        t_img = time.time() - t0
        print(
            f"\n[mm-emb]  image_encode OK in {t_img:.2f}s, "
            f"shape={img_vec.shape}"
        )
    except Exception as e:
        t_img = time.time() - t0
        print(
            f"\n[mm-emb]  image_encode FAILED in {t_img:.2f}s: "
            f"{type(e).__name__}: {e}"
        )
        img_vec = None

    t0 = time.time()
    try:
        txt_vec = await mm_emb.text_encode([query])
        t_txt = time.time() - t0
        print(f"[mm-emb]  text_encode OK  in {t_txt:.2f}s, shape={txt_vec.shape}")
    except Exception as e:
        t_txt = time.time() - t0
        print(
            f"[mm-emb]  text_encode FAILED in {t_txt:.2f}s: "
            f"{type(e).__name__}: {e}"
        )
        txt_vec = None

    # ---- cross-modal similarity ----
    if img_vec is not None and txt_vec is not None:
        sim = _cosine(img_vec[0], txt_vec[0])
        print(f"\n[similarity]  query='{query}'  →  cos={sim:+.4f}")
    else:
        sim = None

    return {
        "label": label,
        "caption": caption,
        "img_vec": img_vec,
        "txt_vec": txt_vec,
        "similarity": sim,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="+", help="PDF files to test")
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        help="Number of pages per PDF to rasterize (starting at page 1).",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="1-based starting page number.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=144,
        help="Raster DPI (higher = sharper but larger uploads).",
    )
    parser.add_argument(
        "--query",
        default=None,
        help=(
            "Query text used for cross-modal similarity. If omitted, reads "
            "from TEST_QUERY env var, falling back to a built-in Chinese "
            "default. Passing CJK text on the Windows shell is unreliable "
            "(the shell mangles it before Python sees it), so prefer "
            "setting TEST_QUERY in your .env file."
        ),
    )
    args = parser.parse_args()
    # Resolve the query from (in order): CLI arg > env > built-in literal.
    # The literal is embedded in Python source (UTF-8 per PEP 3120) so it
    # survives even when the shell mangles command-line arguments.
    query_text = (
        args.query
        or os.environ.get("TEST_QUERY")
        or "工程图纸 施工 设备 材料 幕墙 建筑标准"
    )

    vision_model = _require("VISION_MODEL")
    vision_host = _require("VISION_BINDING_HOST")
    vision_key = _require("VISION_BINDING_API_KEY")
    mm_model = _require("MM_EMB_MODEL")
    mm_dim = int(_require("MM_EMB_DIM"))
    mm_key = _require("MM_EMB_BINDING_API_KEY")
    mm_host = os.environ.get("MM_EMB_BINDING_HOST") or None

    print("Phase 1 PDF smoke test")
    print("-" * 78)
    print(f"  Vision    : {vision_model}  @  {vision_host}")
    print(
        f"  Multimodal: {mm_model}  (dim={mm_dim})  @  "
        f"{mm_host or '(dashscope SDK default)'}"
    )
    print(f"  PDFs      : {len(args.pdfs)} file(s)")
    print(f"  Pages     : {args.pages} page(s) starting at {args.start_page}")
    print(f"  DPI       : {args.dpi}")
    print(f"  Query     : {query_text!r}")

    mm_emb = tongyi_multimodal_embedding(
        model=mm_model,
        embedding_dim=mm_dim,
        api_key=mm_key,
        base_url=mm_host,
    )

    results = []
    for pdf_str in args.pdfs:
        pdf_path = Path(pdf_str)
        if not pdf_path.exists():
            print(f"\n[skip] {pdf_path}  — file not found")
            continue

        try:
            doc = fitz.open(pdf_path)
            total_pages = doc.page_count
            doc.close()
        except Exception as e:
            print(f"\n[skip] {pdf_path}  — pymupdf open failed: {e}")
            continue

        print(f"\n### {pdf_path.name}  ({total_pages} pages total)")

        for offset in range(args.pages):
            page_idx = args.start_page - 1 + offset
            if page_idx >= total_pages:
                break
            try:
                png_bytes = rasterize_pdf_page(pdf_path, page_idx, dpi=args.dpi)
            except Exception as e:
                print(f"  [rasterize p{page_idx+1}]  FAILED: {e}")
                continue
            label = f"{pdf_path.name}  page {page_idx+1}/{total_pages}"
            result = await test_one_image(
                label,
                png_bytes,
                vision_model=vision_model,
                vision_host=vision_host,
                vision_key=vision_key,
                mm_emb=mm_emb,
                query=query_text,
            )
            results.append(result)

    # ---- cross-document similarity matrix ----
    valid = [r for r in results if r["img_vec"] is not None]
    if len(valid) >= 2:
        print(f"\n{'=' * 78}")
        print(" Cross-document image-to-image cosine similarity matrix")
        print("=" * 78)
        header = "              " + "  ".join(
            f"{i+1:>6d}" for i in range(len(valid))
        )
        print(header)
        for i, ri in enumerate(valid):
            row = [f"{i+1}. {ri['label'][:10]:<10}"]
            for j, rj in enumerate(valid):
                s = _cosine(ri["img_vec"][0], rj["img_vec"][0])
                row.append(f"{s:+.3f}")
            print("  ".join(row))

    print(f"\n{'=' * 78}")
    print(" Phase 1 PDF smoke test complete.")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
