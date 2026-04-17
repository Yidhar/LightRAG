"""
Audit emitter (Phase AUDIT-2).

High-level API that route handlers, the auth provider, and the
permission layer call to record an event. Pulls actor + scope out of
the request context (set by the auth middleware), captures the HTTP
envelope off the starlette Request object, and hands the event to
``audit_store.insert_audit_event`` — which is best-effort: audit
writes never fail the user request.

What this module intentionally does NOT do:

- No HTTP middleware yet. status_code is therefore only populated when
  the caller passes it in (denial events do). A middleware-driven flush
  of status_code for success events lands with the query surface
  (PR-AUDIT-2-part-2).
- No batching / buffering. Every emit writes a row directly. The volume
  is expected to be low-hundreds per day for a typical deployment;
  revisit only if the audit_log table becomes a hotspot.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import Request

from lightrag.api.audit_store import AuditEventRow, insert_audit_event
from lightrag.utils import logger


def _extract_client_ip(request: Request) -> str | None:
    # Honour X-Forwarded-For when present (first hop is the caller).
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    if request.client is not None:
        return request.client.host
    return None


def _extract_user_agent(request: Request) -> str | None:
    ua = request.headers.get("user-agent")
    if ua is None:
        return None
    return ua[:512]


def _extract_actor(request: Request) -> tuple[str | None, str | None, str | None]:
    """Return (user_id, username, role) best-effort from request.state."""
    state = getattr(request, "state", None)
    user_id = getattr(state, "db_user_id", None) if state is not None else None
    # Username and role may be stashed by the auth middleware; fall back
    # to None if the deployment hasn't wired them yet.
    username = getattr(state, "actor_username", None) if state is not None else None
    role = getattr(state, "actor_role", None) if state is not None else None
    return user_id, username, role


def _extract_scope(request: Request) -> tuple[str | None, str | None]:
    # Prefer the already-resolved request context produced by
    # dependencies.get_request_context; fall back to path params if the
    # context hasn't been built (e.g. unauthenticated paths).
    try:
        from lightrag.api.dependencies import get_request_context
    except Exception:
        return None, None

    try:
        context = get_request_context(request)
    except Exception:
        return None, None

    return context.workspace_id, context.kb_id


async def emit_audit_event(
    request: Request,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    outcome: Literal["success", "denied", "error"] = "success",
    status_code: int | None = None,
    metadata: dict[str, Any] | None = None,
    actor_user_id: str | None = None,
    actor_username: str | None = None,
    actor_role: str | None = None,
) -> None:
    """
    Record a single audit event.

    Any of ``actor_user_id``, ``actor_username``, ``actor_role`` may be
    passed explicitly; otherwise they are pulled from request.state.
    ``metadata`` values must be JSON-serialisable and contain no
    secrets (see docs/platform-v2/audit-log-design.md §3.2).
    """
    implicit_user, implicit_username, implicit_role = _extract_actor(request)
    workspace_id, kb_id = _extract_scope(request)

    event = AuditEventRow(
        action=action,
        resource_type=resource_type,
        outcome=outcome,
        actor_user_id=actor_user_id or implicit_user,
        actor_username=actor_username or implicit_username,
        actor_role=actor_role or implicit_role,
        workspace_id=workspace_id,
        kb_id=kb_id,
        resource_id=resource_id,
        http_method=request.method if request else None,
        http_path=request.url.path if request else None,
        status_code=status_code,
        client_ip=_extract_client_ip(request) if request else None,
        user_agent=_extract_user_agent(request) if request else None,
        metadata=metadata,
    )

    ok = await insert_audit_event(event)
    if not ok:
        logger.warning(
            "Audit write dropped (backend unavailable): action=%s resource=%s outcome=%s",
            action,
            resource_type,
            outcome,
        )
