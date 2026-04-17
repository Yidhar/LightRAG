"""
Offline tests for the Phase D federation helpers.

The async ``federated_aquery_llm`` entry point needs a full FastAPI
request + rag_factory, which is awkward in unit scope. These tests
cover the pure helpers — KB enumeration and response merging — which
carry the logic most likely to regress.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from lightrag.api.federation import (
    collect_workspace_kb_ids,
    _format_federated_response,
)

pytestmark = pytest.mark.offline


# ---------------------------------------------------------------------------
# collect_workspace_kb_ids
# ---------------------------------------------------------------------------


def _state_with_registry(registry) -> SimpleNamespace:
    return SimpleNamespace(kb_registry=registry)


def test_collect_returns_empty_when_registry_missing():
    state = SimpleNamespace(kb_registry=None)
    assert collect_workspace_kb_ids(state, "alpha") == []


def test_collect_returns_empty_when_registry_has_no_list_method():
    state = _state_with_registry(SimpleNamespace())
    assert collect_workspace_kb_ids(state, "alpha") == []


def test_collect_deduplicates_and_filters_by_workspace():
    class DummyRegistry:
        def list(self, workspace_id):
            return [
                {"workspace_id": "alpha", "kb_id": "docs"},
                {"workspace_id": "alpha", "id": "graph"},
                {"workspace_id": "alpha", "kb_id": "docs"},  # duplicate
                {"workspace_id": "beta", "kb_id": "leak"},  # wrong workspace
            ]

    state = _state_with_registry(DummyRegistry())
    assert collect_workspace_kb_ids(state, "alpha") == ["docs", "graph"]


def test_collect_handles_dataclass_entries():
    @dataclass
    class Entry:
        workspace_id: str
        kb_id: str

    class DummyRegistry:
        def list(self, workspace_id):
            return [Entry("alpha", "docs"), Entry("alpha", "graph")]

    state = _state_with_registry(DummyRegistry())
    assert collect_workspace_kb_ids(state, "alpha") == ["docs", "graph"]


def test_collect_tolerates_legacy_no_arg_lister():
    class DummyRegistry:
        def list(self):
            return [{"workspace_id": "alpha", "kb_id": "only"}]

    state = _state_with_registry(DummyRegistry())
    assert collect_workspace_kb_ids(state, "alpha") == ["only"]


# ---------------------------------------------------------------------------
# _format_federated_response
# ---------------------------------------------------------------------------


def test_format_combines_responses_with_kb_headers():
    merged = _format_federated_response(
        [
            (
                "docs",
                {
                    "llm_response": {"content": "hello from docs"},
                    "data": {"references": [], "chunks": []},
                },
            ),
            (
                "graph",
                {
                    "llm_response": {"content": "hello from graph"},
                    "data": {"references": [], "chunks": []},
                },
            ),
        ]
    )

    assert "### Knowledge base: docs" in merged["llm_response"]["content"]
    assert "hello from docs" in merged["llm_response"]["content"]
    assert "### Knowledge base: graph" in merged["llm_response"]["content"]
    assert "hello from graph" in merged["llm_response"]["content"]

    federation = merged["data"]["federation"]
    assert federation == {"kb_ids": ["docs", "graph"], "kb_count": 2}


def test_format_namespaces_reference_and_chunk_ids():
    merged = _format_federated_response(
        [
            (
                "docs",
                {
                    "llm_response": {"content": "a"},
                    "data": {
                        "references": [{"reference_id": "1", "file_path": "/a.pdf"}],
                        "chunks": [{"reference_id": "1", "content": "x"}],
                    },
                },
            ),
            (
                "graph",
                {
                    "llm_response": {"content": "b"},
                    "data": {
                        "references": [{"reference_id": "1", "file_path": "/b.pdf"}],
                        "chunks": [{"reference_id": "1", "content": "y"}],
                    },
                },
            ),
        ]
    )

    ref_ids = {ref["reference_id"] for ref in merged["data"]["references"]}
    assert ref_ids == {"docs:1", "graph:1"}

    for ref in merged["data"]["references"]:
        assert ref["knowledge_base_id"] in {"docs", "graph"}

    chunk_ids = {chunk["reference_id"] for chunk in merged["data"]["chunks"]}
    assert chunk_ids == {"docs:1", "graph:1"}


def test_format_empty_content_falls_back_to_placeholder():
    merged = _format_federated_response(
        [
            (
                "docs",
                {
                    "llm_response": {"content": ""},
                    "data": {"references": [], "chunks": []},
                },
            ),
        ]
    )

    assert merged["llm_response"]["content"].startswith("No relevant context")
    assert merged["data"]["federation"]["kb_count"] == 1
