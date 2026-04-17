"""
Workspace entity definitions.

Until Phase W1 landed, workspaces only existed as strings referenced by
memberships and by the storage namespacing layer. This module introduces
the persisted ``workspaces`` table so the UI can list, create, rename,
and delete workspaces without guessing from memberships.
"""

from __future__ import annotations

from dataclasses import dataclass

from lightrag.api.db import get_declarative_base, has_sqlalchemy_support

WORKSPACE_TABLE_NAME = "workspaces"

Base = get_declarative_base()

if has_sqlalchemy_support():
    from sqlalchemy import String
    from sqlalchemy.orm import Mapped, mapped_column

    class WorkspaceRow(Base):
        __tablename__ = WORKSPACE_TABLE_NAME

        id: Mapped[str] = mapped_column(String(255), primary_key=True)
        name: Mapped[str] = mapped_column(String(255), nullable=False)
        description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
        owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
        created_at: Mapped[str] = mapped_column(String(64), nullable=False)
        updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

else:

    @dataclass(slots=True)
    class WorkspaceRow:
        id: str
        name: str
        description: str | None = None
        owner_user_id: str | None = None
        created_at: str = ""
        updated_at: str = ""
