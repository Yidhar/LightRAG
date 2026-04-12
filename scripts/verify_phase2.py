"""Phase 2 smoke test: FileSystemBlobStorage round-trip and LightRAG wiring.

Exercises the blob storage in isolation (no network, no LLM calls) plus
verifies that the LightRAG dataclass correctly instantiates the multimodal
stores when ``image_embedding_func`` is set, and leaves them at ``None``
when it is not.

Run::

    uv run python scripts/verify_phase2.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import numpy as np

from lightrag.base import BaseBlobStorage
from lightrag.kg.filesystem_blob_impl import (
    FileSystemBlobStorage,
    _extension_for_mime,
)
from lightrag.namespace import NameSpace
from lightrag.utils import MultimodalEmbeddingFunc


def _check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  — {detail}" if detail else ""))
    if not condition:
        raise SystemExit(1)


async def test_blob_storage_isolated() -> None:
    print("\n== [A] FileSystemBlobStorage in isolation ==")
    tmpdir = tempfile.mkdtemp(prefix="lightrag_blob_test_")
    try:
        store = FileSystemBlobStorage(
            namespace=NameSpace.BLOB_STORE_IMAGES,
            workspace="test_ws_a",
            global_config={"working_dir": tmpdir},
        )
        _check("FileSystemBlobStorage is a BaseBlobStorage", isinstance(store, BaseBlobStorage))

        await store.initialize()
        _check("initialize() created root dir", os.path.isdir(store._root), store._root)

        # put + get round-trip
        b1 = b"\x89PNG\r\n\x1a\nFAKE_PNG_DATA_001"
        ref1 = await store.put(
            "img-abc12345",
            b1,
            content_type="image/png",
            metadata={"source_doc_id": "doc-test-001", "source_page": 3},
        )
        _check("put() returns an existing file path", os.path.isfile(ref1), ref1)

        got = await store.get("img-abc12345")
        _check("get() returns identical bytes", got == b1, f"{len(got or b'')} bytes")

        ref = await store.get_reference("img-abc12345")
        _check("get_reference() returns absolute path", ref == ref1 and os.path.isabs(ref))

        meta = await store.get_metadata("img-abc12345")
        required = (
            "blob_id",
            "namespace",
            "workspace",
            "content_type",
            "size",
            "created_at",
            "extra",
            "filename",
        )
        _check("metadata sidecar non-None", meta is not None)
        _check(
            "metadata has all required keys",
            all(k in meta for k in required),
            f"keys={sorted(meta.keys())}",
        )
        _check("metadata.content_type round-trips", meta["content_type"] == "image/png")
        _check("metadata.size matches", meta["size"] == len(b1))
        _check(
            "metadata.extra merged",
            meta["extra"]["source_doc_id"] == "doc-test-001"
            and meta["extra"]["source_page"] == 3,
        )

        # exists
        _check("exists() returns True", await store.exists("img-abc12345") is True)
        _check("exists() returns False for missing", await store.exists("img-nope") is False)

        # Fanout naming
        fanout = store._fanout_dir("img-abc12345")
        _check("fanout dir = 'ab'", os.path.basename(fanout) == "ab", fanout)

        # Re-put with different content_type cleans up old file
        b2 = b"FAKE_JPG_DATA_002"
        ref2 = await store.put("img-abc12345", b2, content_type="image/jpeg")
        _check("re-put with .jpg ext", ref2.endswith(".jpg"))
        _check("old .png file removed", not os.path.isfile(ref1))
        got2 = await store.get("img-abc12345")
        _check("re-put content is new bytes", got2 == b2)

        # Delete
        removed = await store.delete("img-abc12345")
        _check("delete() returns True", removed is True)
        _check("after delete: exists False", await store.exists("img-abc12345") is False)
        _check("after delete: get None", await store.get("img-abc12345") is None)
        _check("after delete: metadata None", await store.get_metadata("img-abc12345") is None)

        # Workspace isolation
        store_b = FileSystemBlobStorage(
            namespace=NameSpace.BLOB_STORE_IMAGES,
            workspace="test_ws_b",
            global_config={"working_dir": tmpdir},
        )
        await store_b.initialize()
        await store_b.put("img-shared-id", b"WORKSPACE_B_DATA", content_type="image/png")

        _check("ws_a doesn't see ws_b data", await store.exists("img-shared-id") is False)
        _check("ws_b sees its own data", await store_b.exists("img-shared-id") is True)
        _check(
            "ws_b get() returns correct bytes",
            (await store_b.get("img-shared-id")) == b"WORKSPACE_B_DATA",
        )
        _check("ws_a and ws_b have different roots", store._root != store_b._root)

        # drop() clears namespace
        drop_result = await store_b.drop()
        _check(
            "drop() returns success",
            drop_result == {"status": "success", "message": "data dropped"},
        )
        _check(
            "after drop: ws_b data gone", await store_b.exists("img-shared-id") is False
        )
        _check("after drop: root recreated empty", os.path.isdir(store_b._root))

        # Mime -> ext table
        cases = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
            "image/tiff": ".tiff",
            "application/octet-stream": ".bin",
            "image/png; charset=binary": ".png",
            "unknown/weird": ".bin",
        }
        for ct, expected in cases.items():
            _check(
                f"_extension_for_mime({ct!r}) == {expected}",
                _extension_for_mime(ct) == expected,
            )

        # Error paths
        try:
            await store.put("img-x", "not bytes", content_type="image/png")
            _check("str data rejected", False, "TypeError not raised")
        except TypeError:
            _check("str data rejected", True)

        try:
            await store.put("", b"x", content_type="image/png")
            _check("empty blob_id rejected", False, "ValueError not raised")
        except ValueError:
            _check("empty blob_id rejected", True)

        # Missing-blob reads return None, delete returns False
        _check(
            "missing get() -> None", await store.get("img-does-not-exist") is None
        )
        _check(
            "missing get_reference() -> None",
            await store.get_reference("img-does-not-exist") is None,
        )
        _check(
            "missing get_metadata() -> None",
            await store.get_metadata("img-does-not-exist") is None,
        )
        _check(
            "missing delete() -> False",
            await store.delete("img-does-not-exist") is False,
        )

        await store.finalize()
        await store_b.finalize()

    finally:
        if os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


async def test_lightrag_wiring() -> None:
    print("\n== [B] LightRAG dataclass wiring ==")
    # Stub embedding funcs so LightRAG.__post_init__ doesn't complain.
    async def fake_text_embed(texts):
        return np.zeros((len(texts), 8), dtype=np.float32)

    from lightrag.utils import EmbeddingFunc

    text_emb = EmbeddingFunc(
        embedding_dim=8,
        func=fake_text_embed,
        max_token_size=8192,
    )

    async def fake_text_encode(texts):
        return np.zeros((len(texts), 8), dtype=np.float32)

    async def fake_image_encode(imgs):
        return np.ones((len(imgs), 8), dtype=np.float32)

    mm_emb = MultimodalEmbeddingFunc(
        embedding_dim=8,
        text_encode=fake_text_encode,
        image_encode=fake_image_encode,
        model_name="fake-mm",
    )

    # Import LightRAG only now to keep module-level side effects scoped
    from lightrag import LightRAG

    # ---- B.1: text-only LightRAG: multimodal fields stay None ----
    tmpdir1 = tempfile.mkdtemp(prefix="lightrag_text_only_")
    try:
        rag_text = LightRAG(
            working_dir=tmpdir1,
            embedding_func=text_emb,
            llm_model_func=lambda *a, **kw: "stub",
        )
        _check("text-only: image_blob_store is None", rag_text.image_blob_store is None)
        _check("text-only: image_metadata is None", rag_text.image_metadata is None)
        _check(
            "text-only: image_embedding_func is None",
            rag_text.image_embedding_func is None,
        )
        _check(
            "text-only: vision_model_func is None",
            rag_text.vision_model_func is None,
        )
        # Original storages still present
        _check("text-only: full_docs present", rag_text.full_docs is not None)
        _check("text-only: chunks_vdb present", rag_text.chunks_vdb is not None)
    finally:
        if os.path.isdir(tmpdir1):
            shutil.rmtree(tmpdir1, ignore_errors=True)

    # ---- B.2: multimodal LightRAG: blob store + image metadata instantiated ----
    tmpdir2 = tempfile.mkdtemp(prefix="lightrag_mm_")
    try:
        rag_mm = LightRAG(
            working_dir=tmpdir2,
            workspace="mm_test",
            embedding_func=text_emb,
            llm_model_func=lambda *a, **kw: "stub",
            image_embedding_func=mm_emb,
            vision_model_func=lambda *a, **kw: "stub_vision",
        )
        _check(
            "multimodal: image_blob_store instantiated",
            rag_mm.image_blob_store is not None,
        )
        _check(
            "multimodal: image_blob_store is BaseBlobStorage",
            isinstance(rag_mm.image_blob_store, BaseBlobStorage),
        )
        _check(
            "multimodal: image_metadata instantiated",
            rag_mm.image_metadata is not None,
        )
        _check(
            "multimodal: image_embedding_func set",
            rag_mm.image_embedding_func is mm_emb,
        )
        _check(
            "multimodal: vision_model_func set",
            rag_mm.vision_model_func is not None,
        )

        # Initialize storages and round-trip a blob through rag.image_blob_store
        await rag_mm.initialize_storages()
        test_bytes = b"\x89PNG\r\n\x1a\nTEST_VIA_LIGHTRAG"
        await rag_mm.image_blob_store.put(
            "img-wired-test",
            test_bytes,
            content_type="image/png",
            metadata={"source": "phase2_smoke"},
        )
        round_trip = await rag_mm.image_blob_store.get("img-wired-test")
        _check("round-trip via rag.image_blob_store", round_trip == test_bytes)

        # image_metadata KV round-trip
        await rag_mm.image_metadata.upsert(
            {
                "img-wired-test": {
                    "blob_ref": "fake://ref",
                    "caption": "test caption",
                    "source_doc_id": "doc-stub",
                }
            }
        )
        got = await rag_mm.image_metadata.get_by_id("img-wired-test")
        _check("image_metadata KV stores record", got is not None)
        _check(
            "image_metadata KV round-trips caption",
            got.get("caption") == "test caption",
        )

        # Verify the physical layout is under the per-workspace blob subdir
        expected_root = os.path.join(tmpdir2, "blobs", "mm_test", "image_blobs")
        _check(
            "blob store root path follows workspace convention",
            rag_mm.image_blob_store._root == expected_root,
            rag_mm.image_blob_store._root,
        )

        await rag_mm.finalize_storages()
    finally:
        if os.path.isdir(tmpdir2):
            shutil.rmtree(tmpdir2, ignore_errors=True)


async def main() -> None:
    try:
        await test_blob_storage_isolated()
        await test_lightrag_wiring()
    except SystemExit:
        print("\n-- Phase 2 smoke test FAILED --")
        sys.exit(1)
    print("\n-- Phase 2 smoke test: ALL PASS --")


if __name__ == "__main__":
    asyncio.run(main())
