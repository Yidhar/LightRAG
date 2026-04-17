"""
Knowledge-base metadata model.

KBs are now global resources — they live in a top-level ``kbs`` map in
``kb_registry.json`` and are referenced by workspaces through a
``workspace_kb_links`` junction. ``workspace_id`` on this record is
retained for backwards-compat reads and still populated with the
*owner* workspace (the workspace that originally created the KB); new
code should prefer the explicit ``owner_workspace_id`` field.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class KnowledgeBase:
    id: str
    # Owner workspace — the workspace the KB was first created in.
    # Exposed under both ``workspace_id`` (legacy name, preserved for
    # existing JSON on disk) and ``owner_workspace_id`` (the
    # semantically-correct name going forward). ``from_dict`` / the
    # registry's migration code keep them in sync.
    workspace_id: str
    name: str = "default"
    description: str = ""
    created_at: str = field(default_factory=_utcnow_iso)
    config_override: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    # Free-form category tag chosen by the KB's owner workspace. Empty
    # string means "uncategorised" and is treated as a distinct bucket
    # by the UI grouping logic.
    category: str = ""
    # Explicit alias for ``workspace_id`` — the KB was created here,
    # but may be linked into additional workspaces via the registry's
    # link table. None on freshly-constructed KBs falls back to
    # ``workspace_id`` in ``__post_init__``.
    owner_workspace_id: str | None = None

    def __post_init__(self) -> None:
        self.id = str(self.id or "").strip()
        self.workspace_id = str(self.workspace_id or "").strip()
        self.name = str(self.name or "").strip() or self.id
        self.description = str(self.description or "").strip()
        self.status = str(self.status or "").strip() or "active"
        self.category = str(self.category or "").strip()

        if isinstance(self.created_at, datetime):
            created_at = self.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            self.created_at = created_at.astimezone(timezone.utc).isoformat()
        else:
            self.created_at = str(self.created_at or _utcnow_iso()).strip()

        if not isinstance(self.config_override, dict):
            raise TypeError("config_override must be a dictionary.")
        self.config_override = dict(self.config_override)

        if not self.id:
            raise ValueError("Knowledge base id must not be empty.")
        if not self.workspace_id:
            raise ValueError("Knowledge base workspace_id must not be empty.")

        # Keep owner/legacy fields in lockstep. If only one is provided,
        # mirror it onto the other so callers reading either see the
        # same value.
        if not self.owner_workspace_id:
            self.owner_workspace_id = self.workspace_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeBase":
        owner = data.get("owner_workspace_id") or data.get("workspace_id") or ""
        return cls(
            id=data.get("id", ""),
            workspace_id=data.get("workspace_id", owner),
            name=data.get("name", "default"),
            description=data.get("description", ""),
            created_at=data.get("created_at", _utcnow_iso()),
            config_override=data.get("config_override", {}) or {},
            status=data.get("status", "active"),
            category=data.get("category", ""),
            owner_workspace_id=owner,
        )
