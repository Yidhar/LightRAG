"""
KB federation for workspace-level queries (Phase D).

When a caller targets ``kb_id == 'default'`` (the URL-less, post-Phase-A
shape the UI uses) and the workspace owns more than one knowledge base,
we want the query to span every KB in the workspace rather than silently
hitting the literal ``default`` KB.

This module provides:

- ``collect_workspace_kb_ids(state, workspace_id)`` — enumerate every KB
  id registered for a workspace.
- ``federated_aquery_llm(request, query, param)`` — fan out ``aquery_llm``
  across those KBs in parallel, then merge the LLM responses plus
  reference / chunk lists into a single shape that matches what a single
  ``rag.aquery_llm`` call would have returned. When federation does not
  apply (isolation disabled, or only one KB), the function returns
  ``None`` so the caller can fall back to its own single-rag path.

All three query surfaces — ``/query``, ``/query/stream``, ``/query/data`` —
now route through here. ``/query/stream`` federation is serial: the
server runs each KB's streaming LLM call one-after-another and prefixes
each block with a ``### Knowledge base: <id>`` marker so clients can
visually split the output. Parallel token interleave was considered
and rejected — the resulting stream would be unreadable without
per-chunk KB tags on every NDJSON line.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import Request

from lightrag.utils import logger


def collect_workspace_kb_ids(state: Any, workspace_id: str) -> list[str]:
    """Return every KB id linked to ``workspace_id``.

    Reads from the v2 link table on ``kb_registry``: a KB shows up here
    iff the workspace has a row in ``workspace_kb_links``. Shared KBs
    (same KB linked to multiple workspaces) are returned once per
    workspace that links them, exactly as the user expects for the
    "workspace owns a reference, not the data" model.
    """
    registry = getattr(state, "kb_registry", None)
    if registry is None:
        return []

    # list_kbs returns KnowledgeBase dataclasses in link order.
    try:
        records = registry.list_kbs(workspace_id)
    except Exception:  # pragma: no cover — defensive
        logger.exception("Failed to enumerate KBs for workspace %s", workspace_id)
        return []

    ids: list[str] = []
    for record in records or []:
        candidate = getattr(record, "id", None)
        if candidate and candidate not in ids:
            ids.append(str(candidate))
    return ids


def _format_federated_response(results: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Merge per-KB aquery_llm results into the single-rag response shape.

    The per-KB ``### Knowledge base: <id>`` header is emitted ONLY when
    more than one KB contributed content — otherwise a workspace with a
    single linked KB would have every answer prefixed by a useless
    single-section header. Reference IDs are still namespaced so the
    wire format stays consistent regardless of KB count.
    """
    parts: list[str] = []
    merged_references: list[dict[str, Any]] = []
    merged_chunks: list[dict[str, Any]] = []
    seen_refs: set[tuple[str, str]] = set()

    # Count how many KBs actually returned content — drives the
    # single-shard unwrap below.
    content_bearing = sum(
        1
        for _, result in results
        if ((result.get("llm_response") or {}).get("content") or "").strip()
    )

    for kb_id, result in results:
        data = result.get("data") or {}
        llm_response = result.get("llm_response") or {}
        text = (llm_response.get("content") or "").strip()
        if text:
            if content_bearing > 1:
                parts.append(f"### Knowledge base: {kb_id}\n\n{text}")
            else:
                parts.append(text)

        for ref in data.get("references") or []:
            # Reference ids are per-rag sequential ("1", "2", …). Namespace
            # them so they remain unique when concatenated across KBs.
            key = (str(kb_id), str(ref.get("reference_id", "")))
            if key in seen_refs:
                continue
            seen_refs.add(key)
            scoped = dict(ref)
            scoped["reference_id"] = f"{kb_id}:{ref.get('reference_id', '')}"
            scoped["knowledge_base_id"] = kb_id
            merged_references.append(scoped)

        for chunk in data.get("chunks") or []:
            scoped = dict(chunk)
            orig_ref = str(chunk.get("reference_id", ""))
            if orig_ref:
                scoped["reference_id"] = f"{kb_id}:{orig_ref}"
            scoped["knowledge_base_id"] = kb_id
            merged_chunks.append(scoped)

    combined_content = "\n\n".join(parts).strip() or (
        "No relevant context found across the federated knowledge bases."
    )

    return {
        "llm_response": {"content": combined_content},
        "data": {
            "references": merged_references,
            "chunks": merged_chunks,
            "federation": {
                "kb_ids": [kb_id for kb_id, _ in results],
                "kb_count": len(results),
            },
        },
    }


def _should_federate(request: Request) -> tuple[str, list[str]] | None:
    """
    Shared predicate used by every federated entry point.

    Returns ``(workspace_id, kb_ids)`` when federation applies, or
    ``None`` when the caller should fall back to its single-rag path.

    Semantics:
      * Caller sent an EXPLICIT non-default ``X-KB-Id`` — respect it,
        route to that single KB directly (no federation).
      * Caller implicitly hit the default KB fallback — ANY number of
        linked KBs triggers the federation path. The previous
        ``<= 1`` guard fell through to the single-rag code path with
        ``default_kb_id``, which raises 404 on workspaces whose only
        linked KB is NOT "default" (the common case for
        self-registered workspaces). Covering the 1-KB case here
        routes the query to that one KB instead — still via fan-out
        so the response merger runs and namespaces references
        uniformly. ``_format_federated_response`` suppresses the
        multi-KB header when only one shard contributes content,
        so 1-KB output stays clean.
      * Zero linked KBs — fall through; the downstream 404 is the
        correct signal that the workspace has nothing to query.
    """
    from lightrag.api.dependencies import get_request_context

    state = request.app.state

    if not getattr(state, "enable_kb_isolation", False):
        return None
    if getattr(state, "rag_factory", None) is None:
        return None

    context = get_request_context(request)
    workspace_id = context.workspace_id or state.default_workspace_id
    kb_id = context.kb_id or state.default_kb_id
    if kb_id != state.default_kb_id:
        # Caller explicitly targeted a non-default KB; respect that.
        return None

    kb_ids = collect_workspace_kb_ids(state, workspace_id)
    if not kb_ids:
        return None
    return workspace_id, kb_ids


async def _fan_out(
    request: Request,
    workspace_id: str,
    kb_ids: list[str],
    runner,
    *,
    concurrency_limit: int = 4,
) -> list[tuple[str, Any]]:
    """Parallel per-KB runner with a bounded semaphore.

    ``runner(rag) -> Any`` is awaited for each KB; results preserve the
    KB id so downstream merges can namespace identifiers. Per-shard
    exceptions are logged and dropped, never propagated.
    """
    rag_factory = request.app.state.rag_factory
    semaphore = asyncio.Semaphore(max(1, concurrency_limit))

    async def _run_one(one_kb_id: str) -> tuple[str, Any]:
        async with semaphore:
            rag = await rag_factory.get(workspace_id, one_kb_id)
            return one_kb_id, await runner(rag)

    tasks = [_run_one(each) for each in kb_ids]
    settled = await asyncio.gather(*tasks, return_exceptions=True)

    ok: list[tuple[str, Any]] = []
    for kb_id_, outcome in zip(kb_ids, settled):
        if isinstance(outcome, Exception):
            logger.warning(
                "Federated call failed for workspace=%s kb=%s: %s",
                workspace_id,
                kb_id_,
                outcome,
            )
            continue
        ok.append(outcome)  # type: ignore[arg-type]
    return ok


async def federated_aquery_llm(
    request: Request,
    query: str,
    param: Any,
    *,
    concurrency_limit: int = 4,
) -> dict[str, Any] | None:
    """
    Run ``aquery_llm`` across every KB in the current workspace and merge
    the results. Returns ``None`` when federation does not apply — the
    caller should fall back to the default single-rag code path.

    ``concurrency_limit`` bounds parallel LLM calls so a workspace with
    dozens of KBs does not accidentally DoS the LLM provider.
    """
    decision = _should_federate(request)
    if decision is None:
        return None
    workspace_id, kb_ids = decision

    async def _run(rag):
        return await rag.aquery_llm(query, param=param)

    ok_results = await _fan_out(
        request,
        workspace_id,
        kb_ids,
        _run,
        concurrency_limit=concurrency_limit,
    )

    if not ok_results:
        # All shards errored; surface as an empty federated response
        # rather than crashing the request.
        return {
            "llm_response": {
                "content": "Every federated knowledge base raised an error.",
            },
            "data": {
                "references": [],
                "chunks": [],
                "federation": {"kb_ids": kb_ids, "kb_count": 0},
            },
        }

    return _format_federated_response(ok_results)


def _merge_data_shards(
    kb_ids: list[str],
    shards: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Merge per-KB ``aquery_data`` results into the unified shape
    consumed by ``/query/data``.

    Reference ids are namespaced the same way as the LLM response
    merger so references coming back to the UI from stream + data +
    llm paths stay internally consistent.
    """
    entities: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    seen_refs: set[tuple[str, str]] = set()

    totals = {
        "total_entities_found": 0,
        "total_relations_found": 0,
        "entities_after_truncation": 0,
        "relations_after_truncation": 0,
        "final_chunks_count": 0,
    }

    def _scope(obj: dict[str, Any], kb_id: str) -> dict[str, Any]:
        scoped = dict(obj)
        orig_ref = str(scoped.get("reference_id", ""))
        if orig_ref:
            scoped["reference_id"] = f"{kb_id}:{orig_ref}"
        scoped["knowledge_base_id"] = kb_id
        return scoped

    for kb_id, shard in shards:
        data = shard.get("data") or {}
        for entity in data.get("entities") or []:
            entities.append(_scope(entity, kb_id))
        for rel in data.get("relationships") or []:
            relationships.append(_scope(rel, kb_id))
        for chunk in data.get("chunks") or []:
            chunks.append(_scope(chunk, kb_id))
        for ref in data.get("references") or []:
            key = (kb_id, str(ref.get("reference_id", "")))
            if key in seen_refs:
                continue
            seen_refs.add(key)
            references.append(_scope(ref, kb_id))

        shard_meta = (shard.get("metadata") or {}).get("processing_info") or {}
        for counter_key in totals:
            value = shard_meta.get(counter_key)
            if isinstance(value, int):
                totals[counter_key] += value

    # Metadata keeps the first successful shard's query_mode / keywords
    # so the caller still sees something meaningful; federation-specific
    # counters land in the federation sub-block.
    first_meta: dict[str, Any] = {}
    for _, shard in shards:
        candidate = shard.get("metadata") or {}
        if candidate:
            first_meta = candidate
            break

    metadata = {
        "query_mode": first_meta.get("query_mode"),
        "keywords": first_meta.get("keywords", {"high_level": [], "low_level": []}),
        "processing_info": totals,
        "federation": {
            "kb_ids": [kb_id for kb_id, _ in shards],
            "kb_count": len(shards),
            "requested_kb_ids": kb_ids,
        },
    }

    return {
        "status": "success" if shards else "failure",
        "message": (
            "Federated query executed across "
            f"{len(shards)}/{len(kb_ids)} knowledge bases."
        ),
        "data": {
            "entities": entities,
            "relationships": relationships,
            "chunks": chunks,
            "references": references,
        },
        "metadata": metadata,
    }


async def federated_aquery_data(
    request: Request,
    query: str,
    param: Any,
    *,
    concurrency_limit: int = 4,
) -> dict[str, Any] | None:
    """
    Run ``aquery_data`` across every KB in the workspace and merge the
    structured retrieval shards. Returns ``None`` when federation does
    not apply so the caller falls back to the single-rag path.
    """
    decision = _should_federate(request)
    if decision is None:
        return None
    workspace_id, kb_ids = decision

    async def _run(rag):
        return await rag.aquery_data(query, param=param)

    shards = await _fan_out(
        request,
        workspace_id,
        kb_ids,
        _run,
        concurrency_limit=concurrency_limit,
    )
    if not shards:
        return {
            "status": "failure",
            "message": "Every federated knowledge base raised an error.",
            "data": {
                "entities": [],
                "relationships": [],
                "chunks": [],
                "references": [],
            },
            "metadata": {
                "federation": {"kb_ids": kb_ids, "kb_count": 0},
            },
        }

    return _merge_data_shards(kb_ids, shards)


async def federated_stream(
    request: Request,
    query: str,
    param: Any,
    *,
    include_references: bool,
    include_chunk_content: bool,
) -> AsyncIterator[str] | None:
    """
    Serial streaming federation for ``/query/stream``.

    Returns ``None`` when federation does not apply. When it does, the
    caller should hand the returned async iterator straight to a
    ``StreamingResponse`` — each yielded string is a ready-to-send NDJSON
    line terminated with ``\\n``.

    Stream ordering, per KB in registry order:

    1. (once, first KB only) merged references line, if requested
    2. ``### Knowledge base: <id>`` header line as ``{"response": "..."}``
    3. forwarded token chunks from the KB's streaming LLM call
    4. repeat 2-3 for each remaining KB

    On per-KB errors we emit an ``{"error": "..."}`` line for that KB
    and continue with the next one — one broken KB should not sink the
    whole response.
    """
    decision = _should_federate(request)
    if decision is None:
        return None
    workspace_id, kb_ids = decision

    async def _generator() -> AsyncIterator[str]:
        state = request.app.state
        rag_factory = state.rag_factory
        references_emitted = False

        for index, kb_id in enumerate(kb_ids):
            try:
                rag = await rag_factory.get(workspace_id, kb_id)
                # Force streaming on for this shard — the caller's param
                # already has stream=True but be explicit in case the
                # caller mutates it between shards.
                param.stream = True
                result = await rag.aquery_llm(query, param=param)
            except Exception as exc:
                logger.warning(
                    "Federated stream shard failed workspace=%s kb=%s: %s",
                    workspace_id,
                    kb_id,
                    exc,
                )
                yield f"{json.dumps({'error': f'[{kb_id}] {exc}'})}\n"
                continue

            data = result.get("data") or {}
            llm_response = result.get("llm_response") or {}

            # Emit the merged-style references line ONCE, using the
            # first KB's references. We namespace them by kb_id so the
            # UI can still tell which KB owns each citation. This keeps
            # the wire shape compatible with the non-federated stream
            # (single references line up front) while reflecting that
            # later KBs have their own citation sets — those end up in
            # their header block as inline hints if the client cares.
            if include_references and not references_emitted:
                refs_out: list[dict[str, Any]] = []
                for ref in data.get("references") or []:
                    scoped = dict(ref)
                    orig_ref = str(scoped.get("reference_id", ""))
                    if orig_ref:
                        scoped["reference_id"] = f"{kb_id}:{orig_ref}"
                    scoped["knowledge_base_id"] = kb_id
                    refs_out.append(scoped)

                if include_chunk_content:
                    ref_id_to_content: dict[str, list[str]] = {}
                    for chunk in data.get("chunks") or []:
                        rid = str(chunk.get("reference_id", ""))
                        content = chunk.get("content") or ""
                        if rid and content:
                            ref_id_to_content.setdefault(
                                f"{kb_id}:{rid}", []
                            ).append(content)
                    for ref in refs_out:
                        content = ref_id_to_content.get(ref["reference_id"])
                        if content:
                            ref["content"] = content

                yield f"{json.dumps({'references': refs_out})}\n"
                references_emitted = True

            # Per-KB separator so the client can visually split shards.
            # Blank line before the header on every KB except the first
            # so the markdown stays tidy when rendered.
            # NOTE: build ``header_text`` as a plain string first —
            # embedding ``\n`` inside a nested f-string expression is a
            # SyntaxError on Python 3.11 (relaxed in 3.12+ under PEP 701).
            separator = "\n\n" if index > 0 else ""
            header_text = f"{separator}### Knowledge base: {kb_id}\n\n"
            yield f"{json.dumps({'response': header_text})}\n"

            if llm_response.get("is_streaming"):
                response_stream = llm_response.get("response_iterator")
                if response_stream:
                    try:
                        async for chunk in response_stream:
                            if chunk:
                                yield f"{json.dumps({'response': chunk})}\n"
                    except Exception as exc:
                        logger.error(
                            "Federated stream chunk error kb=%s: %s", kb_id, exc
                        )
                        yield f"{json.dumps({'error': f'[{kb_id}] {exc}'})}\n"
            else:
                # Non-streaming shard (cache hit, or the underlying rag
                # declined to stream) — emit the whole content as one
                # chunk to keep the transport shape uniform.
                content = llm_response.get("content") or ""
                if content:
                    yield f"{json.dumps({'response': content})}\n"

    return _generator()
