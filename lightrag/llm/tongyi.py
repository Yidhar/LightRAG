"""
Tongyi (Alibaba DashScope) multimodal embedding binding.

This module wraps the official ``dashscope`` SDK's
``MultiModalEmbedding.call`` to produce a
:class:`~lightrag.utils.MultimodalEmbeddingFunc` whose text and image
encoders land in the **same** semantic space (e.g. the
``tongyi-embedding-vision-*`` family). Text queries can therefore retrieve
image vectors directly, which is what enables cross-modal search in
LightRAG's image pipeline.

**Why the SDK and not OpenAI-compat HTTP:**
DashScope's OpenAI-compatible endpoint (``/compatible-mode/v1``) does NOT
support multimodal-embedding models — only text embeddings and chat
completions. The only supported path for multimodal embeddings is the
native DashScope service, and the ``dashscope`` SDK is the
officially-maintained, lowest-friction way to call it. See:

    https://www.alibabacloud.com/help/en/model-studio/multimodal-embedding-api-reference

**Dependency note:**
``dashscope`` is installed lazily via ``pipmaster`` on first import, which
matches the convention used by other LightRAG LLM bindings (see
``lightrag/llm/openai.py``). Users who never touch the multimodal pipeline
do not need to install it.

Typical usage::

    import os
    from lightrag import LightRAG
    from lightrag.llm.tongyi import tongyi_multimodal_embedding

    image_embedding_func = tongyi_multimodal_embedding(
        model=os.getenv("MM_EMB_MODEL", "tongyi-embedding-vision-plus"),
        embedding_dim=int(os.getenv("MM_EMB_DIM", "1152")),
        api_key=os.environ["DASHSCOPE_API_KEY"],
        # base_url is optional — only set it when routing via a private
        # gateway or an alternate region endpoint.
    )

    rag = LightRAG(..., image_embedding_func=image_embedding_func)
"""

from __future__ import annotations

import asyncio
import base64
from functools import partial
from http import HTTPStatus
from typing import Any

import numpy as np
import pipmaster as pm

# Lazy-install the dashscope SDK on first import. Matches the convention
# used by lightrag/llm/openai.py and other bindings.
if not pm.is_installed("dashscope"):
    pm.install("dashscope")

import dashscope  # noqa: E402
from dashscope import MultiModalEmbedding  # noqa: E402

from ..utils import MultimodalEmbeddingFunc, logger  # noqa: E402


class TongyiMultimodalEmbeddingError(RuntimeError):
    """Raised when the DashScope multimodal embedding SDK returns an error."""


def _unwrap_embeddings(resp: Any, embedding_dim: int) -> np.ndarray:
    """Extract embedding vectors from a DashScope SDK response.

    Returns a 2-D ``np.ndarray`` of shape ``(n_inputs, embedding_dim)``,
    ordered to match the original ``input`` list.
    """
    status = getattr(resp, "status_code", None)
    if status != HTTPStatus.OK:
        code = getattr(resp, "code", None)
        message = getattr(resp, "message", None)
        request_id = getattr(resp, "request_id", None)
        logger.error(
            f"Tongyi multimodal embedding failed: status={status} "
            f"code={code} message={message} request_id={request_id}"
        )
        raise TongyiMultimodalEmbeddingError(
            f"DashScope multimodal embedding failed: "
            f"status={status} code={code} message={message} "
            f"request_id={request_id}"
        )

    output = getattr(resp, "output", None)
    embeddings = output.get("embeddings") if isinstance(output, dict) else None
    if not isinstance(embeddings, list) or not embeddings:
        raise TongyiMultimodalEmbeddingError(
            f"DashScope response missing 'output.embeddings': {output}"
        )

    # Sort by reported index so result order always matches input order,
    # even if the SDK returns them unordered.
    embeddings = sorted(embeddings, key=lambda e: e.get("index", 0))
    vectors = [e["embedding"] for e in embeddings]

    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[1] != embedding_dim:
        raise TongyiMultimodalEmbeddingError(
            f"Unexpected embedding shape {arr.shape}, "
            f"expected (*, {embedding_dim}). "
            f"Check that embedding_dim matches the model's native output."
        )
    return arr


def _sync_call(
    inputs: list[dict[str, str]],
    *,
    model: str,
    api_key: str,
    embedding_dim: int,
) -> np.ndarray:
    """Run a single SDK call. Synchronous — wrap in ``asyncio.to_thread``."""
    resp = MultiModalEmbedding.call(
        api_key=api_key,
        model=model,
        input=inputs,
    )
    return _unwrap_embeddings(resp, embedding_dim)


def _chunk(seq: list, size: int):
    """Yield successive chunks of ``size`` from ``seq``."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


async def _text_encode(
    texts: list[str],
    *,
    model: str,
    api_key: str,
    embedding_dim: int,
    batch_size: int,
) -> np.ndarray:
    """Encode a list of text strings into the shared multimodal space."""
    if not texts:
        return np.zeros((0, embedding_dim), dtype=np.float32)

    batch_tasks = []
    for group in _chunk(list(texts), batch_size):
        inputs = [{"text": t} for t in group]
        # The dashscope SDK is synchronous, so we offload each batch to a
        # thread. asyncio.gather then parallelizes the batches.
        batch_tasks.append(
            asyncio.to_thread(
                _sync_call,
                inputs,
                model=model,
                api_key=api_key,
                embedding_dim=embedding_dim,
            )
        )
    results = await asyncio.gather(*batch_tasks)
    return np.concatenate(results, axis=0)


async def _image_encode(
    images: list[bytes | str],
    *,
    model: str,
    api_key: str,
    embedding_dim: int,
    batch_size: int,
    image_mime_type: str = "image/jpeg",
) -> np.ndarray:
    """Encode a list of images into the shared multimodal space.

    Each element may be:
        - ``bytes``: raw image content (base64-encoded inline as a data URI).
        - ``str`` starting with ``http://``, ``https://``, or ``data:``:
          passed to the SDK as-is.
        - Any other ``str``: treated as a local file path and loaded from disk.
    """
    if not images:
        return np.zeros((0, embedding_dim), dtype=np.float32)

    def to_input(img: bytes | str) -> dict[str, str]:
        if isinstance(img, (bytes, bytearray)):
            b64 = base64.b64encode(bytes(img)).decode("ascii")
            return {"image": f"data:{image_mime_type};base64,{b64}"}
        if isinstance(img, str):
            if img.startswith(("http://", "https://", "data:")):
                return {"image": img}
            with open(img, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return {"image": f"data:{image_mime_type};base64,{b64}"}
        raise TypeError(
            f"image_encode inputs must be bytes or str, got {type(img).__name__}"
        )

    batch_tasks = []
    for group in _chunk(list(images), batch_size):
        inputs = [to_input(i) for i in group]
        batch_tasks.append(
            asyncio.to_thread(
                _sync_call,
                inputs,
                model=model,
                api_key=api_key,
                embedding_dim=embedding_dim,
            )
        )
    results = await asyncio.gather(*batch_tasks)
    return np.concatenate(results, axis=0)


def tongyi_multimodal_embedding(
    *,
    model: str,
    embedding_dim: int,
    api_key: str,
    base_url: str | None = None,
    batch_size: int = 10,
) -> MultimodalEmbeddingFunc:
    """Build a :class:`MultimodalEmbeddingFunc` via the dashscope SDK.

    ``api_key`` and ``model`` are required. ``base_url`` is optional: when
    provided, it overrides the SDK's default HTTP API base (useful for
    private gateways, alternate regions, or test proxies). The library
    never hardcodes a default endpoint — omit ``base_url`` to let the SDK
    use its built-in default.

    Args:
        model: DashScope multimodal embedding model id, e.g.
            ``"tongyi-embedding-vision-plus"``.
        embedding_dim: Output dimension of the model, e.g. ``1152`` for
            ``tongyi-embedding-vision-plus``. Used for empty-input fallback
            shapes and for validating every API response.
        api_key: DashScope API key, forwarded per-call to the SDK. This is
            process-safe (no global mutation) and works in multi-tenant
            scenarios where different instances use different keys.
        base_url: Optional SDK HTTP base URL override. When set, writes to
            the module-level ``dashscope.base_http_api_url`` global.
            ⚠ This global is PROCESS-WIDE — if you need to route multiple
            DashScope instances to different gateways simultaneously, call
            the HTTP API directly instead of using this factory.
        batch_size: Number of items (texts or images) sent per SDK call.
            Defaults to 10 (matches DashScope's recommended cap for the
            multimodal embedding endpoint). Batches are dispatched
            concurrently via ``asyncio.gather``.

    Returns:
        A :class:`MultimodalEmbeddingFunc` whose ``text_encode`` and
        ``image_encode`` methods both produce vectors in the same semantic
        space. Its default ``__call__`` routes to ``text_encode``, so the
        instance can also be dropped into any slot expecting a plain text
        ``EmbeddingFunc``-compatible callable (this is what enables
        cross-modal retrieval — a text query encoded via ``__call__`` lands
        in the same space as indexed image vectors).
    """
    if not model:
        raise ValueError("tongyi_multimodal_embedding: 'model' must be non-empty")
    if not api_key:
        raise ValueError("tongyi_multimodal_embedding: 'api_key' must be non-empty")
    if embedding_dim <= 0:
        raise ValueError(
            f"tongyi_multimodal_embedding: 'embedding_dim' must be positive, "
            f"got {embedding_dim}"
        )

    if base_url:
        # dashscope SDK uses a module-level global for the HTTP base URL.
        # Setting it once here is fine for single-endpoint deployments.
        dashscope.base_http_api_url = base_url
        logger.info(
            f"Tongyi multimodal embedding using custom base_url: {base_url}"
        )

    bound_text = partial(
        _text_encode,
        model=model,
        api_key=api_key,
        embedding_dim=embedding_dim,
        batch_size=batch_size,
    )
    bound_image = partial(
        _image_encode,
        model=model,
        api_key=api_key,
        embedding_dim=embedding_dim,
        batch_size=batch_size,
    )

    return MultimodalEmbeddingFunc(
        embedding_dim=embedding_dim,
        text_encode=bound_text,
        image_encode=bound_image,
        model_name=model,
    )
