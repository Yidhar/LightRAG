"""
Authentication provider abstractions for local accounts today and remote identity later.

The goal of this module is to keep the public API surface stable while allowing
future providers (LDAP, OAuth/OIDC, remote identity gateways, SSO bridges) to
replace only the credential-verification and user-directory implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status

from lightrag.api.auth import auth_handler
from lightrag.api.identity_store import (
    IdentityUserRecord,
    create_user,
    get_membership_claims,
    get_user_auth_by_username,
    get_user_by_id,
    list_users,
    revoke_refresh_tokens_for_user,
    set_user_password,
    update_user,
)
from lightrag.api.passwords import verify_password


@dataclass(slots=True)
class AuthenticatedPrincipal:
    """Provider-agnostic authenticated identity returned after password login."""

    username: str
    role: str
    user_id: str | None = None
    memberships: list[dict[str, str | None]] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class AuthStatusResult:
    """Capabilities exposed to the login UI and future remote-auth clients."""

    auth_configured: bool
    auth_mode: str
    available_providers: list[str] = field(default_factory=list)
    supports_password_login: bool = True
    supports_refresh_tokens: bool = False
    supports_user_management: bool = False
    supports_self_registration: bool = False
    self_registration_role: str | None = None
    message: str | None = None

    def to_dict(self) -> dict:
        return {
            "auth_configured": self.auth_configured,
            "auth_mode": self.auth_mode,
            "available_providers": self.available_providers,
            "supports_password_login": self.supports_password_login,
            "supports_refresh_tokens": self.supports_refresh_tokens,
            "supports_user_management": self.supports_user_management,
            "supports_self_registration": self.supports_self_registration,
            "self_registration_role": self.self_registration_role,
            "message": self.message,
        }


class AuthProvider(ABC):
    """
    Stable extension contract for the LightRAG authentication surface.

    Future remote providers should keep these methods and response shapes stable
    so `/auth-status`, `/login`, `/auth/me`, `/auth/logout`, and user-management
    endpoints can continue to work without frontend contract churn.
    """

    provider_name = "base"
    supports_password_login = True
    supports_user_management = False

    @abstractmethod
    async def get_auth_status(self, request: Request) -> AuthStatusResult:
        """Return login-mode and capability metadata for the active provider."""

    @abstractmethod
    async def authenticate_password(
        self, request: Request, username: str, password: str
    ) -> AuthenticatedPrincipal | None:
        """Return a normalized principal or ``None`` for invalid credentials."""

    async def get_current_user_profile(self, token_info: dict) -> dict:
        """
        Resolve the signed-in user profile.

        Membership claims are read **fresh from the DB** when a DB-backed
        user is identified — *not* from the JWT's ``memberships`` claim,
        which is frozen at login. A user who creates a new workspace
        after logging in needs ``/auth/me`` to show that new membership
        so the frontend can unlock UI gating immediately (we hit this
        bug with ``工作区2`` where ``canManage`` was false because the
        JWT predated the new workspace).

        Remote providers can override this to surface upstream directory
        attributes while keeping the `/auth/me` response stable.
        """
        user_id = token_info.get("user_id")
        # Snapshot-from-JWT fallback used when the DB is unavailable or
        # the user is env-seeded (no DB row).
        token_memberships = list(token_info.get("memberships") or [])

        if user_id:
            user = await get_user_by_id(user_id)
            if user is not None:
                try:
                    fresh_memberships = await get_membership_claims(user_id)
                except Exception:
                    # DB hiccup — fall back to the JWT snapshot. Still
                    # correct for the "existed at login" set, just
                    # missing anything granted since then.
                    fresh_memberships = token_memberships
                return {
                    "user_id": user.user_id,
                    "username": user.username,
                    "source": user.source,
                    "is_active": user.is_active,
                    "role": token_info.get("role", "viewer"),
                    "memberships": fresh_memberships,
                    "provider": self.provider_name,
                }

        return {
            "user_id": None,
            "username": token_info.get("username"),
            "source": "env",
            "is_active": True,
            "role": token_info.get("role", "viewer"),
            "memberships": token_memberships,
            "provider": self.provider_name,
        }

    async def list_directory_users(self, request: Request) -> list[IdentityUserRecord]:
        """List local users for account-admin flows."""
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The active authentication provider does not expose a local user directory.",
        )

    async def create_directory_user(
        self, request: Request, *, username: str, password: str, is_active: bool = True
    ) -> IdentityUserRecord:
        """Create a local user. Remote providers should override or keep 501."""
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The active authentication provider does not support local user creation.",
        )

    async def update_directory_user(
        self,
        request: Request,
        *,
        user_id: str,
        username: str | None = None,
        is_active: bool | None = None,
    ) -> IdentityUserRecord:
        """Update local user metadata. Remote providers should override or keep 501."""
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The active authentication provider does not support local user updates.",
        )

    async def rotate_directory_password(
        self, request: Request, *, user_id: str, password: str
    ) -> IdentityUserRecord:
        """Rotate a local password secret. Remote providers should override or keep 501."""
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The active authentication provider does not support local password rotation.",
        )

    async def change_password(
        self,
        request: Request,
        *,
        token_info: dict,
        current_password: str,
        new_password: str,
    ) -> dict:
        """User-facing password change flow. Providers can override as needed."""
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The active authentication provider does not support password changes.",
        )

    async def logout(self, request: Request, *, token_info: dict) -> dict:
        """Logout hook for provider-managed refresh/session state."""
        return {"status": "success"}


class LocalAuthProvider(AuthProvider):
    """Default LightRAG auth provider backed by env-seeded or DB-backed local users."""

    provider_name = "local"

    def _db_directory_enabled(self, request: Request) -> bool:
        return bool(
            getattr(request.app.state, "use_db_auth", False)
            and getattr(request.app.state, "db_ready", False)
        )

    async def get_auth_status(self, request: Request) -> AuthStatusResult:
        directory_enabled = self._db_directory_enabled(request)
        configured_user_count = (
            len(await list_users()) if directory_enabled else len(auth_handler.accounts)
        )
        auth_configured = configured_user_count > 0

        allow_self_register = bool(
            getattr(request.app.state, "allow_self_registration", False)
        )
        # Self-registration is only meaningful when the DB user directory
        # is live (env-seeded accounts have no writeable user store).
        supports_self_registration = directory_enabled and allow_self_register
        self_registration_role = (
            getattr(request.app.state, "default_registration_role", "viewer")
            if supports_self_registration
            else None
        )

        return AuthStatusResult(
            auth_configured=auth_configured,
            auth_mode="local" if auth_configured else "setup_required",
            available_providers=["local_password"] if auth_configured else [],
            supports_password_login=True,
            supports_refresh_tokens=directory_enabled,
            supports_user_management=directory_enabled,
            supports_self_registration=supports_self_registration,
            self_registration_role=self_registration_role,
            message=None
            if auth_configured
            else "No local accounts are configured yet. Seed an administrator account before sign-in.",
        )

    async def authenticate_password(
        self, request: Request, username: str, password: str
    ) -> AuthenticatedPrincipal | None:
        if self._db_directory_enabled(request):
            user = await get_user_auth_by_username(username)
            if user is None or not verify_password(password, user.password_secret):
                return None
            if not user.is_active:
                raise HTTPException(status_code=403, detail="User account is inactive")

            memberships = await get_membership_claims(user.user_id)
            default_role = "user" if user.source == "env" else "viewer"
            return AuthenticatedPrincipal(
                username=user.username,
                user_id=user.user_id,
                role=default_role,
                memberships=memberships,
                metadata={
                    "auth_mode": "enabled",
                    "auth_provider": self.provider_name,
                    "account_source": user.source,
                },
            )

        if auth_handler.verify_password(username, password):
            return AuthenticatedPrincipal(
                username=username,
                role="user",
                metadata={
                    "auth_mode": "enabled",
                    "auth_provider": self.provider_name,
                    "account_source": "env",
                },
            )

        return None

    async def list_directory_users(self, request: Request) -> list[IdentityUserRecord]:
        if not self._db_directory_enabled(request):
            return await super().list_directory_users(request)
        return await list_users()

    async def create_directory_user(
        self, request: Request, *, username: str, password: str, is_active: bool = True
    ) -> IdentityUserRecord:
        if not self._db_directory_enabled(request):
            return await super().create_directory_user(
                request, username=username, password=password, is_active=is_active
            )
        return await create_user(username.strip(), password, is_active=is_active)

    async def update_directory_user(
        self,
        request: Request,
        *,
        user_id: str,
        username: str | None = None,
        is_active: bool | None = None,
    ) -> IdentityUserRecord:
        if not self._db_directory_enabled(request):
            return await super().update_directory_user(
                request, user_id=user_id, username=username, is_active=is_active
            )
        return await update_user(
            user_id,
            username=username.strip() if username else None,
            is_active=is_active,
        )

    async def rotate_directory_password(
        self, request: Request, *, user_id: str, password: str
    ) -> IdentityUserRecord:
        if not self._db_directory_enabled(request):
            return await super().rotate_directory_password(
                request, user_id=user_id, password=password
            )
        return await set_user_password(user_id, password)

    async def change_password(
        self,
        request: Request,
        *,
        token_info: dict,
        current_password: str,
        new_password: str,
    ) -> dict:
        if not self._db_directory_enabled(request):
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="Password change requires DB-backed local auth.",
            )

        user_id = token_info.get("user_id")
        username = token_info.get("username")
        if not user_id or not username:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The current session is missing a DB-backed user identity.",
            )

        user = await get_user_auth_by_username(username)
        if user is None or user.user_id != user_id:
            raise HTTPException(status_code=404, detail="User account was not found")
        if not verify_password(current_password, user.password_secret):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect",
            )

        await set_user_password(user_id, new_password)
        await revoke_refresh_tokens_for_user(user_id)
        return {
            "status": "success",
            "message": "Password updated successfully. Please sign in again on this device.",
        }

    async def logout(self, request: Request, *, token_info: dict) -> dict:
        user_id = token_info.get("user_id")
        if user_id and self._db_directory_enabled(request):
            await revoke_refresh_tokens_for_user(user_id)
        return {"status": "success"}


def get_auth_provider(request: Request) -> AuthProvider:
    """
    Resolve the active auth provider for the request.

    Future remote-provider integration should swap this resolver to return
    LDAP/OIDC/etc. while keeping the route layer unchanged.
    """

    provider = getattr(request.app.state, "auth_provider", None)
    if provider is not None:
        return provider
    return LocalAuthProvider()
