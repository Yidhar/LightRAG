"""Phase 3 query-only verification.

Re-opens a preserved working_dir from a previous ``verify_phase3_integration.py
--keep`` run and exercises only the cross-modal query path. Avoids the ~10 min
/ significant API-cost cycle of re-running the full ingest when we're just
validating the threshold / ranking fix.

Usage:
    uv run python scripts/verify_phase3_query_only.py <preserved_working_dir>
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
from functools import partial

from dotenv import load_dotenv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, io.UnsupportedOperation):
        pass

load_dotenv()

from lightrag import LightRAG  # noqa: E402
from lightrag.llm.openai import openai_complete_if_cache, openai_embed  # noqa: E402
from lightrag.llm.tongyi import tongyi_multimodal_embedding  # noqa: E402
from lightrag.utils import EmbeddingFunc  # noqa: E402


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"Missing env var: {name}\n")
        sys.exit(2)
    return val


async def stub_llm(*_a, **_kw):
    return "stub"


async def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write(
            "Usage: uv run python scripts/verify_phase3_query_only.py <working_dir>\n"
        )
        sys.exit(2)
    working_dir = sys.argv[1]
    if not os.path.isdir(working_dir):
        sys.stderr.write(f"Not a directory: {working_dir}\n")
        sys.exit(2)

    workspace = "phase3_test"
    vdb_file = os.path.join(working_dir, workspace, "vdb_images.json")
    if not os.path.isfile(vdb_file):
        sys.stderr.write(f"No vdb_images.json under {working_dir}\n")
        sys.exit(2)

    text_emb_host = _require("EMBEDDING_BINDING_HOST")
    text_emb_key = _require("EMBEDDING_BINDING_API_KEY")
    text_emb_model = _require("EMBEDDING_MODEL")
    text_emb_dim = int(_require("EMBEDDING_DIM"))

    mm_model = _require("MM_EMB_MODEL")
    mm_dim = int(_require("MM_EMB_DIM"))
    mm_key = _require("MM_EMB_BINDING_API_KEY")
    mm_host = os.environ.get("MM_EMB_BINDING_HOST") or None

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

    rag = LightRAG(
        working_dir=working_dir,
        workspace=workspace,
        llm_model_func=stub_llm,
        embedding_func=text_embedding_func,
        image_embedding_func=image_embedding_func,
        vision_model_func=None,
    )
    await rag.initialize_storages()
    print(
        f"images_vdb.cosine_better_than_threshold = "
        f"{rag.images_vdb.cosine_better_than_threshold}"
    )

    queries = [
        ("curtain wall", "幕墙工程 建筑标准 施工规范", ("93545a35", "W020201029")),
        ("art book", "游戏 美术设定 角色插画", ("RCArtBook",)),
        ("english-curtain", "curtain wall engineering technical code", ("93545a35", "W020201029")),
    ]

    total_pass = 0
    total_fail = 0
    for label, q, expected_top_keys in queries:
        print(f"\n=== Query: {q!r}  (expect top: {expected_top_keys}) ===")
        results = await rag.images_vdb.query(q, top_k=5)
        print(f"  -> {len(results)} results")
        for i, r in enumerate(results):
            d = r.get("distance", 0.0)
            cat = r.get("image_category", "")
            sub = r.get("sub_type", "")
            fp = os.path.basename(r.get("file_path", ""))
            print(f"    {i+1}. d={d:+.4f}  {cat}/{sub}  {fp}")
        if not results:
            print("  [FAIL] no results")
            total_fail += 1
            continue
        top_fp = results[0].get("file_path", "")
        top_matches = any(key in top_fp for key in expected_top_keys)
        if top_matches:
            print(f"  [PASS] top result matches {expected_top_keys}")
            total_pass += 1
        else:
            print(f"  [FAIL] top result {top_fp!r} does not contain any of {expected_top_keys}")
            total_fail += 1

    await rag.finalize_storages()
    print(f"\nqueries: {total_pass} pass / {total_fail} fail")
    if total_fail > 0:
        sys.exit(1)
    print("\033[92mPhase 3 query-only verification: ALL PASS\033[0m")


if __name__ == "__main__":
    asyncio.run(main())
