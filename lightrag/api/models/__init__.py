"""
Placeholder ORM metadata for the Platform V2 auth/isolation bootstrap.
"""

from __future__ import annotations

from lightrag.api.db import (
    PLATFORM_BOOTSTRAP_TABLE_NAME,
    get_declarative_base,
    has_sqlalchemy_support,
)
from lightrag.api.models.membership import (
    KB_MEMBER_TABLE_NAME,
    REFRESH_TOKEN_TABLE_NAME,
    WORKSPACE_MEMBER_TABLE_NAME,
    KBMemberRow,
    RefreshTokenRow,
    WorkspaceMemberRow,
)
from lightrag.api.models.kb import KnowledgeBase
from lightrag.api.models.user import USER_TABLE_NAME, UserRow

Base = get_declarative_base()

if has_sqlalchemy_support():
    from sqlalchemy import String
    from sqlalchemy.orm import Mapped, mapped_column

    class PlatformBootstrapState(Base):
        """Small bootstrap marker table so init_db() performs real DDL."""

        __tablename__ = PLATFORM_BOOTSTRAP_TABLE_NAME

        state_key: Mapped[str] = mapped_column(String(64), primary_key=True)
        state_value: Mapped[str | None] = mapped_column(String(255), nullable=True)

else:

    class PlatformBootstrapState:
        """Fallback placeholder when SQLAlchemy is not installed."""

        __tablename__ = PLATFORM_BOOTSTRAP_TABLE_NAME


__all__ = [
    "Base",
    "PlatformBootstrapState",
    "PLATFORM_BOOTSTRAP_TABLE_NAME",
    "USER_TABLE_NAME",
    "WORKSPACE_MEMBER_TABLE_NAME",
    "KB_MEMBER_TABLE_NAME",
    "REFRESH_TOKEN_TABLE_NAME",
    "UserRow",
    "WorkspaceMemberRow",
    "KBMemberRow",
    "RefreshTokenRow",
    "KnowledgeBase",
]
