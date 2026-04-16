"""
JSON-backed knowledge-base metadata registry for WS4 runtime isolation.
"""

from __future__ import annotations

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


class KnowledgeBaseRegistry:
    """Persist knowledge-base metadata per workspace inside the working directory."""

    REGISTRY_FILENAME = "kb_registry.json"
    REGISTRY_VERSION = 1

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

    def load_or_create(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_registry_file()
            return self._read_state_unlocked()

    def list_kbs(self, workspace_id: str) -> list[KnowledgeBase]:
        workspace_key = _sanitize_identifier(workspace_id, label="workspace_id")
        with self._lock:
            state = self._read_state_unlocked()
            raw_workspace = state.get("workspaces", {}).get(workspace_key, {})
            items = raw_workspace.get("kbs", {}).values()
            kb_items = [KnowledgeBase.from_dict(item) for item in items]
        return sorted(kb_items, key=lambda item: (item.created_at, item.id))

    def get_kb(self, workspace_id: str, kb_id: str) -> KnowledgeBase | None:
        workspace_key = _sanitize_identifier(workspace_id, label="workspace_id")
        kb_key = _sanitize_identifier(kb_id, label="kb_id")
        with self._lock:
            state = self._read_state_unlocked()
            payload = (
                state.get("workspaces", {})
                .get(workspace_key, {})
                .get("kbs", {})
                .get(kb_key)
            )
        if payload is None:
            return None
        return KnowledgeBase.from_dict(payload)

    def exists(self, workspace_id: str, kb_id: str) -> bool:
        return self.get_kb(workspace_id, kb_id) is not None

    def create_kb(
        self,
        workspace_id: str,
        *,
        kb_id: str | None = None,
        name: str | None = None,
        description: str = "",
        config_override: dict[str, Any] | None = None,
        status: str = "active",
    ) -> KnowledgeBase:
        workspace_key = _sanitize_identifier(workspace_id, label="workspace_id")
        resolved_kb_id = _sanitize_identifier(
            kb_id or f"kb_{uuid4().hex[:8]}",
            label="kb_id",
        )

        kb_record = KnowledgeBase(
            id=resolved_kb_id,
            workspace_id=workspace_key,
            name=name or resolved_kb_id,
            description=description,
            config_override=config_override or {},
            status=status,
        )

        with self._lock:
            state = self._read_state_unlocked()
            workspace_state = state.setdefault("workspaces", {}).setdefault(
                workspace_key,
                {"kbs": {}},
            )
            kb_state = workspace_state.setdefault("kbs", {})
            if resolved_kb_id in kb_state:
                raise ValueError(
                    f"Knowledge base '{resolved_kb_id}' already exists in workspace '{workspace_key}'."
                )

            kb_state[resolved_kb_id] = kb_record.to_dict()
            self._write_state_unlocked(state)

        return kb_record

    def ensure_default_kb(
        self,
        workspace_id: str,
        kb_id: str = "default",
    ) -> KnowledgeBase:
        workspace_key = _sanitize_identifier(workspace_id, label="workspace_id")
        kb_key = _sanitize_identifier(kb_id, label="kb_id")
        with self._lock:
            existing = self.get_kb(workspace_key, kb_key)
            if existing is not None:
                return existing

            name = "Default" if kb_key == "default" else kb_key
            return self.create_kb(
                workspace_key,
                kb_id=kb_key,
                name=name,
                description="Auto-created default knowledge base.",
            )

    def delete_kb(self, workspace_id: str, kb_id: str) -> bool:
        workspace_key = _sanitize_identifier(workspace_id, label="workspace_id")
        kb_key = _sanitize_identifier(kb_id, label="kb_id")

        with self._lock:
            state = self._read_state_unlocked()
            workspaces = state.setdefault("workspaces", {})
            workspace_state = workspaces.get(workspace_key)
            if workspace_state is None:
                return False

            kb_state = workspace_state.setdefault("kbs", {})
            if kb_key not in kb_state:
                return False

            del kb_state[kb_key]
            if not kb_state:
                workspaces.pop(workspace_key, None)
            self._write_state_unlocked(state)
        return True

    def update_kb(
        self,
        workspace_id: str,
        kb_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        config_override: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> KnowledgeBase | None:
        workspace_key = _sanitize_identifier(workspace_id, label="workspace_id")
        kb_key = _sanitize_identifier(kb_id, label="kb_id")

        with self._lock:
            state = self._read_state_unlocked()
            payload = (
                state.get("workspaces", {})
                .get(workspace_key, {})
                .get("kbs", {})
                .get(kb_key)
            )
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

            state.setdefault("workspaces", {}).setdefault(
                workspace_key,
                {"kbs": {}},
            ).setdefault("kbs", {})[kb_key] = kb_record.to_dict()
            self._write_state_unlocked(state)
            return kb_record

    def _ensure_registry_file(self) -> None:
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_state_atomic(self._empty_state())

    def _empty_state(self) -> dict[str, Any]:
        return {"version": self.REGISTRY_VERSION, "workspaces": {}}

    def _read_state_unlocked(self) -> dict[str, Any]:
        self._ensure_registry_file()
        with self.path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict):
            raise ValueError("kb_registry.json must contain a JSON object.")
        state.setdefault("version", self.REGISTRY_VERSION)
        state.setdefault("workspaces", {})
        return state

    def _write_state_unlocked(self, state: dict[str, Any]) -> None:
        self._write_state_atomic(state)

    def _write_state_atomic(self, state: dict[str, Any]) -> None:
        serialized = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.write("\n")
        os.replace(tmp_path, self.path)
