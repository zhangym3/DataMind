"""Trusted built-in domain manifests. Attachments cannot install capabilities."""
from .core import ENGINE_VERSION, digest, require

FIELDS = {
    "surgery": {
        "surgery_event_id": ("string", "none", ["surgery_event"]),
        "operation_date": ("date", "date", ["surgery_event"]),
        "entry_at": ("datetime", "datetime", ["surgery_event"]),
        "anesthesia_start_at": ("datetime", "datetime", ["surgery_event"]),
        "department": ("string", "none", ["surgery_event"]),
    },
    "hospital_ops": {
        "department": ("string", "none", ["department_day", "discharge_event", "revenue_event"]),
        "stat_date": ("date", "date", ["department_day"]),
        "occupied_bed_days": ("decimal", "bed_day", ["department_day"]),
        "available_bed_days": ("decimal", "bed_day", ["department_day"]),
        "encounter_id": ("string", "none", ["discharge_event"]),
        "discharge_date": ("date", "date", ["discharge_event"]),
        "stay_days": ("decimal", "day", ["discharge_event"]),
        "revenue_id": ("string", "none", ["revenue_event"]),
        "revenue_date": ("date", "date", ["revenue_event"]),
        "amount": ("decimal", "currency", ["revenue_event"]),
    },
}


def manifest(domain_id):
    entities = {"hospital_ops": ["department_day", "discharge_event", "revenue_event"], "surgery": ["surgery_event"]}
    require(domain_id in entities, "DOMAIN_MISMATCH", "领域包未注册")
    value = {"id": domain_id, "version": "1.0.0", "entities": entities[domain_id],
             "schemaVersion": "1.0.0", "engineVersion": ENGINE_VERSION,
             "timezone": "Asia/Shanghai", "preparation": ["union", "join"],
             "extensions": {"first_event": False, "sequence_classify": False, "snapshot_as_of": False},
             "fieldTypes": ["string", "integer", "decimal", "date", "datetime", "boolean"],
             "reviewPolicies": ["DETERMINISTIC_APPROVED", "AI_REQUIRED"],
             "fieldDictionary": [{"id": name, "valueType": spec[0], "unit": spec[1], "entities": spec[2]} for name, spec in FIELDS[domain_id].items()],
             "fieldExtensionPolicy": "explicit-rule-and-source-evidence; no-implicit-coercion",
             "datePolicy": "ISO-8601; explicit-rule-timezone; full-datetime-for-duration",
             "units": ["none", "date", "datetime", "minute", "day", "bed_day", "currency", "case", "person", "item", "percent", "ratio"],
             "operators": ["count", "count_distinct", "sum", "mean", "min", "max", "ratio", "duration_minutes"],
             "acceptanceCapabilities": {"deterministicGoldenCases": True, "clinicalValidation": False, "realProviderValidation": False},
             "goldenCorpus": "golden-cases/1.0", "clinicalInterpretation": False}
    return value | {"sha256": digest(value)}
