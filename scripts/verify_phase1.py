"""Phase 1 end-to-end smoke test for the multimodal pipeline additions.

Verifies that:
  1. ``openai_complete_if_cache`` accepts ``image_data`` and produces a
     structured caption via the configured vision model.
  2. ``tongyi_multimodal_embedding`` produces aligned text + image vectors
     from DashScope's native multimodal embedding endpoint.
  3. ``MultimodalEmbeddingFunc.__call__`` correctly routes to ``text_encode``
     so the same instance can be dropped into a ``BaseVectorStorage`` slot.

Usage::

    uv run python scripts/verify_phase1.py <path/to/image.jpg> [query_text]

Example::

    uv run python scripts/verify_phase1.py tests/fixtures/test_drawing.jpg \
        "绿化布局 杨树 花坛 喷泉"

All configuration is read from ``.env`` — see the ``VISION_*`` and ``MM_EMB_*``
sections there. No URLs or keys are hardcoded in this script.
"""

from __future__ import annotations

import asyncio
import os
import sys

import numpy as np
from dotenv import load_dotenv

# Load .env from the project root (two levels up from this script is unnecessary;
# dotenv looks in the current working directory + parents by default).
load_dotenv()

from lightrag.llm.openai import openai_complete_if_cache  # noqa: E402
from lightrag.llm.tongyi import tongyi_multimodal_embedding  # noqa: E402


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(
            f"[verify_phase1] Missing environment variable: {name}\n"
            f"  Did you create a .env file at the project root?\n"
        )
        sys.exit(2)
    return val


async def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write(
            "Usage: uv run python scripts/verify_phase1.py "
            "<path/to/image.jpg> [query_text]\n"
        )
        sys.exit(2)

    image_path = sys.argv[1]
    query_text = sys.argv[2] if len(sys.argv) > 2 else "绿化布局 杨树 花坛 喷泉"

    vision_model = _require("VISION_MODEL")
    vision_host = _require("VISION_BINDING_HOST")
    vision_key = _require("VISION_BINDING_API_KEY")

    mm_model = _require("MM_EMB_MODEL")
    mm_dim = int(_require("MM_EMB_DIM"))
    mm_key = _require("MM_EMB_BINDING_API_KEY")
    # Optional: dashscope SDK uses its built-in default endpoint unless this
    # is set (e.g. to route via a private gateway).
    mm_host = os.environ.get("MM_EMB_BINDING_HOST") or None

    with open(image_path, "rb") as f:
        image_bytes = f.read()
    print(f"[1/3] Loaded {len(image_bytes)} bytes from {image_path}")

    # ---------- 1. Vision caption ----------
    print(f"[2/3] Calling vision model: {vision_model} @ {vision_host}")
    caption = await openai_complete_if_cache(
        model=vision_model,
        prompt=(
            "请按系统提示的 JSON 格式分析这张图片并输出。"
            "仅返回一个 JSON 对象,不要 markdown 包裹。"
        ),
        system_prompt=(
            "你是工程图纸识读专家。分析提供的图像,识别类别、主题、关键对象"
            "和技术细节,返回一个 JSON 对象,包含以下字段:"
            " image_category, sub_type, caption, detailed_description,"
            " detected_entities, key_attributes。"
            "仅返回 JSON,不要任何其它文字。"
        ),
        base_url=vision_host,
        api_key=vision_key,
        image_data=image_bytes,
        image_mime_type="image/jpeg",
    )
    print("--- CAPTION ---")
    print(caption)
    print()

    # ---------- 2. Multimodal embedding ----------
    host_desc = mm_host if mm_host else "(dashscope SDK default)"
    print(
        f"[3/3] Calling multimodal embedding: {mm_model} "
        f"(dim={mm_dim}) via {host_desc}"
    )
    mm_emb = tongyi_multimodal_embedding(
        model=mm_model,
        embedding_dim=mm_dim,
        api_key=mm_key,
        base_url=mm_host,  # None → SDK uses its built-in default
    )

    img_vec = await mm_emb.image_encode([image_bytes])
    txt_vec = await mm_emb.text_encode([query_text])
    print(f"  image_encode output shape: {img_vec.shape}  (expected (1, {mm_dim}))")
    print(f"  text_encode  output shape: {txt_vec.shape}  (expected (1, {mm_dim}))")

    assert img_vec.shape == (1, mm_dim), "image_encode shape mismatch"
    assert txt_vec.shape == (1, mm_dim), "text_encode shape mismatch"

    # Cosine similarity as a sanity check. Aligned multimodal models should
    # produce a non-trivial positive similarity for a semantically related pair.
    img_v = img_vec[0] / (np.linalg.norm(img_vec[0]) + 1e-9)
    txt_v = txt_vec[0] / (np.linalg.norm(txt_vec[0]) + 1e-9)
    cos_sim = float(np.dot(img_v, txt_v))
    print(f"  cross-modal cosine similarity: {cos_sim:+.4f}")
    print(f"  query: {query_text!r}")

    # Verify __call__ routes to text_encode (the key to cross-modal retrieval).
    via_call = await mm_emb([query_text])
    assert np.allclose(via_call, txt_vec), (
        "MultimodalEmbeddingFunc.__call__ should route to text_encode"
    )
    print("  __call__ → text_encode routing: OK")
    print()
    print("Phase 1 smoke test completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
