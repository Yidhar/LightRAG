"""
LDAP / Active Directory auth provider.

A concrete example of swapping LightRAG's local password store for a
remote identity source while keeping every route contract, JWT shape,
and audit emit site unchanged. The same pattern works for OIDC / SAML
/ custom IdP gateways — subclass ``AuthProvider`` and return
``AuthenticatedPrincipal`` from ``authenticate_password`` (or a
redirect-flow analogue).

Design
------
1. **Credentials verification**: service-account bind → search user DN
   → re-bind with the user-supplied password. A successful re-bind is
   the proof of password correctness; we never see the hash.
2. **Role resolution**: optional group lookup, with a configurable
   ``CN -> lightrag role`` mapping (``admin``/``editor``/``viewer``).
   When no groups match or group search is disabled, falls back to
   ``LDAP_DEFAULT_ROLE``.
3. **Workspace memberships**: LDAP has no notion of LightRAG
   workspaces. We shadow-create the user in the local identity_store
   on first login (source="ldap") so the rest of the stack —
   ``get_membership_claims``, the workspace/KB registry, audit, the
   no_access isolation sentinel — keep working unchanged. Each
   LDAP user gets a personal uuid4 workspace on first login, owned
   by that user.
4. **User management**: listing / creating / rotating passwords is
   intentionally not supported — LDAP is the source of truth. We
   return the ABC's ``501 Not Implemented`` defaults. Administrators
   manage accounts in the upstream directory instead.

Requirements
------------
- ``USE_DB_AUTH=true`` + DB is reachable. LDAP does credential
  verification; workspace memberships still live in the local DB.
- ``ldap3`` on the import path (``uv pip install ldap3``). We lazy
  import so deployments that don't enable LDAP don't pay for it.

Environment variables
---------------------
See ``docs/Configuration.md`` → "Remote auth providers" for the full
table. The short version:

    LIGHTRAG_AUTH_PROVIDER=ldap
    LDAP_SERVER_URL=ldaps://ldap.example.com:636
    LDAP_BIND_DN=cn=lightrag-svc,ou=service,dc=example,dc=com
    LDAP_BIND_PASSWORD=...
    LDAP_USER_SEARCH_BASE=ou=people,dc=example,dc=com
    LDAP_USER_FILTER=(&(objectClass=inetOrgPerson)(uid={username}))
    LDAP_USER_ATTR_USERNAME=uid
    # optional group→role mapping
    LDAP_GROUP_SEARCH_BASE=ou=groups,dc=example,dc=com
    LDAP_GROUP_FILTER=(&(objectClass=groupOfNames)(member={user_dn}))
    LDAP_GROUP_ATTR=cn
    LDAP_ROLE_MAPPING=lightrag-admins:admin,lightrag-editors:editor
    LDAP_DEFAULT_ROLE=viewer
    LDAP_USE_START_TLS=false
"""

from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass
from uuid import uuid4

from fastapi import HTTPException, Request, status

from lightrag.api.auth_provider import (
    AuthenticatedPrincipal,
    AuthProvider,
    AuthStatusResult,
)
from lightrag.api.identity_store import (
    create_user,
    create_workspace,
    get_membership_claims,
    get_user_auth_by_username,
    get_workspace,
    upsert_workspace_membership,
)
from lightrag.utils import logger


@dataclass(slots=True)
class LDAPSettings:
    """Parsed-from-env config for the LDAP provider."""

    server_url: str
    bind_dn: str
    bind_password: str
    user_search_base: str
    user_filter: str
    username_attr: str
    group_search_base: str | None
    group_filter: str | None
    group_attr: str
    role_mapping: dict[str, str]
    default_role: str
    use_start_tls: bool

    @classmethod
    def from_env(cls) -> "LDAPSettings":
        required = [
            "LDAP_SERVER_URL",
            "LDAP_BIND_DN",
            "LDAP_BIND_PASSWORD",
            "LDAP_USER_SEARCH_BASE",
        ]
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise RuntimeError(
                "LDAP auth provider is selected but required env vars are "
                f"missing: {', '.join(missing)}. See docs/Configuration.md."
            )

        raw_mapping = os.environ.get("LDAP_ROLE_MAPPING", "").strip()
        role_mapping: dict[str, str] = {}
        if raw_mapping:
            for entry in raw_mapping.split(","):
                if ":" not in entry:
                    continue
                group_cn, role = entry.split(":", 1)
                group_cn = group_cn.strip().lower()
                role = role.strip().lower()
                if group_cn and role:
                    role_mapping[group_cn] = role

        return cls(
            server_url=os.environ["LDAP_SERVER_URL"].strip(),
            bind_dn=os.environ["LDAP_BIND_DN"].strip(),
            bind_password=os.environ["LDAP_BIND_PASSWORD"],
            user_search_base=os.environ["LDAP_USER_SEARCH_BASE"].strip(),
            user_filter=os.environ.get(
                "LDAP_USER_FILTER",
                "(&(objectClass=inetOrgPerson)(uid={username}))",
            ),
            username_attr=os.environ.get("LDAP_USER_ATTR_USERNAME", "uid").strip(),
            group_search_base=(
                os.environ.get("LDAP_GROUP_SEARCH_BASE", "").strip() or None
            ),
            group_filter=(os.environ.get("LDAP_GROUP_FILTER", "").strip() or None),
            group_attr=os.environ.get("LDAP_GROUP_ATTR", "cn").strip(),
            role_mapping=role_mapping,
            default_role=os.environ.get("LDAP_DEFAULT_ROLE", "viewer").strip(),
            use_start_tls=_env_bool("LDAP_USE_START_TLS", default=False),
        )


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Role precedence when a user is in multiple mapped groups: the role
# further left wins. Keeps "user in admins + viewers" resolving to admin.
_ROLE_PRIORITY = ("owner", "admin", "editor", "viewer")


def _pick_best_role(candidates: list[str]) -> str:
    for candidate in _ROLE_PRIORITY:
        if candidate in candidates:
            return candidate
    return candidates[0] if candidates else "viewer"


class LDAPAuthProvider(AuthProvider):
    """AuthProvider subclass that verifies credentials against an LDAP server.

    The provider holds a parsed-settings dataclass instead of re-reading
    env vars per request; ``install_platform_state`` constructs one
    instance at startup and attaches it to ``app.state.auth_provider``.
    """

    provider_name = "ldap"
    supports_user_management = False

    def __init__(self, settings: LDAPSettings | None = None) -> None:
        self.settings = settings or LDAPSettings.from_env()

    # --- status ----------------------------------------------------------

    async def get_auth_status(self, request: Request) -> AuthStatusResult:
        # LDAP is always configured if the provider is selected; the UI
        # should not show the "首次使用" banner. Self-registration is
        # disabled for LDAP — accounts come from the upstream directory.
        return AuthStatusResult(
            auth_configured=True,
            auth_mode="ldap",
            available_providers=["ldap"],
            supports_password_login=True,
            supports_refresh_tokens=bool(
                getattr(request.app.state, "db_ready", False)
            ),
            supports_user_management=False,
            supports_self_registration=False,
            self_registration_role=None,
            message=None,
        )

    # --- password login --------------------------------------------------

    async def authenticate_password(
        self, request: Request, username: str, password: str
    ) -> AuthenticatedPrincipal | None:
        if not password:
            return None  # ldap3 silently accepts empty passwords as anon bind

        if not getattr(request.app.state, "db_ready", False):
            # LDAP requires DB-backed workspace memberships — fail closed.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="LDAP auth requires USE_DB_AUTH=true with the DB ready.",
            )

        # ldap3 is blocking; push into a worker thread so the event loop
        # keeps serving other requests while the IdP roundtrip runs.
        try:
            ldap_result = await asyncio.to_thread(
                self._authenticate_sync, username, password
            )
        except Exception as exc:
            logger.warning("LDAP authentication error for %r: %s", username, exc)
            return None

        if ldap_result is None:
            return None

        user_dn, normalized_username, ldap_role = ldap_result

        # Shadow-provision the user in the local identity store on first
        # login so memberships / audit / federation keep their invariants.
        shadow_user_id = await self._ensure_shadow_user(
            normalized_username, user_dn=user_dn
        )

        # Refresh memberships from DB every login — LDAP changes (role
        # bumps, new workspaces they've been invited to) should take
        # effect on the next sign-in without any sync job.
        memberships = await get_membership_claims(shadow_user_id)

        return AuthenticatedPrincipal(
            username=normalized_username,
            user_id=shadow_user_id,
            # Role on the token is "viewer" / "editor" / "admin" — the
            # server-wide fallback used by no-membership paths. Per-
            # workspace roles come from memberships in the DB.
            role=ldap_role,
            memberships=memberships,
            metadata={
                "auth_mode": "enabled",
                "auth_provider": self.provider_name,
                "account_source": "ldap",
                "ldap_dn": user_dn,
            },
        )

    # --- internals -------------------------------------------------------

    def _authenticate_sync(
        self, username: str, password: str
    ) -> tuple[str, str, str] | None:
        """Blocking LDAP call wrapped by asyncio.to_thread.

        Returns ``(user_dn, username, role)`` on success, or ``None`` on
        any failure (bad creds / user not found / LDAP transport error).
        Raises only for misconfiguration-level problems (e.g. bind DN is
        invalid) which should surface to operators as 5xx, not 401.
        """
        try:
            import ldap3
            from ldap3.core.exceptions import LDAPException
        except ImportError as exc:
            raise RuntimeError(
                "LDAP provider selected but the ``ldap3`` package is not "
                "installed. Run ``uv pip install ldap3`` and restart."
            ) from exc

        settings = self.settings
        server = ldap3.Server(settings.server_url, get_info=ldap3.NONE)

        # Step 1: bind as the service account and search for the user.
        try:
            with ldap3.Connection(
                server,
                user=settings.bind_dn,
                password=settings.bind_password,
                auto_bind=(
                    ldap3.AUTO_BIND_TLS_BEFORE_BIND
                    if settings.use_start_tls
                    else ldap3.AUTO_BIND_NO_TLS
                ),
            ) as svc:
                safe_username = _escape_ldap_filter(username)
                user_filter = settings.user_filter.format(username=safe_username)
                svc.search(
                    search_base=settings.user_search_base,
                    search_filter=user_filter,
                    attributes=[settings.username_attr],
                )
                if not svc.entries:
                    return None
                user_entry = svc.entries[0]
                user_dn = user_entry.entry_dn
                resolved_attr = getattr(user_entry, settings.username_attr, None)
                resolved_username = (
                    str(resolved_attr.value) if resolved_attr else username
                )

                # Step 3 (performed while svc still holds the service bind):
                # look up groups if configured, so the role mapping has its
                # source data.
                groups = self._fetch_groups(svc, user_dn)
        except LDAPException as exc:
            logger.warning("LDAP service bind / search failed: %s", exc)
            return None

        # Step 2: rebind as the user to prove they know the password.
        try:
            with ldap3.Connection(
                server,
                user=user_dn,
                password=password,
                auto_bind=(
                    ldap3.AUTO_BIND_TLS_BEFORE_BIND
                    if settings.use_start_tls
                    else ldap3.AUTO_BIND_NO_TLS
                ),
            ):
                pass  # Successful context-enter == bind succeeded.
        except LDAPException:
            return None

        # Step 4: map groups to a role.
        matched_roles = [
            settings.role_mapping[g]
            for g in groups
            if g in settings.role_mapping
        ]
        role = _pick_best_role(matched_roles) if matched_roles else settings.default_role

        return user_dn, resolved_username, role

    def _fetch_groups(self, svc, user_dn: str) -> list[str]:
        """Return lowercased group CNs the user belongs to."""
        settings = self.settings
        if not settings.group_search_base or not settings.group_filter:
            return []

        safe_dn = _escape_ldap_filter(user_dn)
        group_filter = settings.group_filter.format(user_dn=safe_dn)

        svc.search(
            search_base=settings.group_search_base,
            search_filter=group_filter,
            attributes=[settings.group_attr],
        )
        groups: list[str] = []
        for entry in svc.entries or []:
            attr = getattr(entry, settings.group_attr, None)
            if not attr:
                continue
            raw_values = attr.values if hasattr(attr, "values") else [attr.value]
            for value in raw_values or []:
                if value:
                    groups.append(str(value).strip().lower())
        return groups

    async def _ensure_shadow_user(self, username: str, *, user_dn: str) -> str:
        """Return the DB user_id for this LDAP principal, creating on first login.

        We store a random throwaway password secret — LDAP is the source
        of truth, so the local hash is never verified against a user-
        supplied password. Keeping the row lets every downstream table
        (memberships, refresh_tokens, audit_log) reference a stable
        user_id.
        """
        existing = await get_user_auth_by_username(username)
        if existing is not None:
            return existing.user_id

        # First login: create the DB shadow + a personal workspace, and
        # make the user owner of that workspace (mirrors the flow used
        # by /auth/register for self-registered users).
        unusable_secret = secrets.token_urlsafe(32)
        created = await create_user(
            username=username,
            password=unusable_secret,
            source="ldap",
            is_active=True,
        )

        # Allocate a personal workspace. Retry a handful of times on the
        # astronomically-unlikely uuid4 collision.
        for _ in range(5):
            candidate = uuid4().hex
            if await get_workspace(candidate) is None:
                personal_workspace_id = candidate
                break
        else:
            raise HTTPException(
                status_code=500,
                detail="Unable to allocate a personal workspace id for LDAP user",
            )

        await create_workspace(
            workspace_id=personal_workspace_id,
            name=f"{username}'s workspace",
            description="Personal workspace provisioned on first LDAP login.",
            owner_user_id=created.user_id,
        )
        await upsert_workspace_membership(
            created.user_id,
            personal_workspace_id,
            "owner",
            source=f"ldap_first_login:{user_dn}"[:128],
        )
        return created.user_id

    # --- user management (explicitly unsupported) ------------------------

    async def change_password(
        self,
        request: Request,
        *,
        token_info: dict,
        current_password: str,
        new_password: str,
    ) -> dict:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "Password changes go through your LDAP / AD console — "
                "LightRAG does not write back to the upstream directory."
            ),
        )


def _escape_ldap_filter(value: str) -> str:
    """Escape characters that carry meaning inside an RFC 4515 filter.

    Prevents an attacker from injecting ``)(objectClass=*)`` into the
    username field and short-circuiting the search. ldap3 ships
    ``escape_filter_chars`` — we use it if available, otherwise hand-roll
    the five mandatory escapes from RFC 4515.
    """
    try:
        from ldap3.utils.conv import escape_filter_chars

        return escape_filter_chars(value)
    except ImportError:
        escapes = {
            "\\": r"\5c",
            "*": r"\2a",
            "(": r"\28",
            ")": r"\29",
            "\x00": r"\00",
        }
        return "".join(escapes.get(ch, ch) for ch in value)
