"""
Membership and refresh-token model definitions for the staged RBAC rollout.
"""

from __future__ import annotations

from dataclasses import dataclass

from lightrag.api.db import get_declarative_base, has_sqlalchemy_support

WORKSPACE_MEMBER_TABLE_NAME = "workspace_memberships"
KB_MEMBER_TABLE_NAME = "kb_memberships"
REFRESH_TOKEN_TABLE_NAME = "refresh_tokens"

Base = get_declarative_base()

if has_sqlalchemy_support():
    from sqlalchemy import String
    from sqlalchemy.orm import Mapped, mapped_column

    class WorkspaceMemberRow(Base):
        __tablename__ = WORKSPACE_MEMBER_TABLE_NAME

        id: Mapped[str] = mapped_column(String(64), primary_key=True)
        user_id: Mapped[str] = mapped_column(String(64), nullable=False)
        workspace_id: Mapped[str] = mapped_column(String(255), nullable=False)
        role: Mapped[str] = mapped_column(String(32), nullable=False)
        source: Mapped[str] = mapped_column(String(32), nullable=False, default="seed")
        created_at: Mapped[str] = mapped_column(String(64), nullable=False)
        updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

    class KBMemberRow(Base):
        __tablename__ = KB_MEMBER_TABLE_NAME

        id: Mapped[str] = mapped_column(String(64), primary_key=True)
        user_id: Mapped[str] = mapped_column(String(64), nullable=False)
        workspace_id: Mapped[str] = mapped_column(String(255), nullable=False)
        kb_id: Mapped[str] = mapped_column(String(255), nullable=False)
        role: Mapped[str] = mapped_column(String(32), nullable=False)
        source: Mapped[str] = mapped_column(String(32), nullable=False, default="seed")
        created_at: Mapped[str] = mapped_column(String(64), nullable=False)
        updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

    class RefreshTokenRow(Base):
        __tablename__ = REFRESH_TOKEN_TABLE_NAME

        id: Mapped[str] = mapped_column(String(64), primary_key=True)
        user_id: Mapped[str] = mapped_column(String(64), nullable=False)
        token_jti: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
        token_hash: Mapped[str] = mapped_column(String(1024), nullable=False)
        issued_at: Mapped[str] = mapped_column(String(64), nullable=False)
        expires_at: Mapped[str] = mapped_column(String(64), nullable=False)
        revoked_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
        rotated_from_token_id: Mapped[str | None] = mapped_column(
            String(64), nullable=True
        )
        created_at: Mapped[str] = mapped_column(String(64), nullable=False)

else:

    @dataclass(slots=True)
    class WorkspaceMemberRow:
        id: str
        user_id: str
        workspace_id: str
        role: str
        source: str = "seed"
        created_at: str = ""
        updated_at: str = ""

    @dataclass(slots=True)
    class KBMemberRow:
        id: str
        user_id: str
        workspace_id: str
        kb_id: str
        role: str
        source: str = "seed"
        created_at: str = ""
        updated_at: str = ""

    @dataclass(slots=True)
    class RefreshTokenRow:
        id: str
        user_id: str
        token_jti: str
        token_hash: str
        issued_at: str
        expires_at: str
        revoked_at: str | None = None
        rotated_from_token_id: str | None = None
        created_at: str = ""
