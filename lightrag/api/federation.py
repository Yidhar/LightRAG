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

Only the non-streaming ``/query`` endpoint consumes this helper today;
``/query/stream`` and ``/query/data`` are intentionally left on the
single-rag fast path until streaming federation semantics are nailed
down in a follow-up commit.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Request

from lightrag.utils import logger


def collect_workspace_kb_ids(state: Any, workspace_id: str) -> list[str]:
    """Return every KB id registered for ``workspace_id``.

    Empty result means the caller should not federate — either the
    registry is missing, the workspace has no KB record (legacy shape),
    or there is only one KB so federation degenerates to a plain call.
    """
    registry = getattr(state, "kb_registry", None)
    if registry is None:
        return []

    lister = getattr(registry, "list", None) or getattr(registry, "list_for_workspace", None)
    if not callable(lister):
        return []

    try:
        raw = lister(workspace_id)
    except TypeError:
        # Older registry signature accepted no arguments.
        raw = lister()
    except Exception:  # pragma: no cover — defensive
        logger.exception("Failed to enumerate KBs for workspace %s", workspace_id)
        return []

    ids: list[str] = []
    for entry in raw or []:
        # Registry entries come in a few shapes depending on how they
        # were persisted: dicts from the JSON registry, ORM rows, or
        # bare ``KnowledgeBase`` dataclasses. Handle the usual ones.
        candidate: Any
        if isinstance(entry, dict):
            if entry.get("workspace_id") and entry["workspace_id"] != workspace_id:
                continue
            candidate = entry.get("kb_id") or entry.get("id")
        else:
            entry_workspace = getattr(entry, "workspace_id", None)
            if entry_workspace and entry_workspace != workspace_id:
                continue
            candidate = getattr(entry, "kb_id", None) or getattr(entry, "id", None)

        if candidate and candidate not in ids:
            ids.append(candidate)

    return ids


def _format_federated_response(results: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Merge per-KB aquery_llm results into the single-rag response shape."""
    parts: list[str] = []
    merged_references: list[dict[str, Any]] = []
    merged_chunks: list[dict[str, Any]] = []
    seen_refs: set[tuple[str, str]] = set()

    for kb_id, result in results:
        data = result.get("data") or {}
        llm_response = result.get("llm_response") or {}
        text = llm_response.get("content") or ""
        if text:
            parts.append(f"### Knowledge base: {kb_id}\n\n{text.strip()}")

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

    Federation applies when ALL of these hold:

    - KB isolation is enabled on app.state
    - A RagFactory is attached on app.state
    - The request's resolved kb_id is the configured default_kb_id
      (equivalently, the caller asked for "the default KB", i.e. "all")
    - The workspace has more than one registered KB

    ``concurrency_limit`` bounds parallel LLM calls so a workspace with
    dozens of KBs does not accidentally DoS the LLM provider.
    """
    from lightrag.api.dependencies import get_request_context

    state = request.app.state

    if not getattr(state, "enable_kb_isolation", False):
        return None

    rag_factory = getattr(state, "rag_factory", None)
    if rag_factory is None:
        return None

    context = get_request_context(request)
    workspace_id = context.workspace_id or state.default_workspace_id
    kb_id = context.kb_id or state.default_kb_id

    default_kb_id = state.default_kb_id
    if kb_id != default_kb_id:
        # Caller explicitly targeted a non-default KB; respect that and
        # stay on the single-rag path.
        return None

    kb_ids = collect_workspace_kb_ids(state, workspace_id)
    if len(kb_ids) <= 1:
        # Single-KB (or zero-KB) workspace — nothing to federate.
        return None

    semaphore = asyncio.Semaphore(max(1, concurrency_limit))

    async def _run_one(one_kb_id: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            rag = await rag_factory.get(workspace_id, one_kb_id)
            result = await rag.aquery_llm(query, param=param)
            return one_kb_id, result

    tasks = [_run_one(each) for each in kb_ids]
    settled = await asyncio.gather(*tasks, return_exceptions=True)

    ok_results: list[tuple[str, dict[str, Any]]] = []
    for kb_id_, outcome in zip(kb_ids, settled):
        if isinstance(outcome, Exception):
            logger.warning(
                "Federated aquery_llm failed for workspace=%s kb=%s: %s",
                workspace_id,
                kb_id_,
                outcome,
            )
            continue
        # outcome is (kb_id, result)
        ok_results.append(outcome)  # type: ignore[arg-type]

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
