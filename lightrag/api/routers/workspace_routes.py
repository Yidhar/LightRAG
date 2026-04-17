"""
Workspace metadata CRUD (Phase W1).

Before Phase W1 the UI could only list workspaces it derived from
memberships encoded in the JWT. These routes give the frontend a proper
read/write surface for workspace metadata so users can list, create,
rename, and delete workspaces.

Permissions:

- ``GET  /workspaces``                — authenticated user, returns
                                       workspaces they belong to.
- ``POST /workspaces``                — authenticated user, creates a
                                       workspace and assigns the
                                       creator an ``owner`` membership.
- ``GET  /workspaces/{id}``           — requires ``workspace:view`` on
                                       the target workspace.
- ``PATCH /workspaces/{id}``          — requires ``workspace:update``.
- ``DELETE /workspaces/{id}``         — requires ``workspace:delete``.
"""

from __future__ import annotations

from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from lightrag.api.audit import emit_audit_event
from lightrag.api.config import sanitize_platform_identifier
from lightrag.api.dependencies import get_request_context
from lightrag.api.identity_store import (
    WorkspaceRecord,
    create_workspace,
    delete_workspace,
    get_workspace,
    list_workspaces_for_user,
    update_workspace,
    upsert_workspace_membership,
)
from lightrag.api.permissions import Action, require_permission
from lightrag.api.utils_api import get_combined_auth_dependency

router = APIRouter(tags=["workspaces"])


class WorkspaceResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    owner_user_id: str | None = None
    created_at: str
    updated_at: str


class WorkspaceListResponse(BaseModel):
    items: list[WorkspaceResponse]
    total_count: int


class WorkspaceCreateRequest(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1024)


class WorkspaceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def validate_has_updates(self):
        if self.name is None and self.description is None:
            raise ValueError("Provide at least one field to update.")
        return self


class WorkspaceDeleteResponse(BaseModel):
    status: str
    message: str
    id: str


def _to_response(record: WorkspaceRecord) -> WorkspaceResponse:
    return WorkspaceResponse(
        id=record.id,
        name=record.name,
        description=record.description,
        owner_user_id=record.owner_user_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _require_workspace_store(request: Request) -> None:
    if not getattr(request.app.state, "use_db_auth", False):
        raise HTTPException(
            status_code=501,
            detail="Workspace management requires USE_DB_AUTH=true",
        )
    if not getattr(request.app.state, "db_ready", False):
        raise HTTPException(
            status_code=503,
            detail="DB-backed auth is not ready",
        )


def _sanitize_workspace_id(raw_id: str) -> str:
    sanitized = sanitize_platform_identifier(raw_id, label="workspace id")
    if not sanitized:
        raise HTTPException(
            status_code=400,
            detail="workspace id must be a non-empty, slug-safe string",
        )
    return sanitized


def _generate_workspace_id() -> str:
    """Generate an opaque, slug-safe workspace id (32-char uuid4 hex)."""
    return uuid4().hex


def create_workspace_routes(api_key: Optional[str] = None) -> APIRouter:
    combined_auth = get_combined_auth_dependency(api_key)
    view_permission = require_permission(Action.WORKSPACE_VIEW, api_key)
    update_permission = require_permission(Action.WORKSPACE_UPDATE, api_key)
    delete_permission = require_permission(Action.WORKSPACE_DELETE, api_key)

    @router.get(
        "/workspaces",
        response_model=WorkspaceListResponse,
        dependencies=[Depends(combined_auth)],
    )
    async def get_workspaces(request: Request) -> WorkspaceListResponse:
        """
        List the workspaces the caller has membership in.

        Authentication is required; no per-workspace permission check is
        applied because the filter is already user-scoped.
        """
        _require_workspace_store(request)
        context = get_request_context(request)

        if not context.db_user_id:
            # Callers authenticated via a legacy bearer (no resolved db user)
            # simply see nothing; they can always ask for a specific
            # workspace by id through the view permission path.
            return WorkspaceListResponse(items=[], total_count=0)

        records = await list_workspaces_for_user(context.db_user_id)
        responses = [_to_response(record) for record in records]
        return WorkspaceListResponse(items=responses, total_count=len(responses))

    @router.post(
        "/workspaces",
        response_model=WorkspaceResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(combined_auth)],
    )
    async def create_workspace_endpoint(
        request: Request,
        payload: WorkspaceCreateRequest,
    ) -> WorkspaceResponse:
        """
        Create a workspace and assign the caller as its owner.

        Any authenticated user may create a workspace; the resulting
        ownership is what gates subsequent actions.
        """
        _require_workspace_store(request)
        context = get_request_context(request)

        if not context.db_user_id:
            raise HTTPException(
                status_code=403,
                detail="Creating a workspace requires an authenticated DB user",
            )

        # Client may optionally provide an explicit id (legacy / import paths).
        # The UI no longer exposes this field; new workspaces get an opaque
        # uuid4 hex so users never have to think up a slug.
        if payload.id:
            workspace_id = _sanitize_workspace_id(payload.id)
        else:
            # Retry a few times in the astronomically unlikely collision case.
            for _ in range(5):
                candidate = _generate_workspace_id()
                if await get_workspace(candidate) is None:
                    workspace_id = candidate
                    break
            else:
                raise HTTPException(
                    status_code=500,
                    detail="Unable to allocate a workspace id after repeated retries",
                )

        existing = await get_workspace(workspace_id)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Workspace '{workspace_id}' already exists",
            )

        record = await create_workspace(
            workspace_id=workspace_id,
            name=payload.name,
            description=payload.description,
            owner_user_id=context.db_user_id,
        )

        # Give the creator an owner membership so they can manage it.
        await upsert_workspace_membership(
            user_id=context.db_user_id,
            workspace_id=workspace_id,
            role="owner",
            source="workspace_create",
        )

        await emit_audit_event(
            request,
            action="workspace:create",
            resource_type="workspace",
            resource_id=workspace_id,
            outcome="success",
            status_code=status.HTTP_201_CREATED,
            metadata={
                "name": record.name,
                "description": record.description,
            },
        )

        return _to_response(record)

    @router.get(
        "/workspaces/{workspace_id}",
        response_model=WorkspaceResponse,
        dependencies=[Depends(view_permission)],
    )
    async def get_workspace_endpoint(
        request: Request,
        workspace_id: str,
    ) -> WorkspaceResponse:
        _require_workspace_store(request)
        record = await get_workspace(workspace_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"Workspace '{workspace_id}' not found",
            )
        return _to_response(record)

    @router.patch(
        "/workspaces/{workspace_id}",
        response_model=WorkspaceResponse,
        dependencies=[Depends(update_permission)],
    )
    async def update_workspace_endpoint(
        request: Request,
        workspace_id: str,
        payload: WorkspaceUpdateRequest,
    ) -> WorkspaceResponse:
        _require_workspace_store(request)
        record = await update_workspace(
            workspace_id,
            name=payload.name,
            description=payload.description,
        )
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"Workspace '{workspace_id}' not found",
            )
        await emit_audit_event(
            request,
            action="workspace:update",
            resource_type="workspace",
            resource_id=workspace_id,
            outcome="success",
            status_code=status.HTTP_200_OK,
            metadata={
                "name": payload.name,
                "description": payload.description,
            },
        )
        return _to_response(record)

    @router.delete(
        "/workspaces/{workspace_id}",
        response_model=WorkspaceDeleteResponse,
        dependencies=[Depends(delete_permission)],
    )
    async def delete_workspace_endpoint(
        request: Request,
        workspace_id: str,
    ) -> WorkspaceDeleteResponse:
        _require_workspace_store(request)

        # Guard against deleting the default workspace; callers who truly
        # want to wipe it must do so via the migration tooling.
        default_workspace_id = getattr(
            request.app.state, "default_workspace_id", "default"
        )
        if workspace_id == default_workspace_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The default workspace '{default_workspace_id}' cannot "
                    "be deleted via this endpoint"
                ),
            )

        removed = await delete_workspace(workspace_id)
        if not removed:
            raise HTTPException(
                status_code=404,
                detail=f"Workspace '{workspace_id}' not found",
            )

        await emit_audit_event(
            request,
            action="workspace:delete",
            resource_type="workspace",
            resource_id=workspace_id,
            outcome="success",
            status_code=status.HTTP_200_OK,
        )

        return WorkspaceDeleteResponse(
            status="deleted",
            message=f"Workspace '{workspace_id}' and its memberships were removed.",
            id=workspace_id,
        )

    return router
