"""
Knowledge-base metadata management routes for WS4 / Platform V2.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from lightrag.api.audit import emit_audit_event
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
    category: str = ""


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBaseResponse]
    total_count: int


class KnowledgeBaseCategoriesResponse(BaseModel):
    workspace_id: str
    categories: list[str]


class KnowledgeBaseCreateRequest(BaseModel):
    kb_id: str | None = Field(default=None, min_length=1)
    name: str | None = Field(default=None, min_length=1)
    description: str = ""
    config_override: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    category: str = ""


class KnowledgeBaseUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    config_override: dict[str, Any] | None = None
    status: str | None = Field(default=None, min_length=1)
    # None = leave unchanged, "" = clear back to "uncategorised",
    # any other string = set / rename the tag.
    category: str | None = None

    @model_validator(mode="after")
    def validate_has_updates(self):
        if (
            self.name is None
            and self.description is None
            and self.config_override is None
            and self.status is None
            and self.category is None
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
        category=kb.category,
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

    @router.get(
        "/workspaces/{workspace_id}/kb/categories",
        response_model=KnowledgeBaseCategoriesResponse,
        dependencies=[Depends(workspace_view_permission)],
    )
    async def list_knowledge_base_categories(
        request: Request,
        workspace_id: str,
    ) -> KnowledgeBaseCategoriesResponse:
        registry = _require_kb_registry(request)
        context = get_request_context(request)
        resolved_workspace_id = context.workspace_id or workspace_id
        return KnowledgeBaseCategoriesResponse(
            workspace_id=resolved_workspace_id,
            categories=registry.list_categories(resolved_workspace_id),
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
                category=payload.category,
            )
        except ValueError as exc:
            detail = str(exc)
            status_code = (
                status.HTTP_409_CONFLICT
                if "already exists" in detail
                else status.HTTP_400_BAD_REQUEST
            )
            raise HTTPException(status_code=status_code, detail=detail) from exc

        await emit_audit_event(
            request,
            action="workspace:update",
            resource_type="kb",
            resource_id=kb.id,
            outcome="success",
            status_code=status.HTTP_201_CREATED,
            metadata={
                "operation": "create",
                "workspace_id": kb.workspace_id,
                "name": kb.name,
            },
        )

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

        # Gate: the KB must be linked to the requesting workspace so
        # workspace A can't rename a KB that only belongs to B.
        if registry.get_kb(resolved_workspace_id, resolved_kb_id) is None:
            raise HTTPException(status_code=404, detail="Knowledge base not found")

        try:
            kb = registry.update_kb(
                resolved_kb_id,
                name=payload.name,
                description=payload.description,
                config_override=payload.config_override,
                status=payload.status,
                category=payload.category,
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

        await emit_audit_event(
            request,
            action="kb:manage_settings",
            resource_type="kb",
            resource_id=resolved_kb_id,
            outcome="success",
            status_code=status.HTTP_200_OK,
            metadata={
                "operation": "update",
                "workspace_id": resolved_workspace_id,
                "name": payload.name,
                "status_field": payload.status,
            },
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
        """Delete semantics after the v2 pivot:

        * If the KB is linked to more than one workspace, this endpoint
          only *unlinks* from the current workspace — the KB stays alive
          for the other workspaces that still reference it.
        * If the current workspace is the last holder, the KB is
          deleted globally (link removed + global record removed +
          runtime evicted + doc-manager cache purged). Storage on disk
          is intentionally left in place as a safety net; operators
          can clean ``rag_storage/<kb_id>/`` manually.
        """
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

        if registry.get_kb(resolved_workspace_id, resolved_kb_id) is None:
            raise HTTPException(status_code=404, detail="Knowledge base not found")

        other_workspaces = [
            ws
            for ws in registry.list_workspaces_linking_kb(resolved_kb_id)
            if ws != resolved_workspace_id
        ]
        unlink_only = bool(other_workspaces)

        if unlink_only:
            # Shared KB — just drop the link from this workspace. The
            # data + runtime stay alive for the other linkers.
            registry.unlink_kb_from_workspace(resolved_workspace_id, resolved_kb_id)
            message = (
                f"Knowledge base '{resolved_kb_id}' unlinked from workspace "
                f"'{resolved_workspace_id}'. Still referenced by "
                f"{len(other_workspaces)} other workspace(s); data untouched."
            )
        else:
            # Last holder — delete globally.
            registry.delete_kb(resolved_kb_id)
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
            message = (
                f"Knowledge base '{resolved_kb_id}' removed. Storage under "
                f"rag_storage/{resolved_kb_id}/ was left in place for safety."
            )

        await emit_audit_event(
            request,
            action="workspace:update",
            resource_type="kb",
            resource_id=resolved_kb_id,
            outcome="success",
            status_code=status.HTTP_200_OK,
            metadata={
                "operation": "unlink" if unlink_only else "delete",
                "workspace_id": resolved_workspace_id,
                "remaining_links": len(other_workspaces),
            },
        )

        return KnowledgeBaseDeleteResponse(
            status="deleted",
            message=message,
            workspace_id=resolved_workspace_id,
            kb_id=resolved_kb_id,
        )

    # ------------------------------------------------------------------
    # New top-level + link/unlink endpoints (v2 sharing model)
    # ------------------------------------------------------------------

    @router.get(
        "/kb",
        response_model=KnowledgeBaseListResponse,
        dependencies=[Depends(workspace_view_permission)],
    )
    async def list_all_knowledge_bases(
        request: Request,
    ) -> KnowledgeBaseListResponse:
        """Every KB in the global pool, regardless of workspace linkage.

        Used by the "link existing KB" picker in the workspace
        management UI: it shows every KB the user could conceivably
        link into a workspace they own.
        """
        registry = _require_kb_registry(request)
        items = registry.list_all_kbs()
        response_items = [_to_response(item) for item in items]
        return KnowledgeBaseListResponse(
            items=response_items,
            total_count=len(response_items),
        )

    class LinkKbRequest(BaseModel):
        kb_id: str = Field(min_length=1)

    class LinkKbResponse(BaseModel):
        status: Literal["linked", "already_linked"]
        message: str
        workspace_id: str
        kb_id: str

    @router.post(
        "/workspaces/{workspace_id}/kb/link",
        response_model=LinkKbResponse,
        dependencies=[Depends(workspace_update_permission)],
    )
    async def link_existing_knowledge_base(
        request: Request,
        workspace_id: str,
        payload: LinkKbRequest,
    ) -> "LinkKbResponse":  # noqa: F821
        """Attach an existing global KB to this workspace.

        After the link is created the workspace's KB list includes
        the newly-linked KB. Idempotent — re-linking returns
        ``already_linked`` without error.
        """
        registry = _require_kb_registry(request)
        context = get_request_context(request)
        resolved_workspace_id = context.workspace_id or workspace_id
        target_kb_id = payload.kb_id.strip()
        if not target_kb_id:
            raise HTTPException(status_code=400, detail="kb_id must not be empty")

        if registry.get_kb(None, target_kb_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"Knowledge base '{target_kb_id}' does not exist.",
            )

        try:
            newly_linked = registry.link_kb_to_workspace(
                resolved_workspace_id, target_kb_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        await emit_audit_event(
            request,
            action="workspace:update",
            resource_type="kb",
            resource_id=target_kb_id,
            outcome="success",
            status_code=status.HTTP_200_OK,
            metadata={
                "operation": "link",
                "workspace_id": resolved_workspace_id,
            },
        )

        return LinkKbResponse(
            status="linked" if newly_linked else "already_linked",
            message=(
                f"Knowledge base '{target_kb_id}' linked to workspace "
                f"'{resolved_workspace_id}'."
                if newly_linked
                else (
                    f"Knowledge base '{target_kb_id}' is already linked to "
                    f"workspace '{resolved_workspace_id}'."
                )
            ),
            workspace_id=resolved_workspace_id,
            kb_id=target_kb_id,
        )

    class UnlinkKbResponse(BaseModel):
        status: Literal["unlinked"]
        message: str
        workspace_id: str
        kb_id: str
        remaining_links: int

    @router.delete(
        "/workspaces/{workspace_id}/kb/{kb_id}/link",
        response_model=UnlinkKbResponse,
        dependencies=[Depends(workspace_update_permission)],
    )
    async def unlink_knowledge_base(
        request: Request,
        workspace_id: str,
        kb_id: str,
    ) -> "UnlinkKbResponse":  # noqa: F821
        """Remove the link between this workspace and ``kb_id`` without
        deleting the KB. The KB stays alive in the global pool and
        other workspaces that still link it keep working.
        """
        registry = _require_kb_registry(request)
        context = get_request_context(request)
        resolved_workspace_id = context.workspace_id or workspace_id
        resolved_kb_id = context.kb_id or kb_id

        if not registry.unlink_kb_from_workspace(
            resolved_workspace_id, resolved_kb_id
        ):
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Knowledge base '{resolved_kb_id}' is not linked to "
                    f"workspace '{resolved_workspace_id}'."
                ),
            )

        remaining = len(registry.list_workspaces_linking_kb(resolved_kb_id))
        await emit_audit_event(
            request,
            action="workspace:update",
            resource_type="kb",
            resource_id=resolved_kb_id,
            outcome="success",
            status_code=status.HTTP_200_OK,
            metadata={
                "operation": "unlink",
                "workspace_id": resolved_workspace_id,
                "remaining_links": remaining,
            },
        )

        return UnlinkKbResponse(
            status="unlinked",
            message=(
                f"Knowledge base '{resolved_kb_id}' unlinked from workspace "
                f"'{resolved_workspace_id}'. Still referenced by {remaining} "
                f"workspace(s)."
            ),
            workspace_id=resolved_workspace_id,
            kb_id=resolved_kb_id,
            remaining_links=remaining,
        )

    return router
