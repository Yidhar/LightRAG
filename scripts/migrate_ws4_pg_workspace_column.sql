-- WS4 / Platform V2 PostgreSQL migration
--
-- Goal:
--   Move legacy workspace-only rows to the combined workspace__default shape used by
--   KB isolation compatibility mode.
--
-- Assumptions:
--   - The default KB id is "default"
--   - The separator is "__"
--   - Legacy workspaces do not already contain a KB suffix
--   - Operators will run this in a maintenance window and verify counts before/after
--   - Vector tables may exist either as legacy base tables or as model-suffixed
--     PGVectorStorage tables such as lightrag_vdb_chunks_<model_suffix>
--
-- Safety:
--   - Idempotent for already-migrated rows because POSITION('__' IN workspace) = 0
--     only matches legacy workspace-only values
--   - Uses one transaction so either all supported tables update together or none do
--   - Uses a transaction-scoped advisory lock to avoid concurrent double-migration
--
-- Optional dry-run / verification ideas before applying:
--   SELECT workspace, COUNT(*) FROM LIGHTRAG_DOC_STATUS GROUP BY workspace ORDER BY workspace;
--   SELECT workspace, COUNT(*) FROM LIGHTRAG_DOC_FULL GROUP BY workspace ORDER BY workspace;
--   SELECT workspace, COUNT(*) FROM LIGHTRAG_DOC_STATUS
--     WHERE POSITION('__' IN workspace) > 0 GROUP BY workspace ORDER BY workspace;
--
-- Note:
--   PostgreSQL LIKE treats "_" as a wildcard, so use POSITION('__' IN workspace) = 0
--   instead of NOT LIKE '%__%' when testing for the literal separator.

BEGIN;
SELECT pg_advisory_xact_lock(2026041601);

UPDATE LIGHTRAG_DOC_FULL
SET workspace = workspace || '__default'
WHERE workspace IS NOT NULL
  AND workspace <> ''
  AND POSITION('__' IN workspace) = 0;

UPDATE LIGHTRAG_DOC_CHUNKS
SET workspace = workspace || '__default'
WHERE workspace IS NOT NULL
  AND workspace <> ''
  AND POSITION('__' IN workspace) = 0;

DO $$
DECLARE
  tbl TEXT;
BEGIN
  FOR tbl IN
    SELECT tablename
    FROM pg_tables
    WHERE schemaname = ANY(current_schemas(false))
      AND (
        tablename = 'lightrag_vdb_chunks'
        OR tablename LIKE 'lightrag_vdb_chunks_%'
        OR tablename = 'lightrag_vdb_entity'
        OR tablename LIKE 'lightrag_vdb_entity_%'
        OR tablename = 'lightrag_vdb_relation'
        OR tablename LIKE 'lightrag_vdb_relation_%'
      )
  LOOP
    EXECUTE format(
      $stmt$
      UPDATE %I
      SET workspace = workspace || '__default'
      WHERE workspace IS NOT NULL
        AND workspace <> ''
        AND POSITION('__' IN workspace) = 0
      $stmt$,
      tbl
    );
  END LOOP;
END $$;

UPDATE LIGHTRAG_LLM_CACHE
SET workspace = workspace || '__default'
WHERE workspace IS NOT NULL
  AND workspace <> ''
  AND POSITION('__' IN workspace) = 0;

UPDATE LIGHTRAG_DOC_STATUS
SET workspace = workspace || '__default'
WHERE workspace IS NOT NULL
  AND workspace <> ''
  AND POSITION('__' IN workspace) = 0;

UPDATE LIGHTRAG_FULL_ENTITIES
SET workspace = workspace || '__default'
WHERE workspace IS NOT NULL
  AND workspace <> ''
  AND POSITION('__' IN workspace) = 0;

UPDATE LIGHTRAG_FULL_RELATIONS
SET workspace = workspace || '__default'
WHERE workspace IS NOT NULL
  AND workspace <> ''
  AND POSITION('__' IN workspace) = 0;

UPDATE LIGHTRAG_ENTITY_CHUNKS
SET workspace = workspace || '__default'
WHERE workspace IS NOT NULL
  AND workspace <> ''
  AND POSITION('__' IN workspace) = 0;

UPDATE LIGHTRAG_RELATION_CHUNKS
SET workspace = workspace || '__default'
WHERE workspace IS NOT NULL
  AND workspace <> ''
  AND POSITION('__' IN workspace) = 0;

COMMIT;

-- Optional post-check:
-- SELECT workspace, COUNT(*) FROM LIGHTRAG_DOC_STATUS GROUP BY workspace ORDER BY workspace;
