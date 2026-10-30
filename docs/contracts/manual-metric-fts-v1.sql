-- Optional derived BM25 index; create only after the packaged SQLite passes
-- an FTS5/trigram capability check. It is rebuilt from manual_metric_version,
-- never treated as the source of truth or as an authorization catalog.
CREATE VIRTUAL TABLE IF NOT EXISTS manual_metric_fts USING fts5(
    metric_id UNINDEXED,
    version_id UNINDEXED,
    route_status UNINDEXED,
    name,
    aliases,
    description,
    tokenize = 'trigram'
);

-- In one service transaction, delete/reinsert search rows whenever a metric is
-- created, edited, published, or soft-deleted. Index only active published
-- versions and the newest draft of each non-deleted metric. Startup may drop
-- and rebuild this table from the canonical metric catalog if hashes differ.
-- Query with a bounded, application-generated, parameter-bound FTS expression:
-- SELECT metric_id, version_id, route_status,
--        bm25(manual_metric_fts, 0.0, 0.0, 0.0, 8.0, 4.0, 1.0) AS rank
-- FROM manual_metric_fts
-- WHERE manual_metric_fts MATCH :safe_fts_expression
-- ORDER BY rank ASC LIMIT :candidate_limit;
-- SQLite FTS5 bm25() ranks smaller (often negative) values first. Short terms
-- below three Unicode characters need exact-name/alias lookup outside FTS5.

-- Versioned schema-KB retrieval index. Rows are derived only from published
-- kb_entity/kb_field/kb_term/kb_query_capability/kb_query_example metadata.
-- Keep relation approval and field permissions in the canonical KB tables.
CREATE VIRTUAL TABLE IF NOT EXISTS kb_search_fts USING fts5(
    release_id UNINDEXED,
    target_kind UNINDEXED,
    entity_id UNINDEXED,
    target_id UNINDEXED,
    title,
    aliases,
    description,
    tokenize = 'trigram'
);

-- SELECT target_kind, entity_id, target_id,
--        bm25(kb_search_fts, 0.0, 0.0, 0.0, 0.0, 8.0, 4.0, 1.0) AS rank
-- FROM kb_search_fts
-- WHERE kb_search_fts MATCH :safe_fts_expression
--   AND release_id = :pinned_release_id
-- ORDER BY rank ASC LIMIT :candidate_limit;
-- Build/rebuild this index when a KB release is published or the index
-- implementation changes. Search hits are candidate IDs, not permissions.
