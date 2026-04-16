from pathlib import Path

import pytest

pytestmark = pytest.mark.offline


def test_ws4_pg_workspace_migration_sql_covers_static_tables_and_safe_guard():
    sql_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "migrate_ws4_pg_workspace_column.sql"
    )
    sql = sql_path.read_text(encoding="utf-8")

    expected_tables = [
        "LIGHTRAG_DOC_FULL",
        "LIGHTRAG_DOC_CHUNKS",
        "LIGHTRAG_LLM_CACHE",
        "LIGHTRAG_DOC_STATUS",
        "LIGHTRAG_FULL_ENTITIES",
        "LIGHTRAG_FULL_RELATIONS",
        "LIGHTRAG_ENTITY_CHUNKS",
        "LIGHTRAG_RELATION_CHUNKS",
    ]

    for table_name in expected_tables:
        assert f"UPDATE {table_name}" in sql

    assert "workspace || '__default'" in sql
    assert "POSITION('__' IN workspace) = 0" in sql
    assert "workspace NOT LIKE '%__%'" not in sql
    assert "pg_advisory_xact_lock" in sql
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql


def test_ws4_pg_workspace_migration_sql_discovers_vector_tables_dynamically():
    sql_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "migrate_ws4_pg_workspace_column.sql"
    )
    sql = sql_path.read_text(encoding="utf-8")

    assert "DO $$" in sql
    assert "FROM pg_tables" in sql
    assert "tablename = 'lightrag_vdb_chunks'" in sql
    assert "tablename LIKE 'lightrag_vdb_chunks_%'" in sql
    assert "tablename = 'lightrag_vdb_entity'" in sql
    assert "tablename LIKE 'lightrag_vdb_entity_%'" in sql
    assert "tablename = 'lightrag_vdb_relation'" in sql
    assert "tablename LIKE 'lightrag_vdb_relation_%'" in sql
    assert "EXECUTE format(" in sql
