"""Install and read the bundled, versioned three-source query catalog."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from .core import digest, now, require

COMPILER_VERSION = "three-source-compiler/1.0"
CATALOG_FILES = {
    "layout": "three-source-layouts-v1.json",
    "routing": "cancellation-unit-routing-v1.json",
    "query": "three-source-query-kb-v1.json",
}
TYPE_MAP = {
    "TEXT": "TEXT", "CSV_TAGS": "TEXT", "TIME_HHMM": "TEXT",
    "DATE": "DATE", "LOCAL_DATETIME": "LOCAL_DATETIME", "INTEGER": "INTEGER",
}


def contract_dir():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return root / "docs" / "contracts"


def bundled_catalog():
    folder = contract_dir()
    result = {}
    for key, name in CATALOG_FILES.items():
        path = folder / name
        require(path.is_file(), "KB_PACKAGE_MISSING", f"Bundled catalog file is missing: {name}")
        result[key] = json.loads(path.read_text(encoding="utf-8"))
    layouts, routing, query = result["layout"], result["routing"], result["query"]
    require(query["storageSchemaVersion"] == 2 and query["layoutCatalog"] == layouts["catalogVersion"]
            and routing["appliesTo"] == "CANCELLATION", "KB_PACKAGE_INVALID", "Catalog versions do not agree")
    by_type = {item["sourceType"]: item for item in layouts["layouts"]}
    require(set(by_type) == {"FIRST_SURGERY", "CANCELLATION", "SURGERY_DETAIL"}
            and len(layouts["layouts"]) == 3, "KB_PACKAGE_INVALID", "Exactly three source layouts are required")
    for entity in query["entities"]:
        layout = by_type[entity["sourceType"]]
        require({column["column"] for column in layout["columns"]} == set(entity["fields"])
                and len(layout["columns"]) == len(entity["fields"]),
                "KB_PACKAGE_INVALID", "Layout and query fields differ")
        require(all(column["type"] in TYPE_MAP for column in layout["columns"]),
                "KB_PACKAGE_INVALID", "Layout contains an unsupported type")
    entity_ids = {entity["id"] for entity in query["entities"]}
    require(len(entity_ids) == 3, "KB_PACKAGE_INVALID", "Entity IDs must be unique")
    fields = {entity["id"]: set(entity["fields"]) | set(entity.get("derivedFields", {}))
              for entity in query["entities"]}
    for term in query["terms"]:
        valid = term["target"] in entity_ids if term["kind"] == "ENTITY" else (
            term.get("entity") in fields and term["target"] in fields[term["entity"]])
        require(valid, "KB_PACKAGE_INVALID", "Knowledge-base term has an unknown target")
    for relation in query["relations"]:
        require(relation["left"] in entity_ids and relation["right"] in entity_ids and
                relation["state"] == "DISABLED", "KB_PACKAGE_INVALID", "Unapproved relationship cannot be published")
    allowed_capabilities = {item["id"] for item in query["queryCapabilities"] if item["allowed"]}
    for example in query["examples"]:
        plan = example["plan"]
        require(plan["entity"] in entity_ids and plan["aggregate"] in allowed_capabilities and
                set(plan.get("groupBy", [])) <= fields[plan["entity"]],
                "KB_PACKAGE_INVALID", "Example references an unapproved plan element")
    hashes = {key: digest(value) for key, value in result.items()}
    result["hashes"] = hashes
    result["contentHash"] = digest({"files": hashes, "compiler": COMPILER_VERSION})
    return result


def _install_fts(db):
    try:
        db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS manual_metric_fts USING fts5(
            metric_id UNINDEXED, version_id UNINDEXED, route_status UNINDEXED,
            name, aliases, description, tokenize='trigram')""")
        db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS kb_search_fts USING fts5(
            release_id UNINDEXED, target_kind UNINDEXED, entity_id UNINDEXED,
            target_id UNINDEXED, title, aliases, description, tokenize='trigram')""")
        return True
    except sqlite3.OperationalError as exc:
        if "fts5" not in str(exc).lower() and "trigram" not in str(exc).lower():
            raise
        return False


def install_builtin_kb(db):
    bundle = bundled_catalog()
    query, layouts, routing = (bundle[key] for key in ("query", "layout", "routing"))
    release_id = query["release"]
    physical_tables = {"FIRST_SURGERY": "first_surgery_row", "CANCELLATION": "cancellation_row",
                       "SURGERY_DETAIL": "surgery_detail_row"}
    for layout in layouts["layouts"]:
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({physical_tables[layout['sourceType']]})")}
        require({column["column"] for column in layout["columns"]} <= columns,
                "KB_PACKAGE_INVALID", "Layout field does not exist in the business table")
    existing = db.execute("SELECT content_sha256 FROM kb_release WHERE id=?", (release_id,)).fetchone()
    require(not existing or existing[0] == bundle["contentHash"],
            "KB_PACKAGE_CHANGED", "Bundled catalog changed without a new release ID")
    if not existing:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("""INSERT INTO kb_release
                (id,storage_schema_version,state,layout_sha256,routing_sha256,query_catalog_sha256,
                 content_sha256,created_at,published_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (release_id, 2, "DRAFT", bundle["hashes"]["layout"],
                 bundle["hashes"]["routing"], bundle["hashes"]["query"],
                 bundle["contentHash"], now(), None))
            for layout in layouts["layouts"]:
                db.execute("INSERT INTO kb_layout VALUES (?,?,?,?,?,?,?)",
                           (release_id, layout["id"], layout["sourceType"], layout["titleContains"],
                            layout["headerRow"], json.dumps(layout["columns"], ensure_ascii=False),
                            json.dumps({k: v for k, v in layout.items() if k not in
                                        ("id", "sourceType", "titleContains", "headerRow", "columns")}, ensure_ascii=False)))
            for entity in query["entities"]:
                entity_id = entity["id"]
                db.execute("INSERT INTO kb_entity VALUES (?,?,?,?,?,?,?)",
                           (release_id, entity_id, entity["table"], entity["sourceType"],
                            entity["grain"], entity["grain"], entity["dateField"]))
                columns = {item["column"]: item for item in by_type(layouts, entity["sourceType"])["columns"]}
                for field_id, field in entity["fields"].items():
                    column = columns[field_id]
                    _insert_field(db, release_id, entity_id, field_id, field_id, None, None,
                                  TYPE_MAP[column["type"]], field)
                for field_id, field in entity.get("derivedFields", {}).items():
                    _insert_field(db, release_id, entity_id, field_id, field["column"], field["table"],
                                  json.dumps({"kind": field_id}, ensure_ascii=False), "TEXT", field)
            for capability in query["queryCapabilities"]:
                db.execute("INSERT INTO kb_query_capability VALUES (?,?,?,?,?,?,?)",
                           (release_id, capability["id"], capability["label"], capability["unit"],
                            capability["grain"], capability["deduplication"], int(capability["allowed"])))
            for relation in query["relations"]:
                db.execute("INSERT INTO kb_relation VALUES (?,?,?,?,?,?,?,?,?)",
                           (release_id, relation["id"], relation["left"], relation["right"],
                            relation["cardinality"], relation["unmatchedPolicy"], relation["state"],
                            relation["reason"], None))
            for term in query["terms"]:
                db.execute("INSERT INTO kb_term VALUES (?,?,?,?,?,?)",
                           (release_id, term["term"], term["kind"], term.get("entity", ""), term["target"], 0))
            for index, example in enumerate(query["examples"], 1):
                db.execute("INSERT INTO kb_query_example VALUES (?,?,?,?,?)",
                           (release_id, f"example-{index}", example["question"],
                            json.dumps(example["plan"], ensure_ascii=False), "Bundled regression example"))
            db.execute("UPDATE kb_release SET state='PUBLISHED',published_at=? WHERE id=?", (now(), release_id))
            db.commit()
        except BaseException:
            db.rollback()
            raise
    for layout in layouts["layouts"]:
        row = db.execute("""SELECT title_contains,header_row,columns_json FROM kb_layout
            WHERE release_id=? AND layout_id=?""", (release_id, layout["id"])).fetchone()
        require(row and row[0] == layout["titleContains"] and row[1] == layout["headerRow"]
                and json.loads(row[2]) == layout["columns"],
                "KB_PROJECTION_CHANGED", "已发布知识库格式投影与内置合同不一致")
    for entity in query["entities"]:
        for field_id, field in (entity["fields"] | entity.get("derivedFields", {})).items():
            row = db.execute("""SELECT description,physical_column,physical_table FROM kb_field
                WHERE release_id=? AND entity_id=? AND field_id=?""",
                (release_id, entity["id"], field_id)).fetchone()
            physical_column = field.get("column", field_id)
            physical_table = field.get("table")
            require(row and tuple(row) == (field["meaning"], physical_column, physical_table),
                    "KB_PROJECTION_CHANGED", "已发布知识库字段投影与内置合同不一致")
    active = db.execute("SELECT release_id FROM kb_active_release WHERE singleton=1").fetchone()
    if not active:
        db.execute("INSERT INTO kb_active_release VALUES (1,?,?)", (release_id, now()))
    elif active[0] != release_id:
        db.execute("UPDATE kb_active_release SET release_id=?,activated_at=? WHERE singleton=1",
                   (release_id, now()))
    if _install_fts(db):
        db.execute("DELETE FROM kb_search_fts WHERE release_id=?", (release_id,))
        for entity in query["entities"]:
            db.execute("""INSERT INTO kb_search_fts
                (release_id,target_kind,entity_id,target_id,title,aliases,description)
                VALUES (?,?,?,?,?,?,?)""",
                (release_id, "ENTITY", entity["id"], entity["id"], entity["id"],
                 " ".join(t["term"] for t in query["terms"] if t["kind"] == "ENTITY" and t["target"] == entity["id"]),
                 entity["grain"]))
            for field_id, field in (entity["fields"] | entity.get("derivedFields", {})).items():
                db.execute("""INSERT INTO kb_search_fts
                    (release_id,target_kind,entity_id,target_id,title,aliases,description)
                    VALUES (?,?,?,?,?,?,?)""",
                    (release_id, "FIELD", entity["id"], field_id, field_id,
                     " ".join(t["term"] for t in query["terms"] if t["kind"] == "FIELD"
                              and t.get("entity") == entity["id"] and t["target"] == field_id),
                     field["meaning"]))
        db.execute("DELETE FROM manual_metric_fts")
        for row in db.execute("""SELECT m.id,v.id,v.name,v.aliases_json,v.description
            FROM manual_metric m JOIN manual_metric_version v ON v.id=m.active_version_id
            WHERE m.deleted_at IS NULL AND v.status='PUBLISHED'"""):
            db.execute("INSERT INTO manual_metric_fts(metric_id,version_id,route_status,name,aliases,description) VALUES (?,?,?,?,?,?)",
                       (row[0], row[1], "PUBLISHED", row[2], " ".join(json.loads(row[3] or "[]")), row[4]))


def by_type(layouts, source_type):
    return next(item for item in layouts["layouts"] if item["sourceType"] == source_type)


def _insert_field(db, release_id, entity_id, field_id, physical_column, physical_table,
                  access_path, value_type, field):
    db.execute("""INSERT INTO kb_field
        (release_id,entity_id,field_id,physical_column,physical_table,access_path_json,
         value_type,unit,nullable,description,null_meaning,filter_ops_json,groupable,
         aggregate_ops_json,sensitivity,value_dictionary_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (release_id, entity_id, field_id, physical_column, physical_table, access_path,
         value_type, None, 1, field["meaning"], "Blank or invalid source value",
         json.dumps(field.get("filter", [])), int(field.get("group", False)), "[]",
         field.get("sensitivity", "BUSINESS"),
         json.dumps(field["valueDictionary"], ensure_ascii=False) if "valueDictionary" in field else None))


def active_release(db):
    row = db.execute("""SELECT r.* FROM kb_active_release a JOIN kb_release r ON r.id=a.release_id
                        WHERE a.singleton=1 AND r.state='PUBLISHED'""").fetchone()
    require(row, "KB_UNAVAILABLE", "No published query catalog is active")
    return dict(row)
