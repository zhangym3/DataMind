"""Read persisted library progress without exposing parser inputs or model prompts."""
from __future__ import annotations

import json


def activities(store, definitions, history_limit=12):
    visible = {d["id"]: d for d in definitions if not d.get("sessionId") and not d.get("deletedAt")}
    with store.connect() as db:
        rows = db.execute("""SELECT jobs.* FROM jobs LEFT JOIN objects
            ON objects.kind='definition' AND objects.id=jobs.scope
            WHERE (jobs.kind='import' AND jobs.scope='library') OR
                (jobs.kind='extract' AND objects.id IS NOT NULL
                 AND json_extract(objects.data,'$.sessionId') IS NULL
                 AND json_extract(objects.data,'$.deletedAt') IS NULL)
            ORDER BY CASE WHEN jobs.status IN ('QUEUED','RUNNING') THEN 0 ELSE 1 END,
                jobs.created DESC,jobs.rowid DESC""")
        jobs, events, finished = [], [], 0
        for row in rows:
            active = row["status"] in ("QUEUED", "RUNNING")
            if not active and finished >= history_limit:
                break
            finished += not active
            job = {k: row[k] for k in ("id", "kind", "scope", "status", "created", "updated")}
            definition = visible.get(row["scope"])
            if definition:
                job.update(definitionId=definition["id"], name=definition["name"],
                           nodeCount=len(definition["nodes"]), metricCount=len(definition["candidates"]))
            result = json.loads(row["result"]) if row["result"] else None
            job["result"] = ({
                "acceptedDetails": [{k: a[k] for k in ("id", "name", "role", "confidence", "definitionId", "extractJobId") if k in a}
                                    for a in result.get("acceptedDetails", [])],
                "failures": [{k: f[k] for k in ("name", "code", "message") if k in f} for f in result.get("failures", [])],
            } if result and row["kind"] == "import" else None)
            error = json.loads(row["error"]) if row["error"] else None
            job["error"] = {k: error[k] for k in ("code", "message")} if error else None
            jobs.append(job)
            progress = db.execute("""SELECT seq,type,data,created FROM events WHERE scope=?
                AND json_extract(data,'$.jobId')=?
                AND type IN ('job.queued','job.terminal','import.started','import.progress','definition.progress')
                ORDER BY seq DESC LIMIT 100""", (row["scope"], row["id"])).fetchall()
            events.extend({"seq": item["seq"], "type": item["type"], "data": json.loads(item["data"]),
                           "created": item["created"]} for item in reversed(progress))
    return {"jobs": jobs, "events": sorted(events, key=lambda item: item["seq"])}
