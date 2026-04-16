"""
User model definitions for the staged DB-backed auth rollout.
"""

from __future__ import annotations

from dataclasses import dataclass

from lightrag.api.db import get_declarative_base, has_sqlalchemy_support

USER_TABLE_NAME = "users"
Base = get_declarative_base()

if has_sqlalchemy_support():
    from sqlalchemy import Boolean, String
    from sqlalchemy.orm import Mapped, mapped_column

    class UserRow(Base):
        __tablename__ = USER_TABLE_NAME

        id: Mapped[str] = mapped_column(String(64), primary_key=True)
        username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
        password_secret: Mapped[str] = mapped_column(String(1024), nullable=False)
        source: Mapped[str] = mapped_column(String(32), nullable=False, default="env")
        is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
        created_at: Mapped[str] = mapped_column(String(64), nullable=False)
        updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

else:

    @dataclass(slots=True)
    class UserRow:
        id: str
        username: str
        password_secret: str
        source: str = "env"
        is_active: bool = True
        created_at: str = ""
        updated_at: str = ""
