"""
JSON-backed knowledge-base registry.

State shape (v2, current)
-------------------------

    {
      "version": 2,
      "kbs": {
        "<kb_id>": {
          "id": "<kb_id>",
          "name": "...",
          "description": "...",
          "owner_workspace_id": "...",    // bookkeeping — who created it
          "category": "", "status": "active",
          "config_override": {...},
          "created_at": "...", "updated_at": "..."
        }
      },
      "workspace_kb_links": {
        "<workspace_id>": ["kb_id_a", "kb_id_b", ...]
      }
    }

Each KB is a top-level object in the ``kbs`` map and lives in exactly
one storage namespace (keyed on ``kb_id`` only, not ``workspace__kb``).
Workspaces are *references*: many-to-many via ``workspace_kb_links``.

V1 → V2 migration
-----------------
Pre-v2 state nested KBs under ``workspaces[W].kbs[K]`` with one storage
namespace per ``W__K`` pair. ``_migrate_state_v1_to_v2`` runs on load
and rewrites the JSON in place. If two workspaces happened to own KBs
with the same ``kb_id`` (e.g. both auto-seeded "default"), the second
one is renamed ``<kb_id>__<workspace_short_hash>`` and we log a warning.

Rag storage directories (``rag_storage/<namespace>/``) are renamed
separately by the server's lifespan — see ``_migrate_storage_layout``
in ``lightrag.api.lightrag_server`` — since only the server knows
where the working dir is.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from lightrag.api.models.kb import KnowledgeBase
from lightrag.utils import logger

_IDENTIFIER_SANITIZER = re.compile(r"[^a-zA-Z0-9_]")

REGISTRY_VERSION = 2


def _sanitize_identifier(value: str | None, *, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty.")

    sanitized = _IDENTIFIER_SANITIZER.sub("_", normalized)
    if sanitized != normalized:
        logger.warning(
            "%s value '%s' contains invalid characters. Sanitized to '%s'.",
            label,
            normalized,
            sanitized,
        )
    return sanitized


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


class KnowledgeBaseRegistry:
    """Persist global KB metadata + workspace→KB link table.

    Every method is synchronous + RLock-guarded because this file is
    the single writer. Concurrency inside the pipeline is handled one
    level up (the RAG runtime cache keyed by kb_id).
    """

    REGISTRY_FILENAME = "kb_registry.json"

    def __init__(self, working_dir: str | Path, *, filename: str | Path | None = None):
        self.working_dir = Path(working_dir)
        self.path = (
            Path(filename)
            if filename is not None
            else self.working_dir / self.REGISTRY_FILENAME
        )
        self._lock = RLock()
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_registry_file()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def load_or_create(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_registry_file()
            return self._read_state_unlocked()

    def list_kbs(self, workspace_id: str) -> list[KnowledgeBase]:
        """KBs linked to ``workspace_id``, sorted by created_at."""
        workspace_key = _sanitize_identifier(workspace_id, label="workspace_id")
        with self._lock:
            state = self._read_state_unlocked()
            kb_ids = state.get("workspace_kb_links", {}).get(workspace_key, [])
            kbs = state.get("kbs", {})
            items = [KnowledgeBase.from_dict(kbs[kb_id]) for kb_id in kb_ids if kb_id in kbs]
        return sorted(items, key=lambda item: (item.created_at, item.id))

    def list_all_kbs(self) -> list[KnowledgeBase]:
        """Every KB registered anywhere — used by the "link existing" picker."""
        with self._lock:
            state = self._read_state_unlocked()
            items = [KnowledgeBase.from_dict(p) for p in state.get("kbs", {}).values()]
        return sorted(items, key=lambda item: (item.created_at, item.id))

    def get_kb(self, workspace_id: str | None, kb_id: str) -> KnowledgeBase | None:
        """Look up a KB record.

        ``workspace_id`` is optional and only used as an existence gate:
        when passed, the KB must be linked to that workspace for the
        lookup to succeed (mirroring the old "per-workspace scope"
        semantics so routes don't silently leak KBs across workspaces).
        Pass ``None`` to search the global pool (used by the "link
        existing" picker and by the rag_factory).
        """
        kb_key = _sanitize_identifier(kb_id, label="kb_id")
        with self._lock:
            state = self._read_state_unlocked()
            payload = state.get("kbs", {}).get(kb_key)
            if payload is None:
                return None
            if workspace_id is not None:
                workspace_key = _sanitize_identifier(
                    workspace_id, label="workspace_id"
                )
                linked = state.get("workspace_kb_links", {}).get(workspace_key, [])
                if kb_key not in linked:
                    return None
        return KnowledgeBase.from_dict(payload)

    def exists(self, workspace_id: str | None, kb_id: str) -> bool:
        return self.get_kb(workspace_id, kb_id) is not None

    def list_categories(self, workspace_id: str) -> list[str]:
        """Distinct category tags among KBs linked to this workspace."""
        seen: set[str] = set()
        for kb in self.list_kbs(workspace_id):
            tag = (kb.category or "").strip()
            if tag:
                seen.add(tag)
        return sorted(seen, key=lambda v: v.lower())

    # ------------------------------------------------------------------
    # KB CRUD (global)
    # ------------------------------------------------------------------

    def create_kb(
        self,
        workspace_id: str,
        *,
        kb_id: str | None = None,
        name: str | None = None,
        description: str = "",
        config_override: dict[str, Any] | None = None,
        status: str = "active",
        category: str = "",
    ) -> KnowledgeBase:
        """Create a new KB globally + link it to ``workspace_id``.

        ``workspace_id`` acts as the owner workspace — stored on the KB
        for bookkeeping, and the newly-created KB is auto-linked here.
        The caller can link it into additional workspaces afterwards
        via :meth:`link_kb_to_workspace`.
        """
        workspace_key = _sanitize_identifier(workspace_id, label="workspace_id")
        resolved_kb_id = _sanitize_identifier(
            kb_id or f"kb_{uuid4().hex[:8]}",
            label="kb_id",
        )

        kb_record = KnowledgeBase(
            id=resolved_kb_id,
            workspace_id=workspace_key,  # legacy field kept for serialization compat
            name=name or resolved_kb_id,
            description=description,
            config_override=config_override or {},
            status=status,
            category=category,
        )

        with self._lock:
            state = self._read_state_unlocked()
            kbs = state.setdefault("kbs", {})
            if resolved_kb_id in kbs:
                raise ValueError(
                    f"Knowledge base '{resolved_kb_id}' already exists."
                )
            kbs[resolved_kb_id] = kb_record.to_dict()
            links = state.setdefault("workspace_kb_links", {}).setdefault(
                workspace_key, []
            )
            if resolved_kb_id not in links:
                links.append(resolved_kb_id)
            self._write_state_unlocked(state)

        return kb_record

    def ensure_default_kb(
        self,
        workspace_id: str,
        kb_id: str = "default",
    ) -> KnowledgeBase:
        """Idempotently ensure a KB is linked to ``workspace_id``.

        If the KB already exists globally it is linked (no-op if the link
        already exists). Otherwise a new KB is created + linked.
        """
        workspace_key = _sanitize_identifier(workspace_id, label="workspace_id")
        kb_key = _sanitize_identifier(kb_id, label="kb_id")
        with self._lock:
            existing_global = self.get_kb(None, kb_key)
            if existing_global is not None:
                self.link_kb_to_workspace(workspace_key, kb_key)
                return existing_global

            name = "Default" if kb_key == "default" else kb_key
            return self.create_kb(
                workspace_key,
                kb_id=kb_key,
                name=name,
                description="Auto-created default knowledge base.",
            )

    def delete_kb(self, kb_id: str) -> bool:
        """Delete a KB globally + unlink from every workspace.

        Returns ``False`` if the KB id does not exist. The storage
        namespace (rag_storage/<kb_id>/) is NOT deleted here — that's
        the caller's job via ``rag_factory.evict`` or explicit cleanup.
        """
        kb_key = _sanitize_identifier(kb_id, label="kb_id")
        with self._lock:
            state = self._read_state_unlocked()
            kbs = state.setdefault("kbs", {})
            if kb_key not in kbs:
                return False
            del kbs[kb_key]
            for links in state.setdefault("workspace_kb_links", {}).values():
                if kb_key in links:
                    links.remove(kb_key)
            self._write_state_unlocked(state)
        return True

    def update_kb(
        self,
        kb_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        config_override: dict[str, Any] | None = None,
        status: str | None = None,
        category: str | None = None,
    ) -> KnowledgeBase | None:
        """Update the global KB record. No workspace context needed —
        KB metadata is global, not per-link."""
        kb_key = _sanitize_identifier(kb_id, label="kb_id")

        with self._lock:
            state = self._read_state_unlocked()
            payload = state.setdefault("kbs", {}).get(kb_key)
            if payload is None:
                return None

            kb_record = KnowledgeBase.from_dict(payload)
            if name is not None:
                normalized_name = str(name).strip()
                if not normalized_name:
                    raise ValueError("Knowledge base name must not be empty.")
                kb_record.name = normalized_name
            if description is not None:
                kb_record.description = str(description).strip()
            if config_override is not None:
                if not isinstance(config_override, dict):
                    raise TypeError("config_override must be a dictionary.")
                kb_record.config_override = dict(config_override)
            if status is not None:
                normalized_status = str(status).strip()
                if not normalized_status:
                    raise ValueError("Knowledge base status must not be empty.")
                kb_record.status = normalized_status
            if category is not None:
                kb_record.category = str(category).strip()

            state["kbs"][kb_key] = kb_record.to_dict()
            self._write_state_unlocked(state)
            return kb_record

    # ------------------------------------------------------------------
    # Link table CRUD
    # ------------------------------------------------------------------

    def link_kb_to_workspace(self, workspace_id: str, kb_id: str) -> bool:
        """Add ``kb_id`` to ``workspace_id``'s link list.

        Idempotent — returns True when a new link row was added, False
        when it was already present. Raises when the KB does not
        exist globally (no phantom links).
        """
        workspace_key = _sanitize_identifier(workspace_id, label="workspace_id")
        kb_key = _sanitize_identifier(kb_id, label="kb_id")
        with self._lock:
            state = self._read_state_unlocked()
            if kb_key not in state.get("kbs", {}):
                raise ValueError(f"Knowledge base '{kb_key}' does not exist.")
            links = state.setdefault("workspace_kb_links", {}).setdefault(
                workspace_key, []
            )
            if kb_key in links:
                return False
            links.append(kb_key)
            self._write_state_unlocked(state)
        return True

    def unlink_kb_from_workspace(self, workspace_id: str, kb_id: str) -> bool:
        """Remove ``kb_id`` from ``workspace_id``'s link list.

        Returns True when a link existed and was removed, False when no
        such link was present. The KB itself remains in the global pool.
        """
        workspace_key = _sanitize_identifier(workspace_id, label="workspace_id")
        kb_key = _sanitize_identifier(kb_id, label="kb_id")
        with self._lock:
            state = self._read_state_unlocked()
            links = state.setdefault("workspace_kb_links", {}).get(workspace_key)
            if not links or kb_key not in links:
                return False
            links.remove(kb_key)
            self._write_state_unlocked(state)
        return True

    def list_workspaces_linking_kb(self, kb_id: str) -> list[str]:
        """Which workspaces reference ``kb_id``. Used to decide whether
        an unlink should also trigger a global delete."""
        kb_key = _sanitize_identifier(kb_id, label="kb_id")
        with self._lock:
            state = self._read_state_unlocked()
            return [
                ws
                for ws, links in state.get("workspace_kb_links", {}).items()
                if kb_key in links
            ]

    # ------------------------------------------------------------------
    # Persistence + migration
    # ------------------------------------------------------------------

    def _ensure_registry_file(self) -> None:
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_state_atomic(self._empty_state())

    def _empty_state(self) -> dict[str, Any]:
        return {
            "version": REGISTRY_VERSION,
            "kbs": {},
            "workspace_kb_links": {},
        }

    def _read_state_unlocked(self) -> dict[str, Any]:
        self._ensure_registry_file()
        with self.path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict):
            raise ValueError("kb_registry.json must contain a JSON object.")

        version = state.get("version", 1)
        if version < REGISTRY_VERSION:
            state = _migrate_state_v1_to_v2(state)
            # Persist the migrated shape so subsequent reads never hit
            # the migrator again.
            self._write_state_atomic(state)

        state.setdefault("version", REGISTRY_VERSION)
        state.setdefault("kbs", {})
        state.setdefault("workspace_kb_links", {})
        return state

    def _write_state_unlocked(self, state: dict[str, Any]) -> None:
        state["version"] = REGISTRY_VERSION
        self._write_state_atomic(state)

    def _write_state_atomic(self, state: dict[str, Any]) -> None:
        serialized = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.write("\n")
        os.replace(tmp_path, self.path)


def _migrate_state_v1_to_v2(state: dict[str, Any]) -> dict[str, Any]:
    """Convert the old ``workspaces[W].kbs[K]`` shape into the new
    global ``kbs`` + ``workspace_kb_links`` shape.

    Collisions: when two workspaces owned KBs with the same id, the
    second one is renamed ``<kb_id>__<workspace_short_hash>`` so the
    global ``kbs`` dict stays flat. Logs a warning — the operator can
    rename manually afterwards if the auto-suffix is ugly.
    """
    old_workspaces = state.get("workspaces", {}) or {}
    new_kbs: dict[str, Any] = {}
    new_links: dict[str, list[str]] = {}
    id_renames: list[tuple[str, str, str]] = []  # (workspace, old_id, new_id)

    for workspace_id, workspace_state in old_workspaces.items():
        links_for_ws: list[str] = []
        for kb_id, payload in (workspace_state.get("kbs") or {}).items():
            resolved_id = kb_id
            if resolved_id in new_kbs:
                # Collision — preserve this one under a suffixed id.
                suffix = _short_hash(workspace_id)
                resolved_id = f"{kb_id}__{suffix}"
                id_renames.append((workspace_id, kb_id, resolved_id))
                logger.warning(
                    "Migrating kb_registry: collision on kb_id '%s' between "
                    "workspaces — renamed the copy from workspace '%s' to '%s'.",
                    kb_id,
                    workspace_id,
                    resolved_id,
                )

            record = dict(payload)
            record["id"] = resolved_id
            # The old record's ``workspace_id`` field is preserved for
            # the legacy serialization path; we also stamp a new
            # ``owner_workspace_id`` so downstream code can tell where
            # a shared KB originally came from.
            record.setdefault("owner_workspace_id", workspace_id)
            new_kbs[resolved_id] = record
            links_for_ws.append(resolved_id)

        new_links[workspace_id] = links_for_ws

    migrated = {
        "version": REGISTRY_VERSION,
        "kbs": new_kbs,
        "workspace_kb_links": new_links,
    }
    logger.info(
        "Migrated kb_registry from v1 to v2: %s KBs, %s workspace links, %s renames.",
        len(new_kbs),
        sum(len(v) for v in new_links.values()),
        len(id_renames),
    )
    return migrated
