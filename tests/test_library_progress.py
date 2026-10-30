"""Library progress is durable, attempt-scoped, and backed by real parser work."""
import json

from docx import Document

from insight_agent.core import AppError
from insight_agent.library_progress import activities
from insight_agent.service import AppService
from insight_agent.worker import run_once
from test_document_workflow import DocumentGateway


def imported(tmp_path, paragraphs):
    service = AppService(tmp_path / "home")
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    document.add_paragraph("分母：当前全部手术事件，计算规则须以原文证据为准。")
    path = tmp_path / "合成指标说明.docx"
    document.save(path)
    job_id = service.import_paths([str(path)])["jobId"]
    assert service.bootstrap()["libraryActivities"]["jobs"][0]["status"] == "QUEUED"
    assert run_once(service)
    definition = service.store.list("definition")[0]
    return service, definition, job_id


def test_import_and_live_extraction_progress_survive_service_reopen(tmp_path):
    service, definition, import_id = imported(tmp_path, ["统计口径：按 surgery_event_id 唯一手术事件计数，分母为全部手术事件，缺失值不计算。"])
    before = service.bootstrap()["libraryActivities"]
    job = next(j for j in before["jobs"] if j["kind"] == "extract")
    assert job["status"] == "QUEUED" and job["nodeCount"] == 2
    imported_job = next(j for j in before["jobs"] if j["id"] == import_id)
    assert imported_job["result"]["acceptedDetails"][0]["extractJobId"] == job["id"]
    assert [e["data"]["phase"] for e in before["events"] if e["type"] == "import.progress"] == ["READING", "PARSING", "ROUTING", "COMPLETED"]

    class InspectingGateway(DocumentGateway):
        def call(self, *args):
            live = AppService(service.store.home).bootstrap()["libraryActivities"]
            assert next(j for j in live["jobs"] if j["id"] == job["id"])["status"] == "RUNNING"
            latest = [e for e in live["events"] if e["data"]["jobId"] == job["id"]][-1]
            assert latest["data"]["phase"] == "EXTRACTING"
            return super().call(*args)

    assert run_once(service, InspectingGateway())
    after = AppService(service.store.home).bootstrap()["libraryActivities"]
    events = [e["data"] for e in after["events"] if e["type"] == "definition.progress"]
    assert [e["phase"] for e in events] == ["STARTED", "EXTRACTING", "VALIDATING", "READY"]
    assert events[-1]["completed"] == events[-1]["total"] == 1
    assert service.store.get("definition", definition["id"])["status"] == "DRAFT"
    encoded = json.dumps(after, ensure_ascii=False)
    assert str(tmp_path) not in encoded and "ruleSchema" not in encoded and "sourceEvidence" not in encoded


def test_fill_batch_progress_counts_only_saved_batches_and_tracks_retry(tmp_path):
    service, definition, _ = imported(tmp_path, ["指标说明：按手术事件统计总手术量，每项单位台。", *["总手术量X台。" for _ in range(6)]])

    class Gateway:
        calls = 0

        def call(self, stage, payload, schema, budget_id):
            self.calls += 1
            if self.calls in (2, 4):
                raise AppError("MODEL_TIMEOUT", "合成网络超时")
            return {"items": [{"slot": s["id"], "name": "总手术量", "method": "SURGERY_COUNT", "room": "", "start": "", "end": "", "minutes": 0, "reason": ""} for s in payload["slots"]]}

    gateway = Gateway()
    assert run_once(service, gateway)
    first = next(j for j in service.bootstrap()["libraryActivities"]["jobs"] if j["kind"] == "extract")
    assert first["status"] == "FAILED" and first["error"]["code"] == "MODEL_TIMEOUT"
    events = [e["data"] for e in service.bootstrap()["libraryActivities"]["events"] if e["data"]["jobId"] == first["id"] and e["type"] == "definition.progress"]
    assert [e["completed"] for e in events if e["phase"] == "BATCH_DONE"] == [4, 5]
    assert not any(e["phase"] == "READY" for e in events)
    assert any(e["phase"] == "SPLITTING" for e in events)
    retry = service.queue("extract", definition["id"], {"definitionId": definition["id"]})
    assert run_once(service, gateway)
    after = AppService(service.store.home).bootstrap()["libraryActivities"]
    retry_events = [e["data"] for e in after["events"] if e["data"]["jobId"] == retry["jobId"] and e["type"] == "definition.progress"]
    assert any(e["phase"] == "RESUMED" and e["completed"] == 4 for e in retry_events)
    assert retry_events[-1]["phase"] == "READY" and retry_events[-1]["completed"] == 6


def test_library_progress_filters_private_deleted_and_unrelated_jobs(tmp_path):
    service, definition, _ = imported(tmp_path, ["指标说明：统计手术量，计数单位台次，分母为手术事件。"])
    with service.store.transaction() as db:
        private = service.store.put("definition", definition | {"id": "private", "sessionId": "private_session"}, db)
        service.store.enqueue(db, "extract", private["id"], {"definitionId": private["id"]})
        service.store.enqueue(db, "import", "private_session", {"paths": ["secret-path"], "sessionId": "private_session"})
        service.store.enqueue(db, "model_test", "settings", {"secret": "secret-key"})
        for i in range(18):
            job_id = service.store.enqueue(db, "import", "library", {"paths": ["secret-path"], "sessionId": None})
            db.execute("UPDATE jobs SET status='SUCCEEDED' WHERE id=?", (job_id,))
    data = activities(service.store, service.store.list("definition"))
    assert len([j for j in data["jobs"] if j["status"] == "SUCCEEDED"]) == 12
    assert any(j["scope"] == definition["id"] and j["status"] == "QUEUED" for j in data["jobs"])
    encoded = json.dumps(data)
    assert "private_session" not in encoded and "secret-" not in encoded
    with service.store.transaction() as db:
        service.store.put("definition", definition | {"deletedAt": "2026-10-30T00:00:00Z"}, db)
    assert not any(j["scope"] == definition["id"] for j in service.bootstrap()["libraryActivities"]["jobs"])
