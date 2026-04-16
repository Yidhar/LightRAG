"""Tests for entity extraction realtime/batch execution paths."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from lightrag.llm.openai_batch import OpenAIBatchJobResult
from lightrag.utils import Tokenizer, TokenizerInterface


class DummyTokenizer(TokenizerInterface):
    """Simple 1:1 character-to-token mapping for testing."""

    def encode(self, content: str):
        return [ord(ch) for ch in content]

    def decode(self, tokens):
        return "".join(chr(token) for token in tokens)


def _make_global_config(
    max_extract_input_tokens: int = 20480,
    entity_extract_max_gleaning: int = 1,
    **overrides,
) -> dict:
    """Build a minimal global_config dict for extract_entities."""
    tokenizer = Tokenizer("dummy", DummyTokenizer())
    config = {
        "llm_model_func": AsyncMock(return_value=""),
        "entity_extract_max_gleaning": entity_extract_max_gleaning,
        "addon_params": {},
        "tokenizer": tokenizer,
        "max_extract_input_tokens": max_extract_input_tokens,
        "llm_model_max_async": 1,
        "llm_binding": "realtime-test",
        "llm_binding_api_key": None,
        "llm_binding_host": None,
        "llm_model_name": "test-model",
        "llm_openai_options": {},
        "entity_extraction_mode": "auto",
        "entity_extraction_batch_min_chunks": 120,
        "entity_extraction_batch_poll_interval_seconds": 1,
        "entity_extraction_batch_timeout_seconds": 30,
        "entity_extraction_batch_fallback_to_realtime": True,
    }
    config.update(overrides)
    return config


class DummyKVStorage:
    """Minimal async KV storage stub used by entity extraction tests."""

    def __init__(self, records: dict[str, dict] | None = None, *, global_config=None):
        self.records = records or {}
        self.global_config = global_config or {}
        self.upserts: list[dict[str, dict]] = []

    async def get_by_id(self, key: str):
        return self.records.get(key)

    async def upsert(self, data: dict[str, dict]):
        self.upserts.append(data)
        self.records.update(data)


# Minimal valid extraction result that _process_extraction_result can parse
_EXTRACTION_RESULT = (
    "(entity<|#|>TEST_ENTITY<|#|>CONCEPT<|#|>A test entity)<|COMPLETE|>"
)


def _make_chunks(content: str = "Test content.") -> dict[str, dict]:
    return {
        "chunk-001": {
            "tokens": len(content),
            "content": content,
            "full_doc_id": "doc-001",
            "chunk_order_index": 0,
        }
    }


def _make_pipeline_status() -> dict:
    return {
        "latest_message": "",
        "history_messages": [],
        "processed_chunks": 0,
        "total_chunks": 0,
        "current_stage": "",
        "current_stage_label": "",
        "stage_total": 0,
        "stage_processed": 0,
        "stage_remaining": 0,
        "stage_elapsed_seconds": 0,
        "stage_eta_seconds": None,
    }


@pytest.mark.offline
@pytest.mark.asyncio
async def test_gleaning_skipped_when_tokens_exceed_limit():
    """Gleaning should be skipped when estimated tokens exceed max_extract_input_tokens."""
    from lightrag.operate import extract_entities

    # Use a very small token limit so the gleaning context will exceed it
    global_config = _make_global_config(
        max_extract_input_tokens=10,
        entity_extract_max_gleaning=1,
    )

    llm_func = global_config["llm_model_func"]
    llm_func.return_value = _EXTRACTION_RESULT

    with patch("lightrag.operate.logger") as mock_logger:
        await extract_entities(
            chunks=_make_chunks(),
            global_config=global_config,
        )

    # LLM should be called exactly once (initial extraction only, no gleaning)
    assert llm_func.await_count == 1
    # Warning should be logged about skipping gleaning
    mock_logger.warning.assert_called_once()
    warning_msg = mock_logger.warning.call_args[0][0]
    assert "Gleaning stopped" in warning_msg
    assert "exceeded limit" in warning_msg


@pytest.mark.offline
@pytest.mark.asyncio
async def test_gleaning_proceeds_when_tokens_within_limit():
    """Gleaning should proceed when estimated tokens are within max_extract_input_tokens."""
    from lightrag.operate import extract_entities

    # Use a very large token limit so gleaning will proceed
    global_config = _make_global_config(
        max_extract_input_tokens=999999,
        entity_extract_max_gleaning=1,
    )

    llm_func = global_config["llm_model_func"]
    llm_func.return_value = _EXTRACTION_RESULT

    with patch("lightrag.operate.logger"):
        await extract_entities(
            chunks=_make_chunks(),
            global_config=global_config,
        )

    # LLM should be called twice (initial extraction + gleaning)
    assert llm_func.await_count == 2


@pytest.mark.offline
@pytest.mark.asyncio
async def test_no_gleaning_when_max_gleaning_zero():
    """No gleaning when entity_extract_max_gleaning is 0, regardless of token limit."""
    from lightrag.operate import extract_entities

    global_config = _make_global_config(
        max_extract_input_tokens=999999,
        entity_extract_max_gleaning=0,
    )

    llm_func = global_config["llm_model_func"]
    llm_func.return_value = _EXTRACTION_RESULT

    with patch("lightrag.operate.logger"):
        await extract_entities(
            chunks=_make_chunks(),
            global_config=global_config,
        )

    # LLM should be called exactly once (initial extraction only)
    assert llm_func.await_count == 1


@pytest.mark.offline
@pytest.mark.asyncio
async def test_batch_entity_extraction_uses_batch_path_without_realtime_llm():
    """Batch mode should consume batch outputs directly and skip realtime LLM calls."""
    from lightrag.operate import extract_entities

    global_config = _make_global_config(
        entity_extract_max_gleaning=0,
        llm_binding="openai",
        llm_binding_api_key="test-key",
        llm_binding_host="https://dashscope.aliyuncs.com/compatible-mode/v1",
        llm_model_name="qwen-plus",
        entity_extraction_mode="batch",
    )
    llm_func = global_config["llm_model_func"]
    pipeline_status = _make_pipeline_status()
    doc_status_storage = DummyKVStorage(
        {
            "doc-001": {
                "status": "processing",
                "metadata": {"processing_start_time": 1},
            }
        }
    )

    async def fake_run_openai_chat_batch(requests, **kwargs):
        assert len(requests) == 1
        assert requests[0].custom_id == "chunk-001"
        assert requests[0].body["model"] == "qwen-plus"

        on_status = kwargs.get("on_status")
        if on_status is not None:
            await on_status(
                {
                    "batch_id": "batch-001",
                    "status": "completed",
                    "request_counts": {
                        "total": 1,
                        "completed": 1,
                        "failed": 0,
                    },
                }
            )

        return OpenAIBatchJobResult(
            batch_id="batch-001",
            status="completed",
            output_lines=[
                {
                    "custom_id": "chunk-001",
                    "response": {
                        "status_code": 200,
                        "body": {
                            "choices": [
                                {
                                    "message": {
                                        "content": [{"text": _EXTRACTION_RESULT}],
                                    }
                                }
                            ]
                        },
                    },
                }
            ],
        )

    with patch(
        "lightrag.operate.run_openai_chat_batch",
        new=AsyncMock(side_effect=fake_run_openai_chat_batch),
    ):
        results = await extract_entities(
            chunks=_make_chunks(),
            global_config=global_config,
            pipeline_status=pipeline_status,
            pipeline_status_lock=asyncio.Lock(),
            doc_status_storage=doc_status_storage,
        )

    assert llm_func.await_count == 0
    maybe_nodes, maybe_edges = results[0]
    assert "TEST_ENTITY" in maybe_nodes
    assert maybe_edges == {}
    assert pipeline_status["current_stage"] == "entity_extraction_batch"
    assert pipeline_status["processed_chunks"] == 1
    assert pipeline_status["stage_processed"] == 1
    assert pipeline_status["stage_remaining"] == 0
    assert doc_status_storage.records["doc-001"]["metadata"]["processing_stage"] == (
        "entity_extraction_batch"
    )
    assert (
        doc_status_storage.records["doc-001"]["metadata"][
            "processing_items_processed"
        ]
        == 1
    )


@pytest.mark.offline
@pytest.mark.asyncio
async def test_batch_entity_extraction_falls_back_to_realtime_when_batch_fails():
    """Batch failures should fall back to realtime extraction when enabled."""
    from lightrag.operate import extract_entities

    global_config = _make_global_config(
        entity_extract_max_gleaning=0,
        llm_binding="openai",
        llm_binding_api_key="test-key",
        llm_binding_host="https://dashscope.aliyuncs.com/compatible-mode/v1",
        llm_model_name="qwen-plus",
        entity_extraction_mode="batch",
        entity_extraction_batch_fallback_to_realtime=True,
    )
    llm_func = global_config["llm_model_func"]
    llm_func.return_value = _EXTRACTION_RESULT
    pipeline_status = _make_pipeline_status()
    doc_status_storage = DummyKVStorage(
        {
            "doc-001": {
                "status": "processing",
                "metadata": {"processing_start_time": 1},
            }
        }
    )

    with patch(
        "lightrag.operate.run_openai_chat_batch",
        new=AsyncMock(side_effect=RuntimeError("batch transport failed")),
    ):
        results = await extract_entities(
            chunks=_make_chunks(),
            global_config=global_config,
            pipeline_status=pipeline_status,
            pipeline_status_lock=asyncio.Lock(),
            doc_status_storage=doc_status_storage,
        )

    assert llm_func.await_count == 1
    maybe_nodes, _maybe_edges = results[0]
    assert "TEST_ENTITY" in maybe_nodes
    assert pipeline_status["current_stage"] == "entity_extraction_fallback"
    assert pipeline_status["processed_chunks"] == 1
    assert any(
        "Batch entity extraction failed, falling back to realtime"
        in message
        for message in pipeline_status["history_messages"]
    )
    assert doc_status_storage.records["doc-001"]["metadata"]["processing_stage"] == (
        "entity_extraction_fallback"
    )
    assert (
        doc_status_storage.records["doc-001"]["metadata"]["processing_eta_seconds"] == 0
    )
