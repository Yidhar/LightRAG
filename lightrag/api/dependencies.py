"""
Request-context helpers for the staged platform auth/KB isolation rollout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request
from starlette.datastructures import State

from lightrag.api.config import sanitize_platform_identifier
_WORKSPACE_PARAM_NAMES = ("workspace_id", "workspaceId", "workspace")
_KB_PARAM_NAMES = ("kb_id", "kbId", "knowledge_base_id", "knowledgeBaseId")
_WORKSPACE_HEADERS = ("LIGHTRAG-WORKSPACE", "X-Workspace-Id")
_KB_HEADERS = ("LIGHTRAG-KB", "X-KB-Id")


@dataclass(slots=True)
class RequestContext:
    workspace_id: str | None
    kb_id: str | None
    db_user_id: str | None = None
    source: dict[str, str] = field(default_factory=dict)


def sanitize_context_identifier(
    value: str | None, *, label: str, fallback: str | None = None
) -> str | None:
    """Normalize request-scoped identifiers without changing legacy runtime behavior."""
    return sanitize_platform_identifier(value, label=label, fallback=fallback)


def install_platform_state(app: Any, args: Any) -> None:
    """Attach feature-flag and default-context placeholders onto app.state."""
    from lightrag.api.auth_provider import LocalAuthProvider

    app.state.default_workspace_id = sanitize_context_identifier(
        getattr(args, "default_workspace_id", None),
        label="DEFAULT_WORKSPACE_ID",
        fallback="default",
    )
    app.state.default_kb_id = sanitize_context_identifier(
        getattr(args, "default_kb_id", None),
        label="DEFAULT_KB_ID",
        fallback="default",
    )
    app.state.use_db_auth = bool(getattr(args, "use_db_auth", False))
    app.state.enable_kb_isolation = bool(getattr(args, "enable_kb_isolation", False))
    app.state.kb_separator = (
        (getattr(args, "kb_separator", None) or "__").strip() or "__"
    )
    app.state.db_ready = False
    app.state.identity_schema_ready = False
    app.state.env_account_seed_summary = {
        "parsed_count": 0,
        "inserted_count": 0,
        "updated_count": 0,
    }
    app.state.default_rag = None
    app.state.kb_registry = None
    app.state.rag_factory = None
    app.state.auth_provider = LocalAuthProvider()


def _read_param(request: Request, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = request.path_params.get(name)
        if value:
            return value
    return None


def _read_header(request: Request, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = request.headers.get(name)
        if value:
            return value
    return None


def _get_state(request: Request) -> State:
    return request.app.state


def resolve_workspace_id(request: Request) -> str | None:
    state = _get_state(request)
    candidate = _read_param(request, _WORKSPACE_PARAM_NAMES)
    if candidate is not None:
        return sanitize_context_identifier(
            candidate,
            label="workspace path parameter",
            fallback=state.default_workspace_id,
        )

    candidate = _read_header(request, _WORKSPACE_HEADERS)
    return sanitize_context_identifier(
        candidate,
        label="LIGHTRAG-WORKSPACE header",
        fallback=state.default_workspace_id,
    )


def resolve_kb_id(request: Request) -> str | None:
    state = _get_state(request)
    candidate = _read_param(request, _KB_PARAM_NAMES)
    if candidate is not None:
        return sanitize_context_identifier(
            candidate,
            label="kb path parameter",
            fallback=state.default_kb_id,
        )

    candidate = _read_header(request, _KB_HEADERS)
    return sanitize_context_identifier(
        candidate,
        label="LIGHTRAG-KB header",
        fallback=state.default_kb_id,
    )


def get_request_context(request: Request) -> RequestContext:
    existing = getattr(request.state, "request_context", None)
    if existing is not None:
        return existing

    workspace_id = resolve_workspace_id(request)
    kb_id = resolve_kb_id(request)

    source = {
        "workspace": "path"
        if _read_param(request, _WORKSPACE_PARAM_NAMES)
        else "header"
        if _read_header(request, _WORKSPACE_HEADERS)
        else "default",
        "kb": "path"
        if _read_param(request, _KB_PARAM_NAMES)
        else "header"
        if _read_header(request, _KB_HEADERS)
        else "default",
    }

    context = RequestContext(
        workspace_id=workspace_id,
        kb_id=kb_id,
        db_user_id=getattr(request.state, "db_user_id", None),
        source=source,
    )
    request.state.request_context = context
    return context


def get_current_workspace(request: Request) -> str | None:
    return get_request_context(request).workspace_id


def get_current_kb(request: Request) -> str | None:
    context = get_request_context(request)
    kb_id = context.kb_id
    state = _get_state(request)

    if not getattr(state, "enable_kb_isolation", False) or kb_id is None:
        return kb_id

    registry = getattr(state, "kb_registry", None)
    if registry is None:
        raise RuntimeError(
            "KB isolation is enabled, but no KnowledgeBaseRegistry is attached to app.state."
        )

    workspace_id = context.workspace_id or state.default_workspace_id
    if not registry.exists(workspace_id, kb_id):
        raise HTTPException(
            status_code=404,
            detail=f"Knowledge base '{kb_id}' was not found in workspace '{workspace_id}'.",
        )
    return kb_id


def compose_runtime_workspace(state: State, workspace_id: str, kb_id: str) -> str:
    rag_factory = getattr(state, "rag_factory", None)
    compose = getattr(rag_factory, "compose_workspace", None)
    if callable(compose):
        return compose(workspace_id, kb_id)

    kb_separator = getattr(state, "kb_separator", "__") or "__"
    return f"{workspace_id}{kb_separator}{kb_id}"


async def get_current_rag(request: Request):
    state = _get_state(request)
    if not getattr(state, "enable_kb_isolation", False):
        rag = getattr(state, "default_rag", None)
        if rag is None:
            raise RuntimeError(
                "Default LightRAG instance is not attached to app.state."
            )
        return rag

    rag_factory = getattr(state, "rag_factory", None)
    if rag_factory is None:
        raise RuntimeError(
            "KB isolation is enabled, but no RagFactory is attached to app.state."
        )

    context = get_request_context(request)
    workspace_id = context.workspace_id or state.default_workspace_id
    kb_id = get_current_kb(request) or state.default_kb_id
    return await rag_factory.get(workspace_id, kb_id)
