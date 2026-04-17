"""
Membership management routes for workspace and KB-scoped RBAC.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from lightrag.api.audit import emit_audit_event
from lightrag.api.dependencies import get_request_context
from lightrag.api.identity_store import (
    MembershipRecord,
    delete_kb_membership,
    delete_workspace_membership,
    get_kb_membership,
    get_user_by_id,
    get_user_by_username,
    get_workspace_membership,
    list_kb_memberships,
    list_workspace_memberships,
    upsert_kb_membership,
    upsert_workspace_membership,
)
from lightrag.api.permissions import Action, require_permission

router = APIRouter(tags=["membership"])


class MembershipEntryResponse(BaseModel):
    membership_id: str
    user_id: str
    username: str
    workspace_id: str
    kb_id: str | None = None
    role: Literal["owner", "admin", "editor", "viewer"]
    source: str
    created_at: str
    updated_at: str


class MembershipListResponse(BaseModel):
    members: list[MembershipEntryResponse]
    total_count: int


class MembershipUpsertRequest(BaseModel):
    user_id: str | None = Field(default=None, min_length=1)
    username: str | None = Field(default=None, min_length=1)
    role: Literal["owner", "admin", "editor", "viewer"]

    @model_validator(mode="after")
    def validate_subject(self):
        if bool(self.user_id) == bool(self.username):
            raise ValueError("Provide exactly one of user_id or username.")
        return self


class MembershipRoleUpdateRequest(BaseModel):
    role: Literal["owner", "admin", "editor", "viewer"]


class MembershipMutationResponse(BaseModel):
    status: Literal["created", "updated"]
    message: str
    member: MembershipEntryResponse


class MembershipDeleteResponse(BaseModel):
    status: Literal["deleted"]
    message: str
    user_id: str
    workspace_id: str
    kb_id: str | None = None


def _to_response(record: MembershipRecord) -> MembershipEntryResponse:
    return MembershipEntryResponse(
        membership_id=record.membership_id,
        user_id=record.user_id,
        username=record.username,
        workspace_id=record.workspace_id,
        kb_id=record.kb_id,
        role=record.role,
        source=record.source,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _require_membership_management_support(request: Request) -> None:
    if not getattr(request.app.state, "use_db_auth", False):
        raise HTTPException(
            status_code=501,
            detail="Membership management requires USE_DB_AUTH=true",
        )
    if not getattr(request.app.state, "db_ready", False):
        raise HTTPException(
            status_code=503,
            detail="DB-backed auth is not ready",
        )


async def _resolve_target_user(payload: MembershipUpsertRequest):
    target_user = (
        await get_user_by_id(payload.user_id)
        if payload.user_id
        else await get_user_by_username(payload.username or "")
    )
    if target_user is None:
        lookup_value = payload.user_id or payload.username
        raise HTTPException(
            status_code=404,
            detail=f"Unknown user: {lookup_value}",
        )
    return target_user


def create_membership_routes(api_key: Optional[str] = None) -> APIRouter:
    workspace_members_permission = require_permission(
        Action.WORKSPACE_INVITE_MEMBER,
        api_key,
    )
    kb_members_permission = require_permission(
        Action.KB_MANAGE_PERMISSIONS,
        api_key,
    )

    @router.get(
        "/workspaces/{workspace_id}/members",
        response_model=MembershipListResponse,
        dependencies=[Depends(workspace_members_permission)],
    )
    async def get_workspace_members(
        request: Request,
        workspace_id: str,
    ) -> MembershipListResponse:
        _require_membership_management_support(request)
        context = get_request_context(request)
        members = await list_workspace_memberships(context.workspace_id or workspace_id)
        response_members = [_to_response(member) for member in members]
        return MembershipListResponse(
            members=response_members,
            total_count=len(response_members),
        )

    @router.post(
        "/workspaces/{workspace_id}/members",
        response_model=MembershipMutationResponse,
        dependencies=[Depends(workspace_members_permission)],
    )
    async def create_workspace_member(
        request: Request,
        workspace_id: str,
        payload: MembershipUpsertRequest,
    ) -> MembershipMutationResponse:
        _require_membership_management_support(request)
        context = get_request_context(request)
        target_user = await _resolve_target_user(payload)
        result = await upsert_workspace_membership(
            target_user.user_id,
            context.workspace_id or workspace_id,
            payload.role,
        )
        status_value = "created" if result.created else "updated"
        await emit_audit_event(
            request,
            action="workspace:invite_member",
            resource_type="workspace_membership",
            resource_id=target_user.user_id,
            outcome="success",
            metadata={
                "target_username": target_user.username,
                "role": payload.role,
                "membership_status": status_value,
            },
        )
        return MembershipMutationResponse(
            status=status_value,
            message=(
                f"Workspace membership {status_value} for user '{target_user.username}'."
            ),
            member=_to_response(result.membership),
        )

    @router.put(
        "/workspaces/{workspace_id}/members/{user_id}",
        response_model=MembershipMutationResponse,
        dependencies=[Depends(workspace_members_permission)],
    )
    async def update_workspace_member(
        request: Request,
        workspace_id: str,
        user_id: str,
        payload: MembershipRoleUpdateRequest,
    ) -> MembershipMutationResponse:
        _require_membership_management_support(request)
        context = get_request_context(request)
        target_user = await get_user_by_id(user_id)
        if target_user is None:
            raise HTTPException(status_code=404, detail=f"Unknown user: {user_id}")
        result = await upsert_workspace_membership(
            target_user.user_id,
            context.workspace_id or workspace_id,
            payload.role,
        )
        status_value = "created" if result.created else "updated"
        await emit_audit_event(
            request,
            action="workspace:update",
            resource_type="workspace_membership",
            resource_id=target_user.user_id,
            outcome="success",
            metadata={
                "target_username": target_user.username,
                "role": payload.role,
                "membership_status": status_value,
            },
        )
        return MembershipMutationResponse(
            status=status_value,
            message=(
                f"Workspace membership {status_value} for user '{target_user.username}'."
            ),
            member=_to_response(result.membership),
        )

    @router.delete(
        "/workspaces/{workspace_id}/members/{user_id}",
        response_model=MembershipDeleteResponse,
        dependencies=[Depends(workspace_members_permission)],
    )
    async def remove_workspace_member(
        request: Request,
        workspace_id: str,
        user_id: str,
    ) -> MembershipDeleteResponse:
        _require_membership_management_support(request)
        context = get_request_context(request)
        membership = await get_workspace_membership(
            user_id,
            context.workspace_id or workspace_id,
        )
        if membership is None:
            raise HTTPException(status_code=404, detail="Workspace membership not found")
        await delete_workspace_membership(user_id, context.workspace_id or workspace_id)
        await emit_audit_event(
            request,
            action="workspace:invite_member",
            resource_type="workspace_membership",
            resource_id=user_id,
            outcome="success",
            metadata={
                "target_username": membership.username,
                "operation": "remove",
            },
        )
        return MembershipDeleteResponse(
            status="deleted",
            message=f"Removed workspace membership for user '{membership.username}'.",
            user_id=user_id,
            workspace_id=context.workspace_id or workspace_id,
            kb_id=None,
        )

    @router.get(
        "/workspaces/{workspace_id}/kb/{kb_id}/members",
        response_model=MembershipListResponse,
        dependencies=[Depends(kb_members_permission)],
    )
    async def get_kb_members(
        request: Request,
        workspace_id: str,
        kb_id: str,
    ) -> MembershipListResponse:
        _require_membership_management_support(request)
        context = get_request_context(request)
        members = await list_kb_memberships(
            context.workspace_id or workspace_id,
            context.kb_id or kb_id,
        )
        response_members = [_to_response(member) for member in members]
        return MembershipListResponse(
            members=response_members,
            total_count=len(response_members),
        )

    @router.post(
        "/workspaces/{workspace_id}/kb/{kb_id}/members",
        response_model=MembershipMutationResponse,
        dependencies=[Depends(kb_members_permission)],
    )
    async def create_kb_member(
        request: Request,
        workspace_id: str,
        kb_id: str,
        payload: MembershipUpsertRequest,
    ) -> MembershipMutationResponse:
        _require_membership_management_support(request)
        context = get_request_context(request)
        target_user = await _resolve_target_user(payload)
        workspace_membership = await get_workspace_membership(
            target_user.user_id,
            context.workspace_id or workspace_id,
        )
        if workspace_membership is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "User must already be a workspace member before assigning KB-specific permissions."
                ),
            )
        result = await upsert_kb_membership(
            target_user.user_id,
            context.workspace_id or workspace_id,
            context.kb_id or kb_id,
            payload.role,
        )
        status_value = "created" if result.created else "updated"
        await emit_audit_event(
            request,
            action="kb:manage_permissions",
            resource_type="kb_membership",
            resource_id=target_user.user_id,
            outcome="success",
            metadata={
                "target_username": target_user.username,
                "role": payload.role,
                "membership_status": status_value,
                "operation": "upsert",
            },
        )
        return MembershipMutationResponse(
            status=status_value,
            message=f"KB membership {status_value} for user '{target_user.username}'.",
            member=_to_response(result.membership),
        )

    @router.put(
        "/workspaces/{workspace_id}/kb/{kb_id}/members/{user_id}",
        response_model=MembershipMutationResponse,
        dependencies=[Depends(kb_members_permission)],
    )
    async def update_kb_member(
        request: Request,
        workspace_id: str,
        kb_id: str,
        user_id: str,
        payload: MembershipRoleUpdateRequest,
    ) -> MembershipMutationResponse:
        _require_membership_management_support(request)
        context = get_request_context(request)
        target_user = await get_user_by_id(user_id)
        if target_user is None:
            raise HTTPException(status_code=404, detail=f"Unknown user: {user_id}")
        workspace_membership = await get_workspace_membership(
            target_user.user_id,
            context.workspace_id or workspace_id,
        )
        if workspace_membership is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "User must already be a workspace member before assigning KB-specific permissions."
                ),
            )
        result = await upsert_kb_membership(
            target_user.user_id,
            context.workspace_id or workspace_id,
            context.kb_id or kb_id,
            payload.role,
        )
        status_value = "created" if result.created else "updated"
        await emit_audit_event(
            request,
            action="kb:manage_permissions",
            resource_type="kb_membership",
            resource_id=target_user.user_id,
            outcome="success",
            metadata={
                "target_username": target_user.username,
                "role": payload.role,
                "membership_status": status_value,
                "operation": "update",
            },
        )
        return MembershipMutationResponse(
            status=status_value,
            message=f"KB membership {status_value} for user '{target_user.username}'.",
            member=_to_response(result.membership),
        )

    @router.delete(
        "/workspaces/{workspace_id}/kb/{kb_id}/members/{user_id}",
        response_model=MembershipDeleteResponse,
        dependencies=[Depends(kb_members_permission)],
    )
    async def remove_kb_member(
        request: Request,
        workspace_id: str,
        kb_id: str,
        user_id: str,
    ) -> MembershipDeleteResponse:
        _require_membership_management_support(request)
        context = get_request_context(request)
        membership = await get_kb_membership(
            user_id,
            context.workspace_id or workspace_id,
            context.kb_id or kb_id,
        )
        if membership is None:
            raise HTTPException(status_code=404, detail="KB membership not found")
        await delete_kb_membership(
            user_id,
            context.workspace_id or workspace_id,
            context.kb_id or kb_id,
        )
        await emit_audit_event(
            request,
            action="kb:manage_permissions",
            resource_type="kb_membership",
            resource_id=user_id,
            outcome="success",
            metadata={
                "target_username": membership.username,
                "operation": "remove",
            },
        )
        return MembershipDeleteResponse(
            status="deleted",
            message=f"Removed KB membership for user '{membership.username}'.",
            user_id=user_id,
            workspace_id=context.workspace_id or workspace_id,
            kb_id=context.kb_id or kb_id,
        )

    return router
