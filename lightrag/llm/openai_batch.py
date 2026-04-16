from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Awaitable, Callable, Sequence

from lightrag.utils import logger

from .openai import create_openai_async_client

BatchStatusCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
BatchCancelCallback = Callable[[], Awaitable[bool] | bool]

_FINAL_BATCH_STATUSES = {"completed", "failed", "cancelled", "expired"}


@dataclass(slots=True)
class OpenAIBatchRequest:
    """Single OpenAI-compatible batch request entry."""

    custom_id: str
    body: dict[str, Any]
    method: str = "POST"
    url: str = "/v1/chat/completions"


@dataclass(slots=True)
class OpenAIBatchRequestCounts:
    total: int = 0
    completed: int = 0
    failed: int = 0


@dataclass(slots=True)
class OpenAIBatchJobResult:
    batch_id: str
    status: str
    output_lines: list[dict[str, Any]] = field(default_factory=list)
    error_lines: list[dict[str, Any]] = field(default_factory=list)
    input_file_id: str | None = None
    output_file_id: str | None = None
    error_file_id: str | None = None
    request_counts: OpenAIBatchRequestCounts = field(
        default_factory=OpenAIBatchRequestCounts
    )
    metadata: dict[str, Any] = field(default_factory=dict)


def _sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, str] | None:
    if not metadata:
        return None

    sanitized: dict[str, str] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        sanitized[str(key)] = str(value)[:512]
    return sanitized or None


def _normalize_chat_request_body(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize OpenAI client kwargs into raw batch request JSON.

    The Python SDK accepts ``extra_body`` as a client-side convenience wrapper.
    Batch JSONL lines, however, must contain the final request body sent to the
    server. Therefore we merge ``extra_body`` into the top-level request JSON.
    """

    normalized = dict(body)
    extra_body = normalized.pop("extra_body", None)

    compact: dict[str, Any] = {}
    for key, value in normalized.items():
        if value is None:
            continue
        if isinstance(value, str) and not value and key not in {"model"}:
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        compact[key] = value

    if isinstance(extra_body, dict):
        for key, value in extra_body.items():
            if value is None:
                continue
            compact[key] = value

    return compact


def _extract_request_counts(batch: Any) -> OpenAIBatchRequestCounts:
    request_counts = getattr(batch, "request_counts", None)
    if request_counts is None:
        return OpenAIBatchRequestCounts()

    def _coerce_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return OpenAIBatchRequestCounts(
        total=_coerce_int(getattr(request_counts, "total", 0)),
        completed=_coerce_int(getattr(request_counts, "completed", 0)),
        failed=_coerce_int(getattr(request_counts, "failed", 0)),
    )


def _build_status_payload(batch: Any, *, input_file_id: str | None) -> dict[str, Any]:
    request_counts = _extract_request_counts(batch)
    return {
        "batch_id": getattr(batch, "id", None),
        "status": getattr(batch, "status", None),
        "input_file_id": input_file_id,
        "output_file_id": getattr(batch, "output_file_id", None),
        "error_file_id": getattr(batch, "error_file_id", None),
        "request_counts": {
            "total": request_counts.total,
            "completed": request_counts.completed,
            "failed": request_counts.failed,
        },
        "created_at": getattr(batch, "created_at", None),
        "in_progress_at": getattr(batch, "in_progress_at", None),
        "completed_at": getattr(batch, "completed_at", None),
        "failed_at": getattr(batch, "failed_at", None),
        "cancelled_at": getattr(batch, "cancelled_at", None),
        "expired_at": getattr(batch, "expired_at", None),
        "metadata": getattr(batch, "metadata", None),
    }


async def _maybe_call_status_callback(
    callback: BatchStatusCallback | None, payload: dict[str, Any]
) -> None:
    if callback is None:
        return
    result = callback(payload)
    if asyncio.iscoroutine(result):
        await result


async def _should_cancel(callback: BatchCancelCallback | None) -> bool:
    if callback is None:
        return False
    result = callback()
    if asyncio.iscoroutine(result):
        return bool(await result)
    return bool(result)


async def _read_jsonl_file_lines(client: Any, file_id: str | None) -> list[dict[str, Any]]:
    if not file_id:
        return []

    response = await client.files.content(file_id)
    payload = await response.aread()
    if not payload:
        return []

    lines: list[dict[str, Any]] = []
    for raw_line in payload.decode("utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Failed to parse OpenAI batch JSONL line from file %s: %s",
                file_id,
                exc,
            )
            continue
        if isinstance(parsed, dict):
            lines.append(parsed)
    return lines


async def run_openai_chat_batch(
    requests: Sequence[OpenAIBatchRequest],
    *,
    api_key: str,
    base_url: str | None = None,
    timeout: int | None = None,
    completion_window: str = "24h",
    poll_interval_seconds: int = 5,
    timeout_seconds: int = 3600,
    metadata: dict[str, Any] | None = None,
    on_status: BatchStatusCallback | None = None,
    should_cancel: BatchCancelCallback | None = None,
    client_configs: dict[str, Any] | None = None,
) -> OpenAIBatchJobResult:
    """Submit an OpenAI-compatible chat batch job and wait for completion."""

    if not requests:
        return OpenAIBatchJobResult(batch_id="", status="completed")

    client = create_openai_async_client(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        client_configs=client_configs or {},
    )

    input_file_id: str | None = None
    try:
        jsonl_lines = []
        for request in requests:
            jsonl_lines.append(
                json.dumps(
                    {
                        "custom_id": request.custom_id,
                        "method": request.method,
                        "url": request.url,
                        "body": _normalize_chat_request_body(request.body),
                    },
                    ensure_ascii=False,
                )
            )

        buffer = BytesIO(("\n".join(jsonl_lines) + "\n").encode("utf-8"))
        buffer.name = f"entity-extract-{int(time.time())}.jsonl"

        uploaded_file = await client.files.create(file=buffer, purpose="batch")
        input_file_id = uploaded_file.id

        batch = await client.batches.create(
            input_file_id=input_file_id,
            endpoint="/v1/chat/completions",
            completion_window=completion_window,
            metadata=_sanitize_metadata(metadata),
        )

        status_payload = _build_status_payload(batch, input_file_id=input_file_id)
        await _maybe_call_status_callback(on_status, status_payload)
        last_status_signature = json.dumps(
            status_payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

        deadline = time.monotonic() + max(timeout_seconds, 1)
        while getattr(batch, "status", None) not in _FINAL_BATCH_STATUSES:
            if await _should_cancel(should_cancel):
                try:
                    await client.batches.cancel(batch.id)
                except Exception as exc:  # pragma: no cover - best effort cleanup
                    logger.warning(
                        "Failed to cancel OpenAI batch %s after caller cancellation: %s",
                        batch.id,
                        exc,
                    )
                raise asyncio.CancelledError(
                    f"OpenAI batch {batch.id} was cancelled by caller"
                )

            if time.monotonic() > deadline:
                try:
                    await client.batches.cancel(batch.id)
                except Exception as exc:  # pragma: no cover - best effort cleanup
                    logger.warning(
                        "Failed to cancel timed out OpenAI batch %s: %s",
                        batch.id,
                        exc,
                    )
                raise TimeoutError(
                    f"Timed out while waiting for OpenAI batch {batch.id} after "
                    f"{timeout_seconds} seconds"
                )

            await asyncio.sleep(max(poll_interval_seconds, 1))
            batch = await client.batches.retrieve(batch.id)
            status_payload = _build_status_payload(batch, input_file_id=input_file_id)
            status_signature = json.dumps(
                status_payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if status_signature != last_status_signature:
                await _maybe_call_status_callback(on_status, status_payload)
                last_status_signature = status_signature

        output_lines = await _read_jsonl_file_lines(
            client, getattr(batch, "output_file_id", None)
        )
        error_lines = await _read_jsonl_file_lines(
            client, getattr(batch, "error_file_id", None)
        )

        return OpenAIBatchJobResult(
            batch_id=getattr(batch, "id", ""),
            status=getattr(batch, "status", ""),
            output_lines=output_lines,
            error_lines=error_lines,
            input_file_id=input_file_id,
            output_file_id=getattr(batch, "output_file_id", None),
            error_file_id=getattr(batch, "error_file_id", None),
            request_counts=_extract_request_counts(batch),
            metadata=dict(getattr(batch, "metadata", None) or {}),
        )
    finally:
        await client.close()
