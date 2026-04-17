"""
Knowledge-base metadata models for the staged WS4 isolation rollout.
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
    workspace_id: str
    name: str = "default"
    description: str = ""
    created_at: str = field(default_factory=_utcnow_iso)
    config_override: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    # Free-form category tag chosen by the workspace owner. Empty string
    # means "uncategorised" and is treated as a distinct bucket by the
    # UI grouping logic. No separate categories table — each KB carries
    # its own tag so users can rename / regroup without a CRUD dance.
    category: str = ""

    def __post_init__(self) -> None:
        self.id = str(self.id or "").strip()
        self.workspace_id = str(self.workspace_id or "").strip()
        self.name = str(self.name or "").strip() or self.id
        self.description = str(self.description or "").strip()
        self.status = str(self.status or "").strip() or "active"
        # Normalise category early — strip surrounding whitespace but keep
        # the original spelling / case so "Research" and "research" remain
        # distinct if the user really typed them that way.
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeBase":
        return cls(
            id=data.get("id", ""),
            workspace_id=data.get("workspace_id", ""),
            name=data.get("name", "default"),
            description=data.get("description", ""),
            created_at=data.get("created_at", _utcnow_iso()),
            config_override=data.get("config_override", {}) or {},
            status=data.get("status", "active"),
            category=data.get("category", ""),
        )
