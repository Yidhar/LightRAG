"""Regression tests for multimodal image retrieval across KG query modes."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lightrag.base import QueryParam


def _make_text_chunks_db():
    mock = MagicMock()
    mock.embedding_func = None
    mock.global_config = {"kg_chunk_pick_method": "VECTOR"}
    return mock


def _make_image_vdb():
    mock = AsyncMock()
    mock.query = AsyncMock(
        return_value=[
            {
                "blob_id": "img-1",
                "caption": "A line chart",
                "file_path": "reports/sample.pdf",
                "blob_ref": "C:/tmp/img-1.png",
                "distance": 0.01,
                "created_at": 1234567890,
            }
        ]
    )
    mock.cosine_better_than_threshold = 0.2
    return mock


def _make_image_metadata():
    mock = AsyncMock()
    mock.get_by_ids = AsyncMock(
        return_value=[
            {
                "annotation_text": "Annotated chart showing quarterly revenue growth.",
                "source_doc_id": "doc-1",
                "source_page": 66,
                "source_printed_page": 61,
                "source_page_label": "61",
                "source_bbox": {"x0": 10, "y0": 20, "width": 100, "height": 80},
                "picture_index": 0,
                "page_picture_index": 0,
                "native_xref": 654,
                "merged_extraction_modes": ["pymupdf_native_image"],
                "context_text": "[当前页 p.66/页码61]\nRevenue in Q1/Q2/Q3 is highlighted.",
            }
        ]
    )
    return mock


@pytest.mark.offline
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "ll_keywords", "hl_keywords"),
    [
        ("local", "revenue chart", ""),
        ("global", "", "revenue chart"),
    ],
)
async def test_kg_modes_append_image_chunks(mode, ll_keywords, hl_keywords):
    """Local/global KG modes should still return multimodal image chunks."""
    from lightrag.operate import _perform_kg_search

    with (
        patch("lightrag.operate._get_node_data", AsyncMock(return_value=([], []))),
        patch("lightrag.operate._get_edge_data", AsyncMock(return_value=([], []))),
    ):
        result = await _perform_kg_search(
            query="What does the revenue chart show?",
            ll_keywords=ll_keywords,
            hl_keywords=hl_keywords,
            knowledge_graph_inst=AsyncMock(),
            entities_vdb=AsyncMock(),
            relationships_vdb=AsyncMock(),
            text_chunks_db=_make_text_chunks_db(),
            query_param=QueryParam(mode=mode, top_k=5),
            images_vdb=_make_image_vdb(),
            image_metadata=_make_image_metadata(),
        )

    assert result["vector_chunks"], "Expected multimodal image chunks to be returned"
    image_chunk = result["vector_chunks"][0]
    assert image_chunk["source_type"] == "image_vector"
    assert image_chunk["image_blob_id"] == "img-1"
    assert "Annotated chart showing quarterly revenue growth." in image_chunk["content"]
    assert "PDF物理第 66 页" in image_chunk["content"]
    assert "文档页码 61" in image_chunk["content"]
    assert "【图像邻近上下文】" in image_chunk["content"]
    assert image_chunk["source_page"] == 66
    assert image_chunk["source_printed_page"] == 61
    assert image_chunk["source_page_label"] == "61"
    assert image_chunk["native_xref"] == 654
    assert result["chunk_tracking"]["img-1"]["source"] == "I"


@pytest.mark.offline
@pytest.mark.asyncio
async def test_build_query_context_allows_chunk_only_image_results_for_global_mode():
    """Chunk-only multimodal hits should not be discarded outside mix mode."""
    from lightrag.operate import _build_query_context

    fake_search_result = {
        "final_entities": [],
        "final_relations": [],
        "vector_chunks": [
            {
                "chunk_id": "img-1",
                "content": "Annotated chart showing quarterly revenue growth.",
                "file_path": "reports/sample.pdf",
                "source_type": "image_vector",
                "image_blob_id": "img-1",
            }
        ],
        "chunk_tracking": {"img-1": {"source": "I", "frequency": 1, "order": 1}},
        "query_embedding": None,
    }
    fake_truncation_result = {
        "entities_context": [],
        "relations_context": [],
        "filtered_entities": [],
        "filtered_relations": [],
        "entity_id_to_original": {},
        "relation_id_to_original": {},
    }
    fake_raw_data = {
        "status": "success",
        "message": "Query processed successfully",
        "data": {
            "entities": [],
            "relationships": [],
            "chunks": [
                {
                    "chunk_id": "img-1",
                    "content": "Annotated chart showing quarterly revenue growth.",
                    "file_path": "reports/sample.pdf",
                    "source_type": "image_vector",
                    "image_blob_id": "img-1",
                }
            ],
            "references": [],
        },
    }

    text_chunks_db = MagicMock()
    text_chunks_db.global_config = {}

    with (
        patch(
            "lightrag.operate._perform_kg_search",
            AsyncMock(return_value=fake_search_result),
        ),
        patch(
            "lightrag.operate._apply_token_truncation",
            AsyncMock(return_value=fake_truncation_result),
        ),
        patch(
            "lightrag.operate._merge_all_chunks",
            AsyncMock(return_value=fake_search_result["vector_chunks"]),
        ),
        patch(
            "lightrag.operate._build_context_str",
            AsyncMock(return_value=("image context", fake_raw_data)),
        ),
    ):
        result = await _build_query_context(
            query="What does the revenue chart show?",
            ll_keywords="",
            hl_keywords="revenue chart",
            knowledge_graph_inst=AsyncMock(),
            entities_vdb=AsyncMock(),
            relationships_vdb=AsyncMock(),
            text_chunks_db=text_chunks_db,
            query_param=QueryParam(mode="global", top_k=5),
            images_vdb=AsyncMock(),
            image_metadata=AsyncMock(),
        )

    assert result is not None
    assert result.context == "image context"
    assert result.raw_data["data"]["chunks"][0]["image_blob_id"] == "img-1"
