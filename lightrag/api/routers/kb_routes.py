"""
Knowledge-base metadata management routes for WS4 / Platform V2.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from lightrag.api.dependencies import compose_runtime_workspace, get_request_context
from lightrag.api.kb_registry import KnowledgeBaseRegistry
from lightrag.api.models.kb import KnowledgeBase
from lightrag.api.permissions import Action, require_permission

router = APIRouter(tags=["knowledge-bases"])


class KnowledgeBaseResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    description: str
    created_at: str
    config_override: dict[str, Any]
    status: str


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBaseResponse]
    total_count: int


class KnowledgeBaseCreateRequest(BaseModel):
    kb_id: str | None = Field(default=None, min_length=1)
    name: str | None = Field(default=None, min_length=1)
    description: str = ""
    config_override: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"


class KnowledgeBaseUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    config_override: dict[str, Any] | None = None
    status: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_has_updates(self):
        if (
            self.name is None
            and self.description is None
            and self.config_override is None
            and self.status is None
        ):
            raise ValueError("Provide at least one field to update.")
        return self


class KnowledgeBaseMutationResponse(BaseModel):
    status: Literal["created", "updated"]
    message: str
    kb: KnowledgeBaseResponse


class KnowledgeBaseDeleteResponse(BaseModel):
    status: Literal["deleted"]
    message: str
    workspace_id: str
    kb_id: str


def _to_response(kb: KnowledgeBase) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        id=kb.id,
        workspace_id=kb.workspace_id,
        name=kb.name,
        description=kb.description,
        created_at=kb.created_at,
        config_override=dict(kb.config_override),
        status=kb.status,
    )


def _require_kb_registry(request: Request) -> KnowledgeBaseRegistry:
    if not getattr(request.app.state, "enable_kb_isolation", False):
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Knowledge base management requires ENABLE_KB_ISOLATION=true",
        )

    registry = getattr(request.app.state, "kb_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Knowledge base registry is not ready",
        )
    return registry


async def _evict_kb_runtime_if_present(
    request: Request,
    *,
    workspace_id: str,
    kb_id: str,
) -> None:
    rag_factory = getattr(request.app.state, "rag_factory", None)
    if rag_factory is not None and hasattr(rag_factory, "evict"):
        await rag_factory.evict(workspace_id, kb_id)


def _cleanup_doc_manager_cache(
    request: Request,
    *,
    workspace_id: str,
    kb_id: str,
) -> None:
    cache = getattr(request.app.state, "doc_manager_cache", None)
    if not isinstance(cache, dict):
        return

    runtime_workspace = compose_runtime_workspace(
        request.app.state, workspace_id, kb_id
    )
    cache.pop(runtime_workspace, None)


def create_kb_routes(api_key: Optional[str] = None) -> APIRouter:
    workspace_view_permission = require_permission(Action.WORKSPACE_VIEW, api_key)
    workspace_update_permission = require_permission(Action.WORKSPACE_UPDATE, api_key)
    kb_view_permission = require_permission(Action.KB_VIEW, api_key)
    kb_settings_permission = require_permission(Action.KB_MANAGE_SETTINGS, api_key)

    @router.get(
        "/workspaces/{workspace_id}/kb",
        response_model=KnowledgeBaseListResponse,
        dependencies=[Depends(workspace_view_permission)],
    )
    async def list_knowledge_bases(
        request: Request,
        workspace_id: str,
    ) -> KnowledgeBaseListResponse:
        registry = _require_kb_registry(request)
        context = get_request_context(request)
        resolved_workspace_id = context.workspace_id or workspace_id
        items = registry.list_kbs(resolved_workspace_id)
        response_items = [_to_response(item) for item in items]
        return KnowledgeBaseListResponse(
            items=response_items,
            total_count=len(response_items),
        )

    @router.post(
        "/workspaces/{workspace_id}/kb",
        response_model=KnowledgeBaseMutationResponse,
        dependencies=[Depends(workspace_update_permission)],
    )
    async def create_knowledge_base(
        request: Request,
        workspace_id: str,
        payload: KnowledgeBaseCreateRequest,
    ) -> KnowledgeBaseMutationResponse:
        registry = _require_kb_registry(request)
        context = get_request_context(request)
        resolved_workspace_id = context.workspace_id or workspace_id
        try:
            kb = registry.create_kb(
                resolved_workspace_id,
                kb_id=payload.kb_id,
                name=payload.name,
                description=payload.description,
                config_override=payload.config_override,
                status=payload.status,
            )
        except ValueError as exc:
            detail = str(exc)
            status_code = (
                status.HTTP_409_CONFLICT
                if "already exists" in detail
                else status.HTTP_400_BAD_REQUEST
            )
            raise HTTPException(status_code=status_code, detail=detail) from exc

        return KnowledgeBaseMutationResponse(
            status="created",
            message=f"Knowledge base '{kb.id}' created in workspace '{kb.workspace_id}'.",
            kb=_to_response(kb),
        )

    @router.get(
        "/workspaces/{workspace_id}/kb/{kb_id}",
        response_model=KnowledgeBaseResponse,
        dependencies=[Depends(kb_view_permission)],
    )
    async def get_knowledge_base(
        request: Request,
        workspace_id: str,
        kb_id: str,
    ) -> KnowledgeBaseResponse:
        registry = _require_kb_registry(request)
        context = get_request_context(request)
        resolved_workspace_id = context.workspace_id or workspace_id
        resolved_kb_id = context.kb_id or kb_id
        kb = registry.get_kb(resolved_workspace_id, resolved_kb_id)
        if kb is None:
            raise HTTPException(status_code=404, detail="Knowledge base not found")
        return _to_response(kb)

    @router.patch(
        "/workspaces/{workspace_id}/kb/{kb_id}",
        response_model=KnowledgeBaseMutationResponse,
        dependencies=[Depends(kb_settings_permission)],
    )
    async def update_knowledge_base(
        request: Request,
        workspace_id: str,
        kb_id: str,
        payload: KnowledgeBaseUpdateRequest,
    ) -> KnowledgeBaseMutationResponse:
        registry = _require_kb_registry(request)
        context = get_request_context(request)
        resolved_workspace_id = context.workspace_id or workspace_id
        resolved_kb_id = context.kb_id or kb_id
        try:
            kb = registry.update_kb(
                resolved_workspace_id,
                resolved_kb_id,
                name=payload.name,
                description=payload.description,
                config_override=payload.config_override,
                status=payload.status,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if kb is None:
            raise HTTPException(status_code=404, detail="Knowledge base not found")

        await _evict_kb_runtime_if_present(
            request,
            workspace_id=resolved_workspace_id,
            kb_id=resolved_kb_id,
        )

        return KnowledgeBaseMutationResponse(
            status="updated",
            message=f"Knowledge base '{resolved_kb_id}' updated.",
            kb=_to_response(kb),
        )

    @router.delete(
        "/workspaces/{workspace_id}/kb/{kb_id}",
        response_model=KnowledgeBaseDeleteResponse,
        dependencies=[Depends(workspace_update_permission)],
    )
    async def delete_knowledge_base(
        request: Request,
        workspace_id: str,
        kb_id: str,
    ) -> KnowledgeBaseDeleteResponse:
        registry = _require_kb_registry(request)
        context = get_request_context(request)
        resolved_workspace_id = context.workspace_id or workspace_id
        resolved_kb_id = context.kb_id or kb_id
        default_kb_id = getattr(request.app.state, "default_kb_id", "default")

        if resolved_kb_id == default_kb_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "The default knowledge base cannot be deleted while compatibility mode is enabled."
                ),
            )

        kb = registry.get_kb(resolved_workspace_id, resolved_kb_id)
        if kb is None:
            raise HTTPException(status_code=404, detail="Knowledge base not found")

        deleted = registry.delete_kb(resolved_workspace_id, resolved_kb_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Knowledge base not found")

        await _evict_kb_runtime_if_present(
            request,
            workspace_id=resolved_workspace_id,
            kb_id=resolved_kb_id,
        )
        _cleanup_doc_manager_cache(
            request,
            workspace_id=resolved_workspace_id,
            kb_id=resolved_kb_id,
        )

        return KnowledgeBaseDeleteResponse(
            status="deleted",
            message=(
                f"Knowledge base '{resolved_kb_id}' was removed from workspace '{resolved_workspace_id}'. "
                "Existing storage data was left in place."
            ),
            workspace_id=resolved_workspace_id,
            kb_id=resolved_kb_id,
        )

    return router
