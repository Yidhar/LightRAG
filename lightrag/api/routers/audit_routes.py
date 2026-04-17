"""
Audit management routes (PR-AUDIT-3).

Owners and admins can read the audit log for a workspace; only owners
can prune (retention delete). Platform-level "view every workspace"
access is explicitly out of scope for this commit.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from lightrag.api.audit_store import (
    delete_audit_events_before,
    get_audit_event_by_id,
    list_audit_events,
)
from lightrag.api.dependencies import get_request_context
from lightrag.api.permissions import Action, require_permission

router = APIRouter(tags=["audit"])


class AuditActor(BaseModel):
    user_id: str | None = None
    username: str | None = None
    role: str | None = None


class AuditHttpEnvelope(BaseModel):
    method: str | None = None
    path: str | None = None
    status: int | None = None


class AuditClient(BaseModel):
    ip: str | None = None
    user_agent: str | None = None


class AuditResource(BaseModel):
    type: str
    id: str | None = None


class AuditEventResponse(BaseModel):
    id: str
    occurred_at: str
    workspace_id: str | None = None
    kb_id: str | None = None
    action: str
    outcome: Literal["success", "denied", "error"]
    actor: AuditActor
    resource: AuditResource
    http: AuditHttpEnvelope
    client: AuditClient
    metadata: dict[str, Any] | None = None


class AuditEventListResponse(BaseModel):
    events: list[AuditEventResponse]
    total_count: int
    next_offset: int | None = None


class AuditDeleteResponse(BaseModel):
    status: Literal["deleted"]
    deleted_count: int
    before: str


def _to_response(row: dict[str, Any]) -> AuditEventResponse:
    return AuditEventResponse(
        id=row["id"],
        occurred_at=row["occurred_at"],
        workspace_id=row.get("workspace_id"),
        kb_id=row.get("kb_id"),
        action=row["action"],
        outcome=row["outcome"],
        actor=AuditActor(
            user_id=row.get("actor_user_id"),
            username=row.get("actor_username"),
            role=row.get("actor_role"),
        ),
        resource=AuditResource(
            type=row["resource_type"],
            id=row.get("resource_id"),
        ),
        http=AuditHttpEnvelope(
            method=row.get("http_method"),
            path=row.get("http_path"),
            status=row.get("status_code"),
        ),
        client=AuditClient(
            ip=row.get("client_ip"),
            user_agent=row.get("user_agent"),
        ),
        metadata=row.get("metadata"),
    )


def create_audit_routes(api_key: Optional[str] = None) -> APIRouter:
    view_permission = require_permission(Action.AUDIT_VIEW_WORKSPACE, api_key)
    delete_permission = require_permission(Action.AUDIT_DELETE_WORKSPACE, api_key)

    @router.get(
        "/workspaces/{workspace_id}/audit",
        response_model=AuditEventListResponse,
        dependencies=[Depends(view_permission)],
    )
    async def list_workspace_audit_events(
        request: Request,
        workspace_id: str,
        actor_user_id: str | None = Query(default=None),
        action: str | None = Query(default=None),
        outcome: str | None = Query(default=None),
        since: str | None = Query(default=None),
        until: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> AuditEventListResponse:
        """
        Paginated feed of audit events for the workspace. Filters compose
        with AND semantics; omit any parameter to widen the match.
        """
        context = get_request_context(request)
        scope_workspace = context.workspace_id or workspace_id

        rows = await list_audit_events(
            workspace_id=scope_workspace,
            actor_user_id=actor_user_id,
            action=action,
            outcome=outcome,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        events = [_to_response(row) for row in rows]

        next_offset = offset + len(events) if len(events) == limit else None
        return AuditEventListResponse(
            events=events,
            total_count=len(events),
            next_offset=next_offset,
        )

    @router.get(
        "/workspaces/{workspace_id}/audit/{event_id}",
        response_model=AuditEventResponse,
        dependencies=[Depends(view_permission)],
    )
    async def get_workspace_audit_event(
        request: Request,
        workspace_id: str,
        event_id: str,
    ) -> AuditEventResponse:
        row = await get_audit_event_by_id(event_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Audit event not found")

        # Enforce workspace scope: an operator can only see events for the
        # workspace they are authorised against.
        context = get_request_context(request)
        scope_workspace = context.workspace_id or workspace_id
        if (row.get("workspace_id") or None) != scope_workspace:
            raise HTTPException(status_code=404, detail="Audit event not found")

        return _to_response(row)

    @router.delete(
        "/workspaces/{workspace_id}/audit",
        response_model=AuditDeleteResponse,
        dependencies=[Depends(delete_permission)],
    )
    async def delete_workspace_audit_events(
        request: Request,
        workspace_id: str,
        before: str = Query(
            ...,
            description="ISO 8601 timestamp; every event strictly older than this is removed.",
        ),
    ) -> AuditDeleteResponse:
        context = get_request_context(request)
        scope_workspace = context.workspace_id or workspace_id

        deleted_count = await delete_audit_events_before(
            scope_workspace, before=before
        )
        if deleted_count == 0:
            # Not an error — just nothing matched the filter.
            return AuditDeleteResponse(
                status="deleted", deleted_count=0, before=before
            )

        return AuditDeleteResponse(
            status="deleted",
            deleted_count=deleted_count,
            before=before,
        )

    _unused_status = status  # keep the import in case future endpoints need it
    return router
