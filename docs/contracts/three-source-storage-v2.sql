-- Additive SQLite schema draft for three fixed surgery-data exports.
-- Design artifact only: this file is not an application migration.
-- Source columns follow seven workbooks in templet/; business rules still need approval.
-- SQLite does not persist SQL comments as catalog metadata. Import the versioned
-- query-knowledge catalog when runtime field descriptions are required.

PRAGMA foreign_keys = ON;

-- One isolated data namespace per application conversation.
-- The application checks that session_id exists in its existing session object store.
CREATE TABLE data_space (
    session_id TEXT PRIMARY KEY, -- Application conversation ID; never supplied by query text.
    created_at TEXT NOT NULL -- Namespace creation timestamp, set by the application.
);

-- One logical imported workbook. A physical blob can be shared across sessions,
-- but this row and all derived rows belong to exactly one session.
CREATE TABLE data_source (
    id TEXT PRIMARY KEY, -- Stable logical source ID used by revision manifests.
    session_id TEXT NOT NULL REFERENCES data_space(session_id), -- Owning conversation.
    source_type TEXT NOT NULL CHECK (source_type IN
        ('FIRST_SURGERY', 'CANCELLATION', 'SURGERY_DETAIL')), -- Fixed export family.
    file_sha256 TEXT NOT NULL CHECK (length(file_sha256) = 64), -- Hash of original workbook blob.
    source_fingerprint TEXT CHECK (source_fingerprint IS NULL OR length(source_fingerprint) = 64), -- Final hash of source bytes/name, layout, parser, ingest skill, and unit decision; NULL until resolution.
    file_name TEXT NOT NULL, -- Original display name; used for cancellation-unit matching, never source-type matching.
    layout_version TEXT NOT NULL, -- Exact fixed-format contract used to parse the file.
    layout_sha256 TEXT NOT NULL CHECK (length(layout_sha256) = 64), -- Hash of the exact ordered layout contract accepted at import.
    parser_version TEXT NOT NULL, -- Parser implementation version for reproducibility.
    ingest_skill_id TEXT NOT NULL, -- Trusted type-specific ingestion adapter ID, never read from workbook contents.
    ingest_skill_version TEXT NOT NULL, -- Adapter release used for validation and row mapping.
    ingest_skill_sha256 TEXT NOT NULL CHECK (length(ingest_skill_sha256) = 64), -- Hash of the registered adapter contract and policy.
    state TEXT NOT NULL CHECK (state IN ('STAGING', 'NEEDS_UNIT_CONFIRMATION', 'READY', 'FAILED')), -- Uncertain cancellation unit awaits manual selection.
    row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0), -- Accepted source data rows across sheets.
    origin_operating_unit TEXT, -- Resolved source-level unit; cancellation export lacks this column.
    origin_unit_basis TEXT CHECK (origin_unit_basis IN
        ('WORKBOOK_COLUMN', 'FILENAME_SEMANTIC', 'USER_CONFIRMED')), -- How the unit was resolved.
    unit_routing_policy_sha256 TEXT CHECK (unit_routing_policy_sha256 IS NULL OR length(unit_routing_policy_sha256) = 64), -- Cancellation filename-matching policy hash; NULL for other sources.
    origin_unit_evidence_json TEXT, -- Matcher/alias version, filename evidence, candidates, conflicts, optional embedding scores, and manual audit.
    observed_start_date TEXT, -- Minimum valid row date, ISO YYYY-MM-DD; not a completeness claim.
    observed_end_date TEXT, -- Maximum valid row date, ISO YYYY-MM-DD; not a completeness claim.
    declared_start_date TEXT, -- Explicit export-period start if independently supplied.
    declared_end_date TEXT, -- Explicit export-period end if independently supplied.
    imported_at TEXT NOT NULL, -- Import creation timestamp.
    published_at TEXT, -- Timestamp when source became READY.
    CHECK ((origin_operating_unit IS NULL) = (origin_unit_basis IS NULL)),
    CHECK (state <> 'NEEDS_UNIT_CONFIRMATION' OR
           (source_type = 'CANCELLATION' AND origin_operating_unit IS NULL AND
            origin_unit_evidence_json IS NOT NULL)),
    CHECK (state <> 'READY' OR (source_fingerprint IS NOT NULL AND published_at IS NOT NULL)),
    CHECK (source_type <> 'CANCELLATION' OR state <> 'READY' OR
           (origin_operating_unit IS NOT NULL AND
            origin_operating_unit IN ('OR_1', 'OR_2', 'OR_3') AND
            origin_unit_basis IS NOT NULL AND
            origin_unit_basis IN ('FILENAME_SEMANTIC', 'USER_CONFIRMED') AND
            unit_routing_policy_sha256 IS NOT NULL AND
            origin_unit_evidence_json IS NOT NULL)),
    UNIQUE (id, session_id)
);

-- Fingerprint includes file hash/name, layout hash, parser and skill versions,
-- unit, basis, and routing-policy hash.
-- A corrected dimension creates a new logical source for a later revision.
CREATE UNIQUE INDEX uq_data_source_ready_fingerprint
    ON data_source(session_id, source_fingerprint) WHERE state = 'READY';
CREATE INDEX ix_data_source_session_type
    ON data_source(session_id, source_type, state);

-- A parsed worksheet within one workbook. The sheet and Excel row locate raw evidence.
CREATE TABLE data_sheet (
    id TEXT PRIMARY KEY, -- Stable sheet ID referenced by business rows.
    source_id TEXT NOT NULL REFERENCES data_source(id), -- Owning workbook source.
    sheet_name TEXT NOT NULL, -- Original worksheet name.
    header_row INTEGER NOT NULL CHECK (header_row > 0), -- One-based Excel row containing exact headers.
    header_sha256 TEXT NOT NULL CHECK (length(header_sha256) = 64), -- Hash of normalized ordered headers.
    row_count INTEGER NOT NULL CHECK (row_count >= 0), -- Parsed data-row count in this sheet.
    UNIQUE (source_id, sheet_name),
    UNIQUE (id, source_id)
);

-- Immutable snapshot of the source set visible to one conversation at a point in time.
-- Its rows are not copied: membership is stored in data_revision_source.
CREATE TABLE data_revision (
    id TEXT PRIMARY KEY, -- Snapshot ID pinned to a query or report.
    session_id TEXT NOT NULL REFERENCES data_space(session_id), -- Owning conversation.
    revision_no INTEGER NOT NULL CHECK (revision_no > 0), -- Monotone number within that conversation.
    parent_id TEXT, -- Previous revision in the same conversation; NULL for first revision.
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64), -- Canonical hash of ordered source membership and provenance.
    created_at TEXT NOT NULL, -- Snapshot creation timestamp.
    UNIQUE (session_id, revision_no),
    UNIQUE (id, session_id),
    FOREIGN KEY (parent_id, session_id) REFERENCES data_revision(id, session_id)
);

-- Membership of a revision. Composite foreign keys prevent cross-session inclusion.
CREATE TABLE data_revision_source (
    revision_id TEXT NOT NULL, -- Version receiving the source.
    session_id TEXT NOT NULL, -- Shared session scope validated against both parents.
    source_id TEXT NOT NULL, -- READY source included in this version.
    PRIMARY KEY (revision_id, source_id),
    FOREIGN KEY (revision_id, session_id)
        REFERENCES data_revision(id, session_id),
    FOREIGN KEY (source_id, session_id)
        REFERENCES data_source(id, session_id)
);

CREATE INDEX ix_data_revision_source_source
    ON data_revision_source(source_id, revision_id);

CREATE TRIGGER data_revision_source_ready
BEFORE INSERT ON data_revision_source
WHEN (SELECT state FROM data_source WHERE id = NEW.source_id) <> 'READY'
BEGIN
    SELECT RAISE(ABORT, 'source is not ready');
END;

-- Renaming the same workbook does not permit its rows to be counted twice in
-- one revision. A later revision may replace the old source with a correction.
CREATE TRIGGER data_revision_source_unique_file
BEFORE INSERT ON data_revision_source
WHEN EXISTS (
    SELECT 1 FROM data_revision_source AS existing
    JOIN data_source AS prior ON prior.id = existing.source_id
    JOIN data_source AS incoming ON incoming.id = NEW.source_id
    WHERE existing.revision_id = NEW.revision_id
      AND prior.file_sha256 = incoming.file_sha256
)
BEGIN
    SELECT RAISE(ABORT, 'duplicate workbook in revision');
END;

-- One row from the daily first-surgery export, not necessarily one performed case.
-- All source fields are nullable because import issues are retained separately.
-- Inpatient and patient IDs are not operation IDs and are deliberately not UNIQUE.
CREATE TABLE first_surgery_row (
    id INTEGER PRIMARY KEY, -- Internal row key; no clinical meaning.
    sheet_id TEXT NOT NULL REFERENCES data_sheet(id), -- Source worksheet.
    excel_row INTEGER NOT NULL CHECK (excel_row > 0), -- One-based row in original workbook.
    operation_date TEXT, -- Source "surgery date", ISO YYYY-MM-DD after parsing.
    operating_unit TEXT, -- Source operating-unit area ("surgery suite").
    operating_room TEXT, -- Specific operating room within the unit.
    inpatient_no TEXT, -- Inpatient number; preserve leading zeroes; not a surgery key.
    patient_id TEXT, -- Source patient ID; sensitive and not a cross-table surgery key.
    patient_name TEXT, -- Source patient name; sensitive.
    surgical_department TEXT, -- Source surgical department.
    surgeon TEXT, -- Source operating surgeon; sensitive staff name.
    room_entry_hhmm TEXT, -- Original room-entry HH:MM without date or timezone.
    room_entry_minute INTEGER CHECK (room_entry_minute BETWEEN 0 AND 1439), -- Parsed minute after midnight; NULL if blank/invalid.
    anesthesia_start_hhmm TEXT, -- Original anesthesia-start HH:MM without date.
    anesthesia_start_minute INTEGER CHECK (anesthesia_start_minute BETWEEN 0 AND 1439), -- Parsed minute after midnight.
    operation_start_hhmm TEXT, -- Original surgery-start HH:MM without date.
    operation_start_minute INTEGER CHECK (operation_start_minute BETWEEN 0 AND 1439), -- Parsed minute after midnight.
    anesthetist TEXT, -- Source primary anesthetist; sensitive staff name.
    operation_name TEXT, -- Source operation name, not a deduplication key.
    preop_diagnosis TEXT, -- Source preoperative diagnosis; sensitive free text.
    row_sha256 TEXT NOT NULL CHECK (length(row_sha256) = 64), -- Hash of canonical original row content.
    UNIQUE (sheet_id, excel_row)
);

-- One row from a cancellation export. It is not proven to be a unique operation
-- or a unique cancellation event; no operating-unit column exists in this export.
CREATE TABLE cancellation_row (
    id INTEGER PRIMARY KEY, -- Internal row key; no clinical meaning.
    sheet_id TEXT NOT NULL REFERENCES data_sheet(id), -- Source worksheet.
    excel_row INTEGER NOT NULL CHECK (excel_row > 0), -- One-based row in original workbook.
    sequence_no INTEGER, -- Export sequence number; not globally or clinically unique.
    record_date TEXT, -- Source "date" column, ISO YYYY-MM-DD; distinct from cancellation timestamp.
    inpatient_no TEXT, -- Inpatient number; sensitive, not a surgery key.
    patient_name TEXT, -- Source patient name; sensitive.
    operation_category TEXT, -- Source surgery category, not execution status.
    operating_room TEXT, -- Specific room; source has no operating-unit area field.
    case_order INTEGER, -- Source case order within schedule; not a unique key.
    surgeon_department TEXT, -- Source surgeon department.
    surgeon TEXT, -- Source operating surgeon; sensitive staff name.
    anesthetist TEXT, -- Source anesthesiologist; sensitive staff name.
    scrub_nurse TEXT, -- Source scrub nurse; sensitive staff name.
    circulating_nurse TEXT, -- Source circulating nurse; sensitive staff name.
    cancelled_local TEXT, -- Local ISO datetime without timezone; not interchangeable with record_date.
    cancelled_by TEXT, -- Source cancelling person; sensitive staff name.
    cancellation_categories_raw TEXT, -- Original possibly comma-separated category cell.
    cancellation_reason TEXT, -- Source free-text reason; potentially sensitive.
    row_sha256 TEXT NOT NULL CHECK (length(row_sha256) = 64), -- Hash of canonical original row content.
    UNIQUE (sheet_id, excel_row)
);

-- Normalized labels from cancellation_categories_raw. Preserve raw cell on parent;
-- duplicate labels in one row are stored once, and separate labels may overlap.
CREATE TABLE cancellation_tag (
    cancellation_row_id INTEGER NOT NULL REFERENCES cancellation_row(id), -- Parent source row.
    position INTEGER NOT NULL CHECK (position > 0), -- Original one-based label order.
    tag TEXT NOT NULL, -- Trimmed category label; not a controlled enum until approved.
    PRIMARY KEY (cancellation_row_id, position)
);

CREATE INDEX ix_cancellation_tag_tag ON cancellation_tag(tag, cancellation_row_id);
CREATE UNIQUE INDEX uq_cancellation_row_tag
    ON cancellation_tag(cancellation_row_id, tag);

-- One source detail row, not automatically one performed operation. The export
-- has no shared operation ID or explicit performed-status column.
CREATE TABLE surgery_detail_row (
    id INTEGER PRIMARY KEY, -- Internal row key; no clinical meaning.
    sheet_id TEXT NOT NULL REFERENCES data_sheet(id), -- Source worksheet.
    excel_row INTEGER NOT NULL CHECK (excel_row > 0), -- One-based row in original workbook.
    operation_date TEXT, -- Source surgery date, ISO YYYY-MM-DD.
    operating_unit TEXT, -- Source operating-unit area.
    operating_room TEXT, -- Source specific operating room.
    inpatient_no TEXT, -- Inpatient number; sensitive and not a surgery key.
    patient_name TEXT, -- Source patient name; sensitive.
    sex TEXT, -- Source patient sex text; no inferred enum policy.
    age_raw TEXT, -- Original age cell as text, including source formatting.
    age_years INTEGER CHECK (age_years >= 0), -- Parsed years only when age unit/format is unambiguous.
    clinical_department TEXT, -- Source clinical department.
    operation_name TEXT, -- Source operation name, not a deduplication key.
    anesthesia_method TEXT, -- Source anesthesia method.
    pacu_entry_hhmm TEXT, -- Original PACU-entry HH:MM without date.
    pacu_entry_minute INTEGER CHECK (pacu_entry_minute BETWEEN 0 AND 1439), -- Parsed minute after midnight.
    pacu_exit_hhmm TEXT, -- Original PACU-exit HH:MM without date.
    pacu_exit_minute INTEGER CHECK (pacu_exit_minute BETWEEN 0 AND 1439), -- Parsed minute after midnight.
    operation_type TEXT, -- Source operation type; not performed status.
    operation_level TEXT, -- Source operation level; values need business review.
    incision_grade TEXT, -- Source incision grade; values need business review.
    room_entry_hhmm TEXT, -- Original room-entry HH:MM without date.
    room_entry_minute INTEGER CHECK (room_entry_minute BETWEEN 0 AND 1439), -- Parsed minute after midnight.
    anesthesia_start_hhmm TEXT, -- Original anesthesia-start HH:MM without date.
    anesthesia_start_minute INTEGER CHECK (anesthesia_start_minute BETWEEN 0 AND 1439), -- Parsed minute after midnight.
    operation_start_hhmm TEXT, -- Original surgery-start HH:MM without date.
    operation_start_minute INTEGER CHECK (operation_start_minute BETWEEN 0 AND 1439), -- Parsed minute after midnight.
    operation_end_hhmm TEXT, -- Original surgery-end HH:MM without date.
    operation_end_minute INTEGER CHECK (operation_end_minute BETWEEN 0 AND 1439), -- Parsed minute after midnight.
    anesthesia_end_hhmm TEXT, -- Original anesthesia-end HH:MM without date.
    anesthesia_end_minute INTEGER CHECK (anesthesia_end_minute BETWEEN 0 AND 1439), -- Parsed minute after midnight.
    room_exit_hhmm TEXT, -- Original room-exit HH:MM without date.
    room_exit_minute INTEGER CHECK (room_exit_minute BETWEEN 0 AND 1439), -- Parsed minute after midnight.
    preop_diagnosis TEXT, -- Source preoperative diagnosis; sensitive free text.
    surgeon TEXT, -- Source operating surgeon; sensitive staff name.
    assistant_1 TEXT, -- Source first assistant; sensitive staff name.
    assistant_2 TEXT, -- Source second assistant; sensitive staff name.
    assistant_3 TEXT, -- Source third assistant; sensitive staff name.
    anesthetist TEXT, -- Source primary anesthetist; sensitive staff name.
    perfusionist TEXT, -- Source perfusionist; sensitive staff name.
    scrub_nurse_1 TEXT, -- Source first scrub nurse; sensitive staff name.
    row_sha256 TEXT NOT NULL CHECK (length(row_sha256) = 64), -- Hash of canonical original row content.
    UNIQUE (sheet_id, excel_row)
);

CREATE TRIGGER first_surgery_row_source_type
BEFORE INSERT ON first_surgery_row
WHEN COALESCE((SELECT src.source_type FROM data_sheet sh
               JOIN data_source src ON src.id = sh.source_id
               WHERE sh.id = NEW.sheet_id), '') <> 'FIRST_SURGERY'
BEGIN
    SELECT RAISE(ABORT, 'wrong source type for first surgery row');
END;

CREATE TRIGGER cancellation_row_source_type
BEFORE INSERT ON cancellation_row
WHEN COALESCE((SELECT src.source_type FROM data_sheet sh
               JOIN data_source src ON src.id = sh.source_id
               WHERE sh.id = NEW.sheet_id), '') <> 'CANCELLATION'
BEGIN
    SELECT RAISE(ABORT, 'wrong source type for cancellation row');
END;

CREATE TRIGGER surgery_detail_row_source_type
BEFORE INSERT ON surgery_detail_row
WHEN COALESCE((SELECT src.source_type FROM data_sheet sh
               JOIN data_source src ON src.id = sh.source_id
               WHERE sh.id = NEW.sheet_id), '') <> 'SURGERY_DETAIL'
BEGIN
    SELECT RAISE(ABORT, 'wrong source type for surgery detail row');
END;

CREATE INDEX ix_first_surgery_date_room
    ON first_surgery_row(sheet_id, operation_date, operating_unit, operating_room);
CREATE INDEX ix_first_surgery_inpatient
    ON first_surgery_row(inpatient_no) WHERE inpatient_no IS NOT NULL;
CREATE INDEX ix_cancellation_date_room
    ON cancellation_row(sheet_id, record_date, operating_room);
CREATE INDEX ix_cancellation_inpatient
    ON cancellation_row(inpatient_no) WHERE inpatient_no IS NOT NULL;
CREATE INDEX ix_detail_date_room
    ON surgery_detail_row(sheet_id, operation_date, operating_unit, operating_room);
CREATE INDEX ix_detail_inpatient
    ON surgery_detail_row(inpatient_no) WHERE inpatient_no IS NOT NULL;

-- Import diagnostics retained alongside source rows. Descriptions must not copy
-- patient or staff values into routine logs; severity controls query quality gates.
CREATE TABLE data_issue (
    id INTEGER PRIMARY KEY, -- Internal issue key.
    source_id TEXT NOT NULL REFERENCES data_source(id), -- Workbook where the issue occurred.
    sheet_id TEXT, -- Optional worksheet; NULL for file-level issues.
    excel_row INTEGER, -- Optional one-based source row; NULL for sheet/file issues.
    column_name TEXT, -- Physical mapped column ID; NULL for whole-row or file issues.
    code TEXT NOT NULL, -- Stable machine-readable error code.
    severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'BLOCKING')), -- Quality-gate level.
    detail TEXT NOT NULL, -- Non-sensitive diagnostic explanation, never raw patient values.
    FOREIGN KEY (sheet_id, source_id) REFERENCES data_sheet(id, source_id)
);

CREATE INDEX ix_data_issue_source ON data_issue(source_id, severity, excel_row);

-- Versioned runtime copy of the query knowledge base. Published releases are
-- immutable at the service layer; repository JSON remains the maintenance source.
CREATE TABLE kb_release (
    id TEXT PRIMARY KEY, -- Human-stable catalog release ID.
    storage_schema_version INTEGER NOT NULL, -- SQLite schema version this catalog targets.
    state TEXT NOT NULL CHECK (state IN ('DRAFT', 'PUBLISHED')), -- Only PUBLISHED is available to query planning.
    layout_sha256 TEXT NOT NULL CHECK (length(layout_sha256) = 64), -- Canonical hash of the fixed-layout JSON.
    routing_sha256 TEXT NOT NULL CHECK (length(routing_sha256) = 64), -- Canonical hash of cancellation filename-routing JSON.
    query_catalog_sha256 TEXT NOT NULL CHECK (length(query_catalog_sha256) = 64), -- Canonical hash of query entities, fields, and terms JSON.
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64), -- Canonical hash of the manifest binding all three contracts and compiler policy.
    created_at TEXT NOT NULL, -- Release creation timestamp.
    published_at TEXT -- Publication timestamp; NULL while draft.
);

-- Exactly one catalog release is active for new imports and queries. Historical
-- releases remain addressable by the IDs and hashes frozen in their Runs.
CREATE TABLE kb_active_release (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1), -- Fixed key prevents multiple active pointers.
    release_id TEXT NOT NULL REFERENCES kb_release(id), -- Published release chosen for new work.
    activated_at TEXT NOT NULL -- CAS-controlled activation timestamp.
);

CREATE TRIGGER kb_active_release_published_insert
BEFORE INSERT ON kb_active_release
WHEN NOT EXISTS (SELECT 1 FROM kb_release WHERE id = NEW.release_id AND state = 'PUBLISHED')
BEGIN
    SELECT RAISE(ABORT, 'active KB release must be published');
END;

CREATE TRIGGER kb_active_release_published_update
BEFORE UPDATE OF release_id ON kb_active_release
WHEN NOT EXISTS (SELECT 1 FROM kb_release WHERE id = NEW.release_id AND state = 'PUBLISHED')
BEGIN
    SELECT RAISE(ABORT, 'active KB release must be published');
END;

CREATE TRIGGER kb_release_published_immutable_update
BEFORE UPDATE ON kb_release
WHEN OLD.state = 'PUBLISHED'
BEGIN
    SELECT RAISE(ABORT, 'published KB release is immutable');
END;

CREATE TRIGGER kb_release_published_immutable_delete
BEFORE DELETE ON kb_release
WHEN OLD.state = 'PUBLISHED'
BEGIN
    SELECT RAISE(ABORT, 'published KB release is immutable');
END;

-- Exact worksheet layouts accepted by this knowledge release. Header order and
-- parser policy are validated before a workbook is imported.
CREATE TABLE kb_layout (
    release_id TEXT NOT NULL REFERENCES kb_release(id), -- Owning catalog version.
    layout_id TEXT NOT NULL, -- Immutable fixed-format layout version.
    source_type TEXT NOT NULL CHECK (source_type IN
        ('FIRST_SURGERY', 'CANCELLATION', 'SURGERY_DETAIL')), -- One of three export families.
    title_contains TEXT NOT NULL, -- Expected worksheet title marker, not a filename rule.
    header_row INTEGER NOT NULL CHECK (header_row > 0), -- One-based row of ordered column headers.
    columns_json TEXT NOT NULL, -- Closed ordered header-to-column mapping.
    parser_policy_json TEXT NOT NULL, -- Closed rules for sheets, dates, times, and malformed cells.
    PRIMARY KEY (release_id, layout_id)
);

-- Logical query entity bound to one source-row table and explicit row grain.
CREATE TABLE kb_entity (
    release_id TEXT NOT NULL REFERENCES kb_release(id), -- Owning catalog version.
    entity_id TEXT NOT NULL, -- Stable logical ID referenced by query plans.
    physical_table TEXT NOT NULL CHECK (physical_table IN
        ('first_surgery_row', 'cancellation_row', 'surgery_detail_row')), -- Allowlisted base table.
    source_type TEXT NOT NULL CHECK (source_type IN
        ('FIRST_SURGERY', 'CANCELLATION', 'SURGERY_DETAIL')), -- Required workbook family.
    grain TEXT NOT NULL, -- Meaning of one row; not necessarily one performed operation.
    description TEXT NOT NULL, -- Human-readable entity semantics and limits.
    default_date_field TEXT, -- Suggested logical date field; validated against kb_field.
    PRIMARY KEY (release_id, entity_id),
    UNIQUE (release_id, physical_table)
);

-- Approved aggregate/query operations available to dynamic plans. This is
-- separate from manually maintained metrics and grants no new field access.
CREATE TABLE kb_query_capability (
    release_id TEXT NOT NULL REFERENCES kb_release(id), -- Owning catalog version.
    capability_id TEXT NOT NULL, -- Stable plan operation ID, e.g. COUNT_SOURCE_ROWS.
    label TEXT NOT NULL, -- Human-facing name shown with a computed result.
    unit TEXT NOT NULL, -- Result display unit such as source rows.
    grain TEXT NOT NULL, -- Business meaning of one counted item.
    deduplication TEXT NOT NULL, -- Explicit record/entity deduplication policy.
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)), -- Disabled entries may be documented but never compiled.
    PRIMARY KEY (release_id, capability_id)
);

-- One approved logical field. SQL identifiers come from this allowlist, never
-- from model output. Derived fields require a closed compiler access path.
CREATE TABLE kb_field (
    release_id TEXT NOT NULL, -- Owning catalog version.
    entity_id TEXT NOT NULL, -- Parent logical entity.
    field_id TEXT NOT NULL, -- Stable field ID referenced by query plans.
    physical_column TEXT NOT NULL, -- Allowlisted SQLite column name.
    physical_table TEXT CHECK (physical_table IN
        ('data_source', 'cancellation_tag')), -- NULL means entity base table; otherwise approved derived table.
    access_path_json TEXT, -- Closed path from base row to derived table; required for derived fields.
    value_type TEXT NOT NULL CHECK (value_type IN
        ('TEXT', 'DATE', 'TIME_MINUTE', 'LOCAL_DATETIME',
         'INTEGER', 'DECIMAL_TEXT', 'BOOLEAN')), -- Logical type used for plan validation.
    unit TEXT, -- Physical or business unit, if applicable.
    nullable INTEGER NOT NULL CHECK (nullable IN (0, 1)), -- Whether source/derived value can be missing.
    description TEXT NOT NULL, -- Business meaning, including source header where relevant.
    null_meaning TEXT NOT NULL, -- Meaning of missing or unparseable values.
    filter_ops_json TEXT NOT NULL, -- Closed list of permitted predicate operators.
    groupable INTEGER NOT NULL CHECK (groupable IN (0, 1)), -- Whether grouping by field is allowed.
    aggregate_ops_json TEXT NOT NULL, -- Closed list of permitted field aggregates.
    sensitivity TEXT NOT NULL CHECK (sensitivity IN
        ('PUBLIC_METADATA', 'BUSINESS', 'PERSONAL')), -- Controls projection and model exposure.
    value_dictionary_json TEXT, -- Approved enum mapping only; sample frequencies are not policy.
    CHECK ((physical_table IS NULL) = (access_path_json IS NULL)),
    PRIMARY KEY (release_id, entity_id, field_id),
    UNIQUE (release_id, entity_id, physical_column),
    FOREIGN KEY (release_id, entity_id)
        REFERENCES kb_entity(release_id, entity_id)
);

-- Cross-entity join permission. Initially DISABLED because no shared operation
-- key exists in the samples; approval requires evidence beyond matching names.
CREATE TABLE kb_relation (
    release_id TEXT NOT NULL, -- Owning catalog version.
    relation_id TEXT NOT NULL, -- Stable relation ID referenced by query plans.
    left_entity_id TEXT NOT NULL, -- Left logical entity.
    right_entity_id TEXT NOT NULL, -- Right logical entity.
    cardinality TEXT NOT NULL CHECK (cardinality IN
        ('UNKNOWN', '1:1', '1:N', 'N:1')), -- Verified join cardinality; UNKNOWN only while disabled.
    unmatched_policy TEXT NOT NULL CHECK (unmatched_policy IN
        ('KEEP_SEPARATE', 'REJECT', 'EXCLUDE_WITH_AUDIT')), -- Handling of unmatched source rows.
    state TEXT NOT NULL CHECK (state IN ('DISABLED', 'APPROVED')), -- Only APPROVED may be compiled to SQL.
    description TEXT NOT NULL, -- Relationship semantics and known limitations.
    approval_evidence TEXT, -- Business decision or validation evidence for approval.
    CHECK (state = 'DISABLED' OR cardinality <> 'UNKNOWN'),
    PRIMARY KEY (release_id, relation_id),
    FOREIGN KEY (release_id, left_entity_id)
        REFERENCES kb_entity(release_id, entity_id),
    FOREIGN KEY (release_id, right_entity_id)
        REFERENCES kb_entity(release_id, entity_id)
);

-- Ordered equality-key components for one approved composite relation.
-- The publisher also validates that key entities match kb_relation endpoints.
CREATE TABLE kb_relation_key (
    release_id TEXT NOT NULL, -- Owning catalog version.
    relation_id TEXT NOT NULL, -- Parent relationship.
    position INTEGER NOT NULL CHECK (position > 0), -- One-based position in composite key.
    left_entity_id TEXT NOT NULL, -- Left endpoint, validated against parent relation.
    left_field_id TEXT NOT NULL, -- Left logical field ID.
    right_entity_id TEXT NOT NULL, -- Right endpoint, validated against parent relation.
    right_field_id TEXT NOT NULL, -- Right logical field ID.
    PRIMARY KEY (release_id, relation_id, position),
    FOREIGN KEY (release_id, relation_id)
        REFERENCES kb_relation(release_id, relation_id),
    FOREIGN KEY (release_id, left_entity_id, left_field_id)
        REFERENCES kb_field(release_id, entity_id, field_id),
    FOREIGN KEY (release_id, right_entity_id, right_field_id)
        REFERENCES kb_field(release_id, entity_id, field_id)
);

-- A manually authored metric identity. Only its active published version is
-- eligible for intent matching; soft deletion preserves historical Run refs.
CREATE TABLE manual_metric (
    id TEXT PRIMARY KEY, -- Stable ID across description and SQL revisions.
    active_version_id TEXT, -- Published version used by new queries; NULL while unpublished.
    created_at TEXT NOT NULL, -- Manual creation timestamp.
    deleted_at TEXT, -- Soft deletion excludes the metric from new routing.
    FOREIGN KEY (id, active_version_id)
        REFERENCES manual_metric_version(metric_id, id)
);

-- Immutable published version of a manually entered name and natural-language
-- definition. AI may propose SQL, but only the locally compiled template runs.
CREATE TABLE manual_metric_version (
    id TEXT PRIMARY KEY, -- Version ID frozen into query Runs.
    metric_id TEXT NOT NULL REFERENCES manual_metric(id), -- Owning manual metric.
    version_no INTEGER NOT NULL CHECK (version_no > 0), -- Monotone per metric.
    name TEXT NOT NULL CHECK (length(trim(name)) > 0), -- User-authored retrieval name.
    aliases_json TEXT, -- Optional manually approved alternative names; never generated from workbook contents.
    description TEXT NOT NULL CHECK (length(trim(description)) > 0), -- User-authored business meaning and exclusions.
    draft_revision INTEGER NOT NULL DEFAULT 1 CHECK (draft_revision > 0), -- CAS token incremented on every editable draft change or generation result.
    last_generation_id TEXT, -- Idempotency key of the most recent AI SQL generation attempt.
    status TEXT NOT NULL CHECK (status IN
        ('DRAFT', 'SQL_DRAFT', 'VALIDATED', 'PUBLISHED')), -- Only active PUBLISHED versions can route queries.
    output_unit TEXT, -- Display unit; required on publication.
    result_grain TEXT, -- One result's business grain; required on publication.
    kb_release_id TEXT REFERENCES kb_release(id), -- Schema catalog used for generation/validation.
    kb_content_sha256 TEXT CHECK (kb_content_sha256 IS NULL OR length(kb_content_sha256) = 64), -- Pinned catalog hash.
    plan_json TEXT, -- Validated closed logical query plan, independent of a conversation.
    ai_sql_draft TEXT, -- AI proposal for review only; never passed to the SQL executor.
    sql_template TEXT, -- Deterministic compiler output with session/revision parameters.
    sql_sha256 TEXT CHECK (sql_sha256 IS NULL OR length(sql_sha256) = 64), -- Hash of executable template.
    parameter_schema_json TEXT, -- Allowed per-query date, group, and filter parameters.
    validation_json TEXT, -- Local schema/semantic/SQL safety checks and preview receipt.
    created_at TEXT NOT NULL, -- Version creation timestamp, preserved across draft edits.
    updated_at TEXT NOT NULL, -- Last draft edit or generation result timestamp; published rows are immutable.
    published_at TEXT, -- Human-approved publication timestamp; NULL before publication.
    UNIQUE (metric_id, version_no),
    UNIQUE (metric_id, id),
    CHECK (status <> 'PUBLISHED' OR
           (output_unit IS NOT NULL AND result_grain IS NOT NULL AND
            kb_release_id IS NOT NULL AND kb_content_sha256 IS NOT NULL AND
            plan_json IS NOT NULL AND sql_template IS NOT NULL AND
            sql_sha256 IS NOT NULL AND parameter_schema_json IS NOT NULL AND
            validation_json IS NOT NULL AND published_at IS NOT NULL))
);

CREATE INDEX ix_manual_metric_version_name
    ON manual_metric_version(name, status);

-- Database backstops for the service publication transaction. SQL/KB hashes,
-- JSON content, and state-transition checks still require local validation.
CREATE TRIGGER manual_metric_active_published_insert
BEFORE INSERT ON manual_metric
WHEN NEW.active_version_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM manual_metric_version
    WHERE id = NEW.active_version_id AND metric_id = NEW.id AND status = 'PUBLISHED'
)
BEGIN
    SELECT RAISE(ABORT, 'active metric version must be published');
END;

CREATE TRIGGER manual_metric_active_published_update
BEFORE UPDATE OF active_version_id ON manual_metric
WHEN NEW.active_version_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM manual_metric_version
    WHERE id = NEW.active_version_id AND metric_id = NEW.id AND status = 'PUBLISHED'
)
BEGIN
    SELECT RAISE(ABORT, 'active metric version must be published');
END;

CREATE TRIGGER manual_metric_version_published_immutable_update
BEFORE UPDATE ON manual_metric_version
WHEN OLD.status = 'PUBLISHED'
BEGIN
    SELECT RAISE(ABORT, 'published metric version is immutable');
END;

CREATE TRIGGER manual_metric_version_published_immutable_delete
BEFORE DELETE ON manual_metric_version
WHEN OLD.status = 'PUBLISHED'
BEGIN
    SELECT RAISE(ABORT, 'published metric version is immutable');
END;

-- Schema terms and aliases only. Manual metric names/descriptions are searched
-- in their own catalog, before falling back to schema-based dynamic queries.
CREATE TABLE kb_term (
    release_id TEXT NOT NULL REFERENCES kb_release(id), -- Owning catalog version.
    term TEXT NOT NULL, -- User-facing Chinese term or alias.
    target_kind TEXT NOT NULL CHECK (target_kind IN
        ('ENTITY', 'FIELD')), -- Kind of schema target.
    entity_id TEXT NOT NULL DEFAULT '', -- Required for FIELD, empty for ENTITY.
    target_id TEXT NOT NULL, -- Logical target ID, validated when catalog is published.
    priority INTEGER NOT NULL DEFAULT 0, -- Search ranking only, never automatic semantic approval.
    CHECK ((target_kind = 'FIELD' AND entity_id <> '') OR
           (target_kind = 'ENTITY' AND entity_id = '')),
    PRIMARY KEY (release_id, term, target_kind, entity_id, target_id)
);

CREATE INDEX ix_kb_term_lookup ON kb_term(release_id, term, priority DESC);

-- Natural-language regression example paired with its expected closed plan.
-- Examples aid retrieval/testing but cannot grant field or relation permission.
CREATE TABLE kb_query_example (
    release_id TEXT NOT NULL REFERENCES kb_release(id), -- Owning catalog version.
    example_id TEXT NOT NULL, -- Stable regression-case ID.
    question TEXT NOT NULL, -- Example user question.
    expected_plan_json TEXT NOT NULL, -- Closed logical plan, not executable SQL.
    explanation TEXT NOT NULL, -- Why this plan is valid or should be rejected.
    PRIMARY KEY (release_id, example_id)
);
