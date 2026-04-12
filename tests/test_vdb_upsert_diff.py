from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lightrag.constants import GRAPH_FIELD_SEP
from lightrag.operate import _filter_unchanged_vdb_upsert_batch


@pytest.mark.offline
@pytest.mark.asyncio
async def test_filter_unchanged_vdb_upsert_batch_skips_same_entity_payload():
    storage = SimpleNamespace(
        meta_fields={"entity_name", "source_id", "content", "file_path"},
        get_by_ids=AsyncMock(
            return_value=[
                {
                    "id": "ent-1",
                    "entity_name": "Pump",
                    "content": "Pump\nBasement circulation pump",
                    "source_id": f"chunk-2{GRAPH_FIELD_SEP}chunk-1",
                    "file_path": f"b.pdf{GRAPH_FIELD_SEP}a.pdf",
                },
                None,
            ]
        ),
    )
    payload = {
        "ent-1": {
            "entity_name": "Pump",
            "content": "Pump\nBasement circulation pump",
            "source_id": f"chunk-1{GRAPH_FIELD_SEP}chunk-2",
            "file_path": f"a.pdf{GRAPH_FIELD_SEP}b.pdf",
            "description": "not persisted in vector store",
        },
        "ent-2": {
            "entity_name": "Valve",
            "content": "Valve\nBasement discharge valve",
            "source_id": "chunk-3",
            "file_path": "c.pdf",
        },
    }

    filtered, stats = await _filter_unchanged_vdb_upsert_batch(
        storage,
        payload,
        label="entity_vdb",
        min_probe_size=1,
    )

    assert filtered == {"ent-2": payload["ent-2"]}
    assert stats == {"total": 2, "kept": 1, "skipped": 1}


@pytest.mark.offline
@pytest.mark.asyncio
async def test_filter_unchanged_vdb_upsert_batch_keeps_changed_relation_payload():
    storage = SimpleNamespace(
        meta_fields={"src_id", "tgt_id", "source_id", "content", "file_path"},
        get_by_ids=AsyncMock(
            return_value=[
                {
                    "id": "rel-1",
                    "src_id": "Pump",
                    "tgt_id": "Valve",
                    "content": "pressure\tPump\nValve\nold description",
                    "source_id": "chunk-1",
                    "file_path": "drawing.pdf",
                }
            ]
        ),
    )
    payload = {
        "rel-1": {
            "src_id": "Pump",
            "tgt_id": "Valve",
            "content": "pressure\tPump\nValve\nnew description",
            "source_id": "chunk-1",
            "file_path": "drawing.pdf",
        }
    }

    filtered, stats = await _filter_unchanged_vdb_upsert_batch(
        storage,
        payload,
        label="relationships_vdb",
        min_probe_size=1,
    )

    assert filtered == payload
    assert stats == {"total": 1, "kept": 1, "skipped": 0}
