"""
Lazy LightRAG runtime factory keyed by knowledge-base id.

Each KB has its own storage namespace (``rag_storage/<kb_id>/`` for
the JSON backends, keyed collections/prefixes for Postgres/Neo4j/etc).
Because KBs are now *shared* between workspaces via the registry's
link table, the cache key is just ``kb_id`` — the same runtime
instance serves every workspace that references the KB. There's no
per-workspace storage layer anymore.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from lightrag.api.kb_registry import KnowledgeBaseRegistry
from lightrag.api.models.kb import KnowledgeBase
from lightrag.utils import logger

RagBuilder = Callable[
    [str, KnowledgeBase | None],
    Any | Awaitable[Any],
]


class RagFactory:
    def __init__(
        self,
        *,
        builder: RagBuilder,
        registry: KnowledgeBaseRegistry | None = None,
        kb_separator: str = "__",  # kept for backwards-compat with callers
    ) -> None:
        self._builder = builder
        self._registry = registry
        # Historically used to join ``workspace__kb`` into a storage
        # namespace. Preserved as an attribute so pre-refactor callers
        # that still read ``kb_separator`` don't crash at import time,
        # but ``compose_workspace`` no longer uses it.
        self._kb_separator = kb_separator or "__"
        self._instances: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @property
    def kb_separator(self) -> str:
        return self._kb_separator

    def compose_workspace(self, _workspace_id: str, kb_id: str) -> str:
        """Return the storage namespace for ``kb_id``.

        Second-positional ``workspace_id`` is accepted for source-
        compatibility with the v1 callers but ignored — the namespace
        is just the KB id since one KB can now live in many workspaces
        but only has a single storage footprint.
        """
        return str(kb_id)

    def prime(self, _workspace_id: str, kb_id: str, rag: Any) -> None:
        """Install a pre-built runtime into the cache (used at startup
        to register the default-KB instance that was built eagerly)."""
        self._instances[str(kb_id)] = rag

    async def get(self, workspace_id: str, kb_id: str) -> Any:
        """Return the runtime for ``kb_id``, building it on first access.

        ``workspace_id`` is still required by the existing call sites
        (they pass it from the request context) and is used only to
        validate the KB is linked to that workspace — so a query in
        workspace A cannot secretly read from a KB only linked to
        workspace B. Pass an empty string to skip that check (the
        factory falls back to a global lookup, used by cross-workspace
        operations like document move).
        """
        key = str(kb_id)
        existing = self._instances.get(key)
        if existing is not None:
            return existing

        lock = await self._get_lock(key)
        async with lock:
            existing = self._instances.get(key)
            if existing is not None:
                return existing

            knowledge_base = None
            if self._registry is not None:
                # When a workspace scope is provided, enforce that the
                # KB is linked to it. The ``None`` path skips the gate
                # for system callers (e.g. move_document reading from
                # the source KB that belongs to a different workspace
                # than the request context).
                scope = workspace_id or None
                knowledge_base = self._registry.get_kb(scope, kb_id)
                if knowledge_base is None:
                    # Fall back to a global lookup so the error message
                    # disambiguates "KB does not exist" from "KB exists
                    # but is not linked to this workspace".
                    global_kb = self._registry.get_kb(None, kb_id)
                    if global_kb is None:
                        raise KeyError(
                            f"Knowledge base '{kb_id}' does not exist."
                        )
                    raise KeyError(
                        f"Knowledge base '{kb_id}' is not linked to workspace "
                        f"'{workspace_id}'."
                    )

            built = self._builder(key, knowledge_base)
            rag = await built if inspect.isawaitable(built) else built
            self._instances[key] = rag
            logger.info(
                "Initialized shared LightRAG runtime for kb='%s' (storage namespace='%s').",
                kb_id,
                key,
            )
            return rag

    async def evict(self, _workspace_id: str, kb_id: str) -> bool:
        key = str(kb_id)
        rag = self._instances.pop(key, None)
        self._locks.pop(key, None)
        if rag is None:
            return False

        finalize = getattr(rag, "finalize_storages", None)
        if finalize is None:
            return True

        result = finalize()
        if inspect.isawaitable(result):
            await result
        logger.info("Evicted shared LightRAG runtime for kb='%s'.", kb_id)
        return True

    async def finalize_all(self) -> None:
        cached_items = list(self._instances.items())
        self._instances.clear()

        failures: list[str] = []
        for kb_id, rag in cached_items:
            finalize = getattr(rag, "finalize_storages", None)
            if finalize is None:
                continue

            try:
                result = finalize()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover - exercised via logging path
                failures.append(kb_id)
                logger.error(
                    "Failed to finalize LightRAG runtime for kb='%s': %s",
                    kb_id,
                    exc,
                )

        self._locks.clear()

        if failures:
            logger.error(
                "LightRAG runtime finalization completed with failures for: %s",
                ", ".join(failures),
            )

    async def _get_lock(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock
