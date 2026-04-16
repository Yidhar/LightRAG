"""
Lazy LightRAG runtime factory keyed by workspace and knowledge-base id.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from lightrag.api.kb_registry import KnowledgeBaseRegistry
from lightrag.api.models.kb import KnowledgeBase
from lightrag.utils import logger

RagKey = tuple[str, str]
RagBuilder = Callable[
    [str, str, str, KnowledgeBase | None],
    Any | Awaitable[Any],
]


class RagFactory:
    def __init__(
        self,
        *,
        builder: RagBuilder,
        registry: KnowledgeBaseRegistry | None = None,
        kb_separator: str = "__",
    ) -> None:
        self._builder = builder
        self._registry = registry
        self._kb_separator = kb_separator or "__"
        self._instances: dict[RagKey, Any] = {}
        self._locks: dict[RagKey, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @property
    def kb_separator(self) -> str:
        return self._kb_separator

    def compose_workspace(self, workspace_id: str, kb_id: str) -> str:
        return f"{workspace_id}{self._kb_separator}{kb_id}"

    def prime(self, workspace_id: str, kb_id: str, rag: Any) -> None:
        self._instances[(workspace_id, kb_id)] = rag

    async def get(self, workspace_id: str, kb_id: str) -> Any:
        key = (workspace_id, kb_id)
        existing = self._instances.get(key)
        if existing is not None:
            return existing

        lock = await self._get_lock(key)
        async with lock:
            existing = self._instances.get(key)
            if existing is not None:
                return existing

            knowledge_base = (
                self._registry.get_kb(workspace_id, kb_id) if self._registry else None
            )
            if self._registry is not None and knowledge_base is None:
                raise KeyError(
                    f"Knowledge base '{kb_id}' does not exist in workspace '{workspace_id}'."
                )

            combined_workspace = self.compose_workspace(workspace_id, kb_id)
            built = self._builder(
                workspace_id,
                kb_id,
                combined_workspace,
                knowledge_base,
            )
            rag = await built if inspect.isawaitable(built) else built
            self._instances[key] = rag
            logger.info(
                "Initialized isolated LightRAG runtime for workspace='%s' kb='%s' (storage workspace='%s').",
                workspace_id,
                kb_id,
                combined_workspace,
            )
            return rag

    async def evict(self, workspace_id: str, kb_id: str) -> bool:
        key = (workspace_id, kb_id)
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
        logger.info(
            "Evicted isolated LightRAG runtime for workspace='%s' kb='%s'.",
            workspace_id,
            kb_id,
        )
        return True

    async def finalize_all(self) -> None:
        cached_items = list(self._instances.items())
        self._instances.clear()

        failures: list[str] = []
        for (workspace_id, kb_id), rag in cached_items:
            finalize = getattr(rag, "finalize_storages", None)
            if finalize is None:
                continue

            try:
                result = finalize()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover - exercised via logging path
                failures.append(f"{workspace_id}/{kb_id}")
                logger.error(
                    "Failed to finalize LightRAG runtime for workspace='%s' kb='%s': %s",
                    workspace_id,
                    kb_id,
                    exc,
                )

        self._locks.clear()

        if failures:
            logger.error(
                "LightRAG runtime finalization completed with failures for: %s",
                ", ".join(failures),
            )

    async def _get_lock(self, key: RagKey) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock
