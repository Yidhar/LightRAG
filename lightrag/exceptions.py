from __future__ import annotations

import httpx
from typing import Literal


class APIStatusError(Exception):
    """Raised when an API response has a status code of 4xx or 5xx."""

    response: httpx.Response
    status_code: int
    request_id: str | None

    def __init__(
        self, message: str, *, response: httpx.Response, body: object | None
    ) -> None:
        super().__init__(message)
        self.request = response.request
        self.body = body
        self.response = response
        self.status_code = response.status_code
        self.request_id = response.headers.get("x-request-id")


class APIConnectionError(Exception):
    def __init__(
        self, *, message: str = "Connection error.", request: httpx.Request | None
    ) -> None:
        super().__init__(message)
        self.request = request


class BadRequestError(APIStatusError):
    status_code: Literal[400] = 400  # pyright: ignore[reportIncompatibleVariableOverride]


class AuthenticationError(APIStatusError):
    status_code: Literal[401] = 401  # pyright: ignore[reportIncompatibleVariableOverride]


class PermissionDeniedError(APIStatusError):
    status_code: Literal[403] = 403  # pyright: ignore[reportIncompatibleVariableOverride]


class NotFoundError(APIStatusError):
    status_code: Literal[404] = 404  # pyright: ignore[reportIncompatibleVariableOverride]


class ConflictError(APIStatusError):
    status_code: Literal[409] = 409  # pyright: ignore[reportIncompatibleVariableOverride]


class UnprocessableEntityError(APIStatusError):
    status_code: Literal[422] = 422  # pyright: ignore[reportIncompatibleVariableOverride]


class RateLimitError(APIStatusError):
    status_code: Literal[429] = 429  # pyright: ignore[reportIncompatibleVariableOverride]


class APITimeoutError(APIConnectionError):
    def __init__(self, request: httpx.Request | None) -> None:
        super().__init__(message="Request timed out.", request=request)


class StorageNotInitializedError(RuntimeError):
    """Raised when storage operations are attempted before initialization."""

    def __init__(self, storage_type: str = "Storage"):
        super().__init__(
            f"{storage_type} not initialized. Please ensure proper initialization:\n"
            f"\n"
            f"  rag = LightRAG(...)\n"
            f"  await rag.initialize_storages()  # Required - auto-initializes pipeline_status\n"
            f"\n"
            f"See: https://github.com/HKUDS/LightRAG#important-initialization-requirements"
        )


class PipelineNotInitializedError(KeyError):
    """Raised when pipeline status is accessed before initialization."""

    def __init__(self, namespace: str = ""):
        msg = (
            f"Pipeline namespace '{namespace}' not found.\n"
            f"\n"
            f"Pipeline status should be auto-initialized by initialize_storages().\n"
            f"If you see this error, please ensure:\n"
            f"\n"
            f"  1. You called await rag.initialize_storages()\n"
            f"  2. For multi-workspace setups, each LightRAG instance was properly initialized\n"
            f"\n"
            f"Standard initialization:\n"
            f"  rag = LightRAG(workspace='your_workspace')\n"
            f"  await rag.initialize_storages()  # Auto-initializes pipeline_status\n"
            f"\n"
            f"If you need manual control (advanced):\n"
            f"  from lightrag.kg.shared_storage import initialize_pipeline_status\n"
            f"  await initialize_pipeline_status(workspace='your_workspace')"
        )
        super().__init__(msg)


class PipelineCancelledException(Exception):
    """Raised when pipeline processing is cancelled by user request."""

    def __init__(self, message: str = "User cancelled"):
        super().__init__(message)
        self.message = message


class DocumentCancelledException(PipelineCancelledException):
    """
    Raised inside the per-document processing path when an operator flips
    ``doc_status.metadata.cancel_requested`` on a single document that is
    currently being processed.

    Inherits ``PipelineCancelledException`` so the existing user-cancel
    handling in ``process_document`` (write doc_status=FAILED, log a
    "User cancelled" message) catches it automatically. The *difference*
    from its parent is scope: this exception terminates only the one
    document's task and does NOT propagate to the outer ``gather`` that
    would otherwise cancel every sibling doc task.
    """

    def __init__(self, doc_id: str, message: str | None = None):
        self.doc_id = doc_id
        super().__init__(message or f"User cancelled document {doc_id}")


class ChunkTokenLimitExceededError(ValueError):
    """Raised when a chunk exceeds the configured token limit."""

    def __init__(
        self,
        chunk_tokens: int,
        chunk_token_limit: int,
        chunk_preview: str | None = None,
    ) -> None:
        preview = chunk_preview.strip() if chunk_preview else None
        truncated_preview = preview[:80] if preview else None
        preview_note = f" Preview: '{truncated_preview}'" if truncated_preview else ""
        message = (
            f"Chunk token length {chunk_tokens} exceeds chunk_token_size {chunk_token_limit}."
            f"{preview_note}"
        )
        super().__init__(message)
        self.chunk_tokens = chunk_tokens
        self.chunk_token_limit = chunk_token_limit
        self.chunk_preview = truncated_preview


class ContextLengthExceededError(ValueError):
    """Raised when the answer LLM rejects a request for exceeding its context
    token limit even after all conversation history has been dropped.

    The message is user-facing (rendered verbatim in the chat) and tells the
    operator to clear the conversation / narrow retrieval, since trimming
    history further cannot help — the retrieved context + query alone overflow.
    """


class DataMigrationError(Exception):
    """Raised when data migration from legacy collection/table fails."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
