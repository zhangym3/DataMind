"""Deletion rules for metric definition drafts."""

import pytest
from docx import Document

from insight_agent import definitions, manual_metrics
from insight_agent.core import AppError
from insight_agent.files import import_file
from insight_agent.service import AppService
from insight_agent.worker import run_once
from test_document_workflow import DocumentGateway


def _draft(tmp_path):
    service = AppService(tmp_path / "workspace")
    document = Document()
    document.add_paragraph("统计口径：以手术事件计数，统计窗口为完整日期。")
    path = tmp_path / "指标说明.docx"
    document.save(path)
    attachment = import_file(service.store, path, "library")
    service.route(attachment["id"], "METRIC_DEFINITION")
    # Explicit routing creates the first draft for a library definition.
    definition = service.store.list("definition")[0]
    return service, definition


def test_unreferenced_draft_can_be_deleted(tmp_path):
    service, definition = _draft(tmp_path)

    result = service.delete_definition(definition["id"], definition["revision"], "确认删除")

    assert result == {"deletedDefinitionId": definition["id"]}
    assert service.store.list("definition") == []
    assert service.store.list("attachment") == []
    assert any(event["type"] == "definition.deleted" for event in _events(service))


def test_definition_delete_rejects_identifier_as_confirmation(tmp_path):
    service, definition = _draft(tmp_path)

    with pytest.raises(AppError) as error:
        service.delete_definition(definition["id"], definition["revision"], definition["id"])

    assert error.value.code == "DELETE_CONFIRMATION"
    assert service.store.get("definition", definition["id"])["status"] == "DRAFT"


def test_published_definition_can_be_deleted(tmp_path):
    service, definition = _draft(tmp_path)
    with service.store.transaction() as db:
        definition = service.store.put("definition", definition | {"status": "PUBLISHED"}, db)

    result = service.delete_definition(definition["id"], definition["revision"], "确认删除")
    assert result["deletedDefinitionId"] == definition["id"]
    assert not service.bootstrap()["definitions"]


def test_bound_draft_is_removed_from_management_but_retained_for_session(tmp_path):
    service, definition = _draft(tmp_path)
    with service.store.transaction() as db:
        session = service.store.put("session", {
            "name": "引用草稿的会话", "definitionId": definition["id"], "attachmentIds": [],
            "mappings": {}, "state": "DRAFT", "archived": False, "draft": "", "baselineHash": None,
        }, db)

    result = service.delete_definition(definition["id"], definition["revision"], "确认删除")

    assert result == {"deletedDefinitionId": definition["id"], "retainedForHistory": True}
    retained = service.session_metrics(session["id"])
    assert retained["deletedAt"] and retained["disabled"] and not retained["default"]
    assert retained["nodes"] == definition["nodes"]
    assert not service.bootstrap()["definitions"]
    assert not service.bootstrap()["libraryAttachments"]
    assert service.store.get("session", session["id"])["definitionId"] == definition["id"]


def test_draft_with_active_extraction_cannot_be_deleted(tmp_path):
    service, definition = _draft(tmp_path)
    job = service.queue("extract", definition["id"], {"definitionId": definition["id"]})

    with pytest.raises(AppError) as error:
        service.delete_definition(definition["id"], definition["revision"], "确认删除")

    assert error.value.code == "DEFINITION_BUSY"
    assert service.store.jobs()[0]["id"] == job["jobId"]


def _draft_with_published_child(tmp_path):
    service, definition = _draft(tmp_path)
    session = service.create_session()
    attachment = import_file(service.store, tmp_path / "指标说明.docx", session["id"])
    with service.store.transaction() as db:
        confirmed = service.store.get("attachment", definition["attachmentId"], db)
        attachment = service.store.put("attachment", attachment | {"route": confirmed["route"], "status": "READY"}, db)
        session = service.store.put("session", session | {"attachmentIds": [attachment["id"]]}, db)
    child = definitions.create_draft(service.store, attachment["id"], attachment["name"], definition["id"])
    child = definitions.extract(service.store, child["id"], DocumentGateway(), "synthetic-extraction")
    child = service.publish_bind(child["id"], child["revision"], "合成审核人", True, session["id"], session["revision"])
    assert child["manifest"]["parentId"] == definition["id"]
    return service, definition, child, service.store.get("session", session["id"])


def test_published_private_child_keeps_its_snapshot_when_parent_draft_is_deleted(tmp_path):
    service, definition, child, session = _draft_with_published_child(tmp_path)
    attachment = service.store.get("attachment", definition["attachmentId"])

    result = service.dispatch("definition.delete", {"definition_id": definition["id"],
        "revision": definition["revision"], "confirmation": "确认删除"})

    assert result["retainedForHistory"]
    assert not service.bootstrap()["definitions"]
    assert not service.bootstrap()["libraryAttachments"]
    assert service.store.get("definition", child["id"]) == child
    assert service.session_metrics(session["id"]) == child
    assert service.store.get("session", session["id"]) == session
    assert service.store.get("attachment", attachment["id"]) == attachment
    assert service.store.read_blob(attachment["sha256"])
    restarted = AppService(service.store.home)
    assert not restarted.bootstrap()["definitions"]
    assert restarted.session_detail(session["id"])["metricDefinition"]["id"] == child["id"]
    assert restarted.store.get("definition", definition["id"])["deletedAt"]
    event = next(event for event in _events(service) if event["type"] == "definition.deleted")
    assert '"retainedForHistory":true' in event["data"]


@pytest.mark.parametrize("kind,field", [("run", "definitionId"), ("mapping_revision", "definitionId"),
    ("history_context", "definition_id"), ("turn", "preparationDefinitionId"), ("fill_attempt", "definitionId")])
def test_historical_references_retain_draft_and_source(tmp_path, kind, field):
    service, definition = _draft(tmp_path)
    with service.store.transaction() as db:
        reference = service.store.put(kind, {"nested": [{field: definition["id"]}]}, db)

    assert service.delete_definition(definition["id"], definition["revision"], "确认删除")["retainedForHistory"]
    assert service.store.get(kind, reference["id"]) == reference
    assert service.store.get("definition", definition["id"])["nodes"] == definition["nodes"]
    assert service.store.get("attachment", definition["attachmentId"])


@pytest.mark.parametrize("action", ["get", "metadata", "amend", "save", "model_save", "manual", "clone",
    "publish", "extract", "queue", "ensure", "disable", "parent", "delete"])
def test_deleted_snapshot_cannot_be_reused_or_modified(tmp_path, action):
    service, definition, _, _ = _draft_with_published_child(tmp_path)
    service.delete_definition(definition["id"], definition["revision"], "确认删除")
    retained = service.store.get("definition", definition["id"])
    definition_id, revision = retained["id"], retained["revision"]
    actions = {
        "get": lambda: service.managed_definition(definition_id),
        "metadata": lambda: definitions.metadata(service.store, definition_id, revision, "v9", ""),
        "amend": lambda: definitions.amend(service.store, definition_id, "补充条款", "维护人", "统计", [], revision),
        "save": lambda: definitions.save_candidates(service.store, definition_id, [], [], revision),
        "model_save": lambda: definitions.save_candidates(service.store, definition_id, [], [], revision,
                                                         provenance={"kind": "MODEL_EXTRACTION"}),
        "manual": lambda: manual_metrics.save(service.store, {}, definition_id, revision),
        "clone": lambda: manual_metrics.clone(service.store, definition_id, revision),
        "publish": lambda: service.publish_bind(definition_id, revision, "审核人", True),
        "extract": lambda: definitions.extract(service.store, definition_id, None, "blocked"),
        "queue": lambda: service.queue("extract", definition_id, {"definitionId": definition_id}),
        "ensure": lambda: service.ensure_definition_extract(definition_id),
        "disable": lambda: service.disable_definition(definition_id, False),
        "parent": lambda: definitions.create_draft(service.store, retained["attachmentId"], "新版本", definition_id),
        "delete": lambda: service.delete_definition(definition_id, revision, "确认删除"),
    }
    events_before = _events(service)
    with pytest.raises(AppError) as error:
        actions[action]()
    assert error.value.code == "DEFINITION_DELETED"
    assert service.store.get("definition", definition_id) == retained
    assert not service.store.jobs()
    assert _events(service) == events_before


def test_reimport_same_document_creates_visible_draft_instead_of_reusing_deleted_parent(tmp_path):
    service, definition, child, _ = _draft_with_published_child(tmp_path)
    service.delete_definition(definition["id"], definition["revision"], "确认删除")

    request = service.import_paths([str(tmp_path / "指标说明.docx")])
    assert run_once(service)
    job = next(job for job in service.store.jobs() if job["id"] == request["jobId"])
    assert job["status"] == "SUCCEEDED", job
    new_id = job["result"]["acceptedDetails"][0]["definitionId"]
    assert new_id != definition["id"]
    visible = service.bootstrap()["definitions"]
    assert [d["id"] for d in visible] == [new_id]
    new_draft = service.managed_definition(new_id)
    assert not new_draft.get("deletedAt") and new_draft["parentId"] is None
    assert new_draft["attachmentId"] == definition["attachmentId"]
    assert any(job["kind"] == "extract" and job["scope"] == new_id for job in service.store.jobs())
    assert service.store.get("definition", child["id"]) == child


@pytest.mark.parametrize("referenced", [False, True])
def test_stale_revision_rejects_deletion_without_changes(tmp_path, referenced):
    service, definition = _draft(tmp_path)
    if referenced:
        with service.store.transaction() as db:
            service.store.put("run", {"contract": {"definitionId": definition["id"]}}, db)
    events_before = _events(service)
    with pytest.raises(AppError) as error:
        service.delete_definition(definition["id"], definition["revision"] - 1, "确认删除")
    assert error.value.code == "REVISION_CONFLICT"
    assert service.store.get("definition", definition["id"]) == definition
    assert _events(service) == events_before


def _events(service):
    with service.store.connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM events ORDER BY seq")]
