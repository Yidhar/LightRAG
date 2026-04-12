"""Phase 4 endpoint-only verification.

Reuses a preserved Phase 4 working_dir (from ``verify_phase4_integration.py
--keep``) to run just Stage 7 of the integration test — the endpoint
tests — without the expensive re-ingest pass. Decouples endpoint
validation from the ~10-minute full pipeline so we can iterate on FastAPI
changes quickly.

Usage::

    uv run python scripts/verify_phase4_endpoints_only.py <preserved_working_dir>
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
from lightrag.base import DocStatus  # noqa: E402
from lightrag.llm.openai import openai_embed  # noqa: E402
from lightrag.llm.tongyi import tongyi_multimodal_embedding  # noqa: E402
from lightrag.utils import EmbeddingFunc  # noqa: E402


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


async def stub_llm(*_a, **_kw):
    return "stub"


async def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write(
            "Usage: uv run python scripts/verify_phase4_endpoints_only.py "
            "<preserved_working_dir>\n"
        )
        sys.exit(2)
    working_dir = sys.argv[1]
    workspace = "phase4_test"
    if not os.path.isdir(os.path.join(working_dir, workspace)):
        sys.stderr.write(
            f"Preserved workspace not found: {os.path.join(working_dir, workspace)}\n"
        )
        sys.exit(2)

    text_emb_host = _require("EMBEDDING_BINDING_HOST")
    text_emb_key = _require("EMBEDDING_BINDING_API_KEY")
    text_emb_model = _require("EMBEDDING_MODEL")
    text_emb_dim = int(_require("EMBEDDING_DIM"))

    mm_model = _require("MM_EMB_MODEL")
    mm_dim = int(_require("MM_EMB_DIM"))
    mm_key = _require("MM_EMB_BINDING_API_KEY")

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
    )

    rag = LightRAG(
        working_dir=working_dir,
        workspace=workspace,
        llm_model_func=stub_llm,
        embedding_func=text_embedding_func,
        image_embedding_func=image_embedding_func,
    )
    await rag.initialize_storages()

    # Recover some state to drive the endpoint tests
    all_processed = await rag.doc_status.get_docs_by_status(DocStatus.PROCESSED)
    image_virtual_docs = [
        (doc_id, s)
        for doc_id, s in all_processed.items()
        if doc_id.startswith("doc-img-")
    ]
    _check(
        "preserved workspace has 3 virtual image docs",
        len(image_virtual_docs) == 3,
        f"found {len(image_virtual_docs)}",
    )

    sample_doc_id, sample_status = image_virtual_docs[0]
    sample_blob_id = (sample_status.metadata or {}).get("image_ids", [None])[0]
    _check("sample blob_id present in metadata", bool(sample_blob_id))

    print(f"\nTesting against preserved workspace:")
    print(f"  working_dir    : {working_dir}")
    print(f"  sample doc_id  : {sample_doc_id}")
    print(f"  sample blob_id : {sample_blob_id}")

    # --- Build test app and exercise endpoints ---
    # Importing lightrag.api.routers.* eventually triggers the server
    # config's argparse. Strip sys.argv first so parse_args sees a clean
    # command line and doesn't choke on our positional working_dir path.
    sys.argv = sys.argv[:1]

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from lightrag.api.routers.image_routes import create_image_routes
    from lightrag.api.routers.document_routes import create_document_routes

    print("\n== [Stage 7a] /images/{blob_id} endpoint ==")
    image_router = create_image_routes(rag, api_key=None)
    _check(
        "create_image_routes returned a router",
        image_router is not None,
    )

    app = FastAPI()
    app.include_router(image_router)
    client = TestClient(app)

    # GET /images/{blob_id} -> image bytes
    r1 = client.get(f"/images/{sample_blob_id}")
    print(f"  GET /images/{sample_blob_id[:20]}... -> {r1.status_code} ({len(r1.content)} bytes)")
    _check("GET /images/{blob_id} returns 200", r1.status_code == 200)
    _check(
        "GET /images/{blob_id} returns non-empty bytes",
        len(r1.content) > 1000,
    )
    _check(
        "GET /images/{blob_id} has image content-type",
        r1.headers.get("content-type", "").startswith("image/"),
        r1.headers.get("content-type", ""),
    )

    # GET /images/{blob_id}/metadata -> JSON
    r2 = client.get(f"/images/{sample_blob_id}/metadata")
    print(f"  GET /images/{sample_blob_id[:20]}.../metadata -> {r2.status_code}")
    _check("GET /images/metadata returns 200", r2.status_code == 200)
    mdata = r2.json()
    _check("metadata has blob_id", mdata.get("blob_id") == sample_blob_id)
    _check("metadata has caption_json", mdata.get("caption_json") is not None)
    _check("metadata has annotation_text", bool(mdata.get("annotation_text")))
    _check("metadata has source_doc_id", mdata.get("source_doc_id") == sample_doc_id)

    # 404 path
    r3 = client.get("/images/img-does-not-exist-abcdef")
    _check("GET unknown blob_id returns 404", r3.status_code == 404)

    # Bad prefix -> 400
    r3b = client.get("/images/chunk-notanimage")
    _check("GET non-img prefix returns 400", r3b.status_code == 400)

    print("\n== [Stage 7b] /documents/{doc_id}/images endpoint ==")

    class _DummyDocManager:
        input_dir = None
        supported_extensions = (".txt",)

        def is_supported_file(self, name):
            return True

    app2 = FastAPI()
    doc_router = create_document_routes(rag, _DummyDocManager(), api_key=None)
    app2.include_router(doc_router)
    client2 = TestClient(app2)

    r4 = client2.get(f"/documents/{sample_doc_id}/images")
    print(f"  GET /documents/{sample_doc_id[:24]}.../images -> {r4.status_code}")
    _check("GET /documents/{doc_id}/images returns 200", r4.status_code == 200)
    doc_images = r4.json()
    _check("endpoint echoes doc_id", doc_images.get("doc_id") == sample_doc_id)
    _check(
        "endpoint returns sample_blob_id in image_ids",
        sample_blob_id in doc_images.get("image_ids", []),
    )
    _check("endpoint reports modality=image", doc_images.get("modality") == "image")
    _check("endpoint count == 1", doc_images.get("count") == 1)

    # 404 for unknown doc
    r5 = client2.get("/documents/doc-img-unknown-1234567890abcdef/images")
    _check("GET unknown doc images returns 404", r5.status_code == 404)

    await rag.finalize_storages()
    print("\n\033[92mPhase 4 endpoint-only verification: ALL PASS\033[0m")


if __name__ == "__main__":
    asyncio.run(main())
