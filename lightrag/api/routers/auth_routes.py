"""
Auth routes for local accounts, refresh-token lifecycle, and future remote-provider readiness.

The route shapes in this module are intended to stay stable when LightRAG later
swaps the local provider for a remote identity source.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

from lightrag.api.auth import auth_handler
from lightrag.api.auth_provider import get_auth_provider
from lightrag.api.identity_store import (
    RefreshTokenValidationError,
    get_membership_claims,
    get_user_by_id,
    get_user_by_username,
    issue_refresh_token,
    revoke_refresh_token,
    rotate_refresh_token,
)
from lightrag.api.permissions import Action, PermissionContext, require_permission
from lightrag.utils import logger

REFRESH_TOKEN_COOKIE_NAME = "lightrag_refresh_token"
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="login",
    auto_error=False,
    description="OAuth2 Password Authentication",
)


class RefreshTokenRequest(BaseModel):
    refresh_token: str | None = Field(
        default=None,
        description="Optional raw refresh token. When omitted, the server checks the secure cookie.",
    )


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)


class AuthMessageResponse(BaseModel):
    status: str
    message: str | None = None


class CurrentUserResponse(BaseModel):
    user_id: str | None = None
    username: str | None = None
    source: str | None = None
    is_active: bool
    role: str
    memberships: list[dict[str, str | None]]
    provider: str


class LocalUserSummary(BaseModel):
    user_id: str
    username: str
    source: str
    is_active: bool
    memberships: list[dict[str, str | None]] = Field(default_factory=list)


class LocalUserListResponse(BaseModel):
    items: list[LocalUserSummary]
    total_count: int
    provider: str


class LocalUserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8)
    is_active: bool = True


class LocalUserUpdateRequest(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None


class LocalUserPasswordRequest(BaseModel):
    password: str = Field(..., min_length=8)


class LocalUserMutationResponse(BaseModel):
    status: str
    message: str
    user: LocalUserSummary


def _refresh_cookie_kwargs(request: Request, max_age_seconds: int) -> dict:
    secure = request.url.scheme == "https"
    return {
        "httponly": True,
        "secure": secure,
        "samesite": "strict" if secure else "lax",
        "max_age": max_age_seconds,
        "path": "/",
    }


def set_refresh_token_cookie(
    response: Response,
    request: Request,
    refresh_token: str,
    *,
    max_age_seconds: int,
) -> None:
    response.set_cookie(
        REFRESH_TOKEN_COOKIE_NAME,
        refresh_token,
        **_refresh_cookie_kwargs(request, max_age_seconds),
    )


def clear_refresh_token_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        REFRESH_TOKEN_COOKIE_NAME,
        path=_refresh_cookie_kwargs(request, 0)["path"],
    )


def _serialize_user(
    user,
    memberships: list[dict[str, str | None]] | None = None,
) -> LocalUserSummary:
    return LocalUserSummary(
        user_id=user.user_id,
        username=user.username,
        source=user.source,
        is_active=user.is_active,
        memberships=list(memberships or []),
    )


async def _require_authenticated_token(
    token: str = Depends(oauth2_scheme),
) -> dict:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No credentials provided. Please login.",
        )
    return auth_handler.validate_token(token)


async def issue_login_tokens(
    request: Request,
    response: Response,
    *,
    username: str,
    user_id: str | None = None,
    role: str = "user",
    metadata: dict | None = None,
    memberships: list[dict[str, str | None]] | None = None,
) -> dict:
    token_payload = {
        "access_token": auth_handler.create_token(
            username=username,
            user_id=user_id,
            role=role,
            memberships=memberships or [],
            metadata=metadata or {},
        ),
        "token_type": "bearer",
    }

    if not getattr(request.app.state, "use_db_auth", False):
        return token_payload

    if not getattr(request.app.state, "db_ready", False):
        logger.warning(
            "USE_DB_AUTH is enabled but DB auth is not ready. Falling back to access-token-only login for user '%s'.",
            username,
        )
        return token_payload

    user = await get_user_by_username(username)
    if user is None:
        logger.warning(
            "DB-backed auth is enabled but no user row exists for '%s'. Falling back to access-token-only login.",
            username,
        )
        return token_payload

    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is inactive")

    session_memberships = memberships if memberships is not None else await get_membership_claims(user.user_id)
    session_role = role or "user"
    session_user_id = user_id or user.user_id
    session_jti = str(uuid4())
    refresh_issue = await issue_refresh_token(
        user.user_id,
        token_jti=session_jti,
        expires_in_hours=auth_handler.refresh_expire_hours,
    )

    set_refresh_token_cookie(
        response,
        request,
        refresh_issue.raw_token,
        max_age_seconds=int(auth_handler.refresh_expire_hours * 3600),
    )

    return {
        "access_token": auth_handler.create_token(
            username=username,
            user_id=session_user_id,
            role=session_role,
            memberships=session_memberships,
            jti=session_jti,
            metadata=metadata or {},
        ),
        "refresh_token": refresh_issue.raw_token,
        "token_type": "bearer",
    }


def _resolve_refresh_token(
    request: Request,
    payload: RefreshTokenRequest | None,
) -> str | None:
    if payload and payload.refresh_token:
        return payload.refresh_token
    return request.cookies.get(REFRESH_TOKEN_COOKIE_NAME)


async def refresh_login_tokens(
    request: Request,
    response: Response,
    *,
    payload: RefreshTokenRequest | None = None,
) -> dict:
    if not getattr(request.app.state, "use_db_auth", False):
        raise HTTPException(
            status_code=501,
            detail="Refresh tokens require USE_DB_AUTH=true",
        )

    if not getattr(request.app.state, "db_ready", False):
        raise HTTPException(
            status_code=503,
            detail="DB-backed auth is not ready",
        )

    raw_refresh_token = _resolve_refresh_token(request, payload)
    if not raw_refresh_token:
        raise HTTPException(status_code=401, detail="Missing refresh token")

    try:
        rotation = await rotate_refresh_token(
            raw_refresh_token,
            new_token_jti=str(uuid4()),
            expires_in_hours=auth_handler.refresh_expire_hours,
        )
    except RefreshTokenValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    refresh_issue = rotation.issued_refresh_token
    set_refresh_token_cookie(
        response,
        request,
        refresh_issue.raw_token,
        max_age_seconds=int(auth_handler.refresh_expire_hours * 3600),
    )

    role = "viewer" if rotation.user.source == "local" else "user"

    return {
        "access_token": auth_handler.create_token(
            username=rotation.user.username,
            user_id=rotation.user.user_id,
            role=role,
            memberships=rotation.memberships,
            jti=refresh_issue.token_jti,
            metadata={
                "auth_mode": "enabled",
                "auth_provider": "local",
                "account_source": rotation.user.source,
            },
        ),
        "refresh_token": refresh_issue.raw_token,
        "token_type": "bearer",
    }


async def _list_user_summaries(request: Request) -> list[LocalUserSummary]:
    provider = get_auth_provider(request)
    users = await provider.list_directory_users(request)
    summaries: list[LocalUserSummary] = []
    for user in users:
        memberships = await get_membership_claims(user.user_id)
        summaries.append(_serialize_user(user, memberships))
    return summaries


def create_auth_routes() -> APIRouter:
    router = APIRouter(tags=["auth"])

    @router.post(
        "/auth/refresh",
        summary="Rotate the current refresh token and mint a new access token",
        response_description="New access/refresh token pair for the active local session",
    )
    async def refresh_auth_tokens(
        request: Request,
        response: Response,
        payload: RefreshTokenRequest | None = None,
    ):
        return await refresh_login_tokens(request, response, payload=payload)

    @router.post(
        "/auth/logout",
        response_model=AuthMessageResponse,
        summary="Logout the current user session",
        description=(
            "Clears the local refresh-token cookie and invokes the provider logout hook. "
            "Future remote-auth providers should keep this route stable and implement their "
            "own upstream session revocation logic behind the same API."
        ),
    )
    async def logout(
        request: Request,
        response: Response,
        token_info: dict = Depends(_require_authenticated_token),
    ):
        raw_refresh_token = request.cookies.get(REFRESH_TOKEN_COOKIE_NAME)
        if raw_refresh_token:
            await revoke_refresh_token(raw_refresh_token)
        result = await get_auth_provider(request).logout(request, token_info=token_info)
        clear_refresh_token_cookie(response, request)
        return AuthMessageResponse(
            status=result.get("status", "success"),
            message=result.get("message"),
        )

    @router.get(
        "/auth/me",
        response_model=CurrentUserResponse,
        summary="Return the current signed-in user profile",
        description=(
            "Provider-agnostic user profile endpoint. Remote identity providers should reuse "
            "this shape so frontend account surfaces do not need contract changes."
        ),
    )
    async def auth_me(
        request: Request,
        token_info: dict = Depends(_require_authenticated_token),
    ):
        profile = await get_auth_provider(request).get_current_user_profile(token_info)
        return CurrentUserResponse(**profile)

    @router.post(
        "/auth/change-password",
        response_model=AuthMessageResponse,
        summary="Change the current user's password",
        description=(
            "Local-password endpoint reserved as a stable extension point. Remote auth backends "
            "may keep the route and forward the operation to an upstream identity service."
        ),
    )
    async def change_password(
        request: Request,
        response: Response,
        payload: ChangePasswordRequest,
        token_info: dict = Depends(_require_authenticated_token),
    ):
        result = await get_auth_provider(request).change_password(
            request,
            token_info=token_info,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
        clear_refresh_token_cookie(response, request)
        return AuthMessageResponse(
            status=result.get("status", "success"),
            message=result.get("message"),
        )

    @router.get(
        "/auth/users",
        response_model=LocalUserListResponse,
        summary="List local account-directory users",
        description=(
            "Administrative local-account API reserved for future remote directory integration. "
            "Today it lists DB-backed local users plus their scoped workspace/KB memberships."
        ),
    )
    async def list_auth_users(
        request: Request,
        _permission: PermissionContext = Depends(
            require_permission(Action.WORKSPACE_INVITE_MEMBER)
        ),
    ):
        summaries = await _list_user_summaries(request)
        return LocalUserListResponse(
            items=summaries,
            total_count=len(summaries),
            provider=get_auth_provider(request).provider_name,
        )

    @router.post(
        "/auth/users",
        response_model=LocalUserMutationResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create a local directory user",
        description=(
            "Administrative local-user bootstrap endpoint. Future remote auth providers may keep "
            "this route but reject it when user provisioning is delegated upstream."
        ),
    )
    async def create_auth_user(
        request: Request,
        payload: LocalUserCreateRequest,
        _permission: PermissionContext = Depends(
            require_permission(Action.WORKSPACE_INVITE_MEMBER)
        ),
    ):
        try:
            user = await get_auth_provider(request).create_directory_user(
                request,
                username=payload.username,
                password=payload.password,
                is_active=payload.is_active,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        return LocalUserMutationResponse(
            status="created",
            message="Local user created successfully.",
            user=_serialize_user(user),
        )

    @router.patch(
        "/auth/users/{user_id}",
        response_model=LocalUserMutationResponse,
        summary="Update a local directory user",
        description=(
            "Administrative user-metadata endpoint. Keep this route stable when integrating "
            "remote identity so provisioning clients have one consistent contract."
        ),
    )
    async def update_auth_user(
        user_id: str,
        request: Request,
        payload: LocalUserUpdateRequest,
        _permission: PermissionContext = Depends(
            require_permission(Action.WORKSPACE_INVITE_MEMBER)
        ),
    ):
        try:
            user = await get_auth_provider(request).update_directory_user(
                request,
                user_id=user_id,
                username=payload.username,
                is_active=payload.is_active,
            )
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if "was not found" in detail else 409
            raise HTTPException(status_code=status_code, detail=detail) from exc

        memberships = await get_membership_claims(user.user_id)
        return LocalUserMutationResponse(
            status="updated",
            message="Local user updated successfully.",
            user=_serialize_user(user, memberships),
        )

    @router.post(
        "/auth/users/{user_id}/password",
        response_model=LocalUserMutationResponse,
        summary="Rotate a local directory user's password",
        description=(
            "Administrative password rotation endpoint reserved as a stable integration point "
            "for remote account-management adapters."
        ),
    )
    async def update_auth_user_password(
        user_id: str,
        request: Request,
        payload: LocalUserPasswordRequest,
        _permission: PermissionContext = Depends(
            require_permission(Action.WORKSPACE_INVITE_MEMBER)
        ),
    ):
        try:
            user = await get_auth_provider(request).rotate_directory_password(
                request,
                user_id=user_id,
                password=payload.password,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        memberships = await get_membership_claims(user.user_id)
        return LocalUserMutationResponse(
            status="updated",
            message="Local user password rotated successfully.",
            user=_serialize_user(user, memberships),
        )

    return router
