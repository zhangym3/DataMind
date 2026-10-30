from pathlib import Path

import pytest

from insight_agent.skills import invoke


REAL_LEGACY_DOC = Path(r"D:\手术数据\手术室领导提供数据.doc")


@pytest.mark.skipif(not REAL_LEGACY_DOC.is_file(), reason="real legacy DOC fixture is not available")
def test_word_spec_legacy_doc_fallback_extracts_chinese_text(monkeypatch):
    """The Word skill's no-Office path handles a valid OLE DOC."""
    import doc2txt

    def reject_new_fib(*args, **kwargs):
        raise RuntimeError("synthetic Antiword compatibility failure")

    monkeypatch.setattr(doc2txt, "extract_text", reject_new_fib)
    snapshot = invoke("surgery-word-spec", "parse", REAL_LEGACY_DOC.read_bytes(), ".doc")
    text = "\n".join(node["text"] for node in snapshot["nodes"])
    assert snapshot["kind"] == "document"
    assert snapshot["parserVersion"] == "surgery-word-spec/1.0.0"
    assert snapshot["layoutVerified"] is False
    assert "手术数量指标" in text
    assert "LEGACY_DOC_TEXT_ONLY" in snapshot["warnings"]


def test_word_spec_rejects_excel_and_excel_skill_rejects_word():
    with pytest.raises(Exception):
        invoke("surgery-word-spec", "parse", b"not-a-doc", ".xlsx")
    with pytest.raises(Exception):
        invoke("surgery-excel-ingest", "parse", b"not-an-excel", ".docx")
