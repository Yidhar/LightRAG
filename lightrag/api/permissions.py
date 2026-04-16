"""
Centralized permission resolution for the staged RBAC rollout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer

from lightrag.api.auth import auth_handler
from lightrag.api.dependencies import get_request_context
from lightrag.api.utils_api import get_combined_auth_dependency


class Action:
    WORKSPACE_VIEW = "workspace:view"
    WORKSPACE_UPDATE = "workspace:update"
    WORKSPACE_INVITE_MEMBER = "workspace:invite_member"
    WORKSPACE_DELETE = "workspace:delete"
    KB_VIEW = "kb:view"
    KB_QUERY = "kb:query"
    KB_UPLOAD_DOCUMENT = "kb:upload_document"
    KB_DELETE_DOCUMENT = "kb:delete_document"
    KB_EDIT_GRAPH = "kb:edit_graph"
    KB_MANAGE_SETTINGS = "kb:manage_settings"
    KB_MANAGE_PERMISSIONS = "kb:manage_permissions"


ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": {
        Action.WORKSPACE_VIEW,
        Action.WORKSPACE_UPDATE,
        Action.WORKSPACE_INVITE_MEMBER,
        Action.WORKSPACE_DELETE,
        Action.KB_VIEW,
        Action.KB_QUERY,
        Action.KB_UPLOAD_DOCUMENT,
        Action.KB_DELETE_DOCUMENT,
        Action.KB_EDIT_GRAPH,
        Action.KB_MANAGE_SETTINGS,
        Action.KB_MANAGE_PERMISSIONS,
    },
    "admin": {
        Action.WORKSPACE_VIEW,
        Action.WORKSPACE_UPDATE,
        Action.WORKSPACE_INVITE_MEMBER,
        Action.KB_VIEW,
        Action.KB_QUERY,
        Action.KB_UPLOAD_DOCUMENT,
        Action.KB_DELETE_DOCUMENT,
        Action.KB_EDIT_GRAPH,
        Action.KB_MANAGE_SETTINGS,
    },
    "editor": {
        Action.WORKSPACE_VIEW,
        Action.KB_VIEW,
        Action.KB_QUERY,
        Action.KB_UPLOAD_DOCUMENT,
        Action.KB_DELETE_DOCUMENT,
        Action.KB_EDIT_GRAPH,
    },
    "viewer": {
        Action.WORKSPACE_VIEW,
        Action.KB_VIEW,
        Action.KB_QUERY,
    },
}

LEGACY_ROLE_FALLBACK: dict[str, str] = {
    # V1 compatibility: a single logged-in "user" historically had full access.
    "user": "owner",
    "owner": "owner",
    "admin": "admin",
    "editor": "editor",
    "viewer": "viewer",
    "guest": "viewer",
}


@dataclass(slots=True)
class PermissionContext:
    action: str
    effective_role: str
    workspace_id: str | None
    kb_id: str | None
    username: str | None = None
    user_id: str | None = None
    memberships: list[dict[str, str | None]] = field(default_factory=list)
    principal_type: str = "token"
    auth_mode: str = "enabled"


def map_legacy_role(role: str | None) -> str:
    return LEGACY_ROLE_FALLBACK.get((role or "").lower(), "viewer")


def resolve_effective_role(
    token_info: dict | None,
    *,
    workspace_id: str | None,
    kb_id: str | None,
) -> str:
    if not token_info:
        return "viewer"

    memberships = token_info.get("memberships") or []
    if memberships:
        for claim in memberships:
            if (
                claim.get("workspace_id") == workspace_id
                and claim.get("kb_id") == kb_id
            ):
                return str(claim.get("role") or "viewer")

        for claim in memberships:
            if (
                claim.get("workspace_id") == workspace_id
                and claim.get("kb_id") is None
            ):
                return str(claim.get("role") or "viewer")

        return "viewer"

    return map_legacy_role(token_info.get("role"))


def has_permission(role: str, action: str) -> bool:
    return action in ROLE_PERMISSIONS.get(role, set())


def require_permission(action: str, api_key: Optional[str] = None):
    """
    Enforce RBAC while preserving legacy auth-disabled and V1 flat-role behavior.
    """

    combined_auth = get_combined_auth_dependency(api_key)
    oauth2_scheme = OAuth2PasswordBearer(
        tokenUrl="login",
        auto_error=False,
        description="OAuth2 Password Authentication",
    )
    api_key_header = None
    if api_key:
        api_key_header = APIKeyHeader(
            name="X-API-Key",
            auto_error=False,
            description="API Key Authentication",
        )

    async def _check(
        request: Request,
        _auth: None = Depends(combined_auth),
        token: str = Security(oauth2_scheme),
        api_key_header_value: Optional[str] = None
        if api_key_header is None
        else Security(api_key_header),
    ) -> PermissionContext:
        api_key_configured = bool(api_key)
        context = get_request_context(request)
        permission_kb_id = (
            None if action.startswith("workspace:") else context.kb_id
        )

        if api_key_configured and api_key_header_value and api_key_header_value == api_key:
            permission_context = PermissionContext(
                action=action,
                effective_role="owner",
                workspace_id=context.workspace_id,
                kb_id=context.kb_id,
                username="api_key",
                principal_type="api_key",
                auth_mode="api_key",
            )
            request.state.permission_context = permission_context
            return permission_context

        token_info = getattr(request.state, "token_info", None)
        if token_info is None and token:
            token_info = auth_handler.validate_token(token)

        if token_info is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No credentials provided. Please login.",
            )

        effective_role = resolve_effective_role(
            token_info,
            workspace_id=context.workspace_id,
            kb_id=permission_kb_id,
        )
        if not has_permission(effective_role, action):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied for action '{action}'",
            )

        permission_context = PermissionContext(
            action=action,
            effective_role=effective_role,
            workspace_id=context.workspace_id,
            kb_id=context.kb_id,
            username=token_info.get("username"),
            user_id=token_info.get("user_id"),
            memberships=list(token_info.get("memberships") or []),
            principal_type="token",
            auth_mode=str(token_info.get("metadata", {}).get("auth_mode", "enabled")),
        )
        request.state.permission_context = permission_context
        return permission_context

    return _check


__all__ = [
    "Action",
    "PermissionContext",
    "ROLE_PERMISSIONS",
    "has_permission",
    "map_legacy_role",
    "require_permission",
    "resolve_effective_role",
]
