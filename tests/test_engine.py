import copy
import json
from pathlib import Path
from fractions import Fraction

import pytest

from insight_agent.core import AppError, sha
from insight_agent.engine import calculate, display, validate_rule

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "docs/contracts/golden-cases.json").read_text(encoding="utf-8"))["cases"]


@pytest.fixture
def surgery_rule():
    return json.loads((ROOT / "docs/contracts/examples/surgery-delay-count.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_independent_golden_cases(case):
    rule = json.loads((ROOT / "docs/contracts" / case["metricExample"]).read_text(encoding="utf-8"))
    result = calculate(rule, case["rows"], case["parameters"])
    expected = case["expected"]
    assert result["outcome"] == expected["outcome"]
    if expected["outcome"] == "SKIP":
        assert result["reasonCode"] == expected["reasonCode"]
        assert "displayValue" not in result
    elif "exactValue" in expected:
        assert result["groups"][0]["displayValue"] == expected["exactValue"]
        assert result["audit"]["inWindowRows"] == expected["inWindowRows"]
        assert result["audit"]["outsideWindowRows"] == expected["outsideWindowRows"]
    elif "simpleGroups" in expected:
        assert {g["group"]["department"]: g["displayValue"] for g in result["groups"]} == expected["simpleGroups"]
    else:
        for actual, wanted in zip(result["groups"], expected["groups"]):
            assert actual["displayValue"] == wanted["displayValue"]
            assert actual["group"]["department"] == wanted["department"]
            assert actual["numerator"]["exactNumerator"] == wanted["numerator"]
            assert actual["denominator"]["exactNumerator"] == wanted["denominator"]


def test_reject_forged_evidence(surgery_rule):
    with pytest.raises(AppError, match="原文证据"):
        validate_rule(surgery_rule, {})


def test_exact_threshold_and_rounding(surgery_rule):
    case = copy.deepcopy(CASES[0])
    case["rows"] = [case["rows"][0]]
    case["rows"][0]["anesthesia_start_at"] = "2026-10-01T08:20:00.000001+08:00"
    assert calculate(surgery_rule, case["rows"], case["parameters"])["groups"][0]["displayValue"] == "1"
    assert display(Fraction(1, 8), {"scale": 2, "roundingMode": "half_even"}) == "0.12"
    assert display(Fraction(1, 8), {"scale": 2, "roundingMode": "half_up"}) == "0.13"
    assert display(Fraction(-1, 8), {"scale": 2, "roundingMode": "down"}) == "-0.12"


@pytest.mark.parametrize("mutation,code", [
    (lambda r: r.update(python="print(1)"), "INVALID_RULE_SCHEMA"),
    (lambda r: r["expression"].update(operator="eval"), "INVALID_RULE_SCHEMA"),
    (lambda r: r["expression"]["where"]["right"].update(value=20.0), "INVALID_RULE_SCHEMA"),
    (lambda r: r["timeWindow"].update(anchorField="unknown"), "INVALID_REFERENCE"),
    (lambda r: r["expression"]["where"]["right"].update(unit="currency"), "OPERATOR_TYPE_MISMATCH"),
    (lambda r: r["missingPolicy"].update(maxExcludedFraction="2"), "DEFINITION_INCOMPLETE"),
    (lambda r: r["domainProfile"].update(id="hospital_ops"), "VERSION_MISMATCH"),
])
def test_negative_rule_compiler(surgery_rule, mutation, code):
    mutation(surgery_rule)
    with pytest.raises(AppError) as caught:
        validate_rule(surgery_rule)
    assert caught.value.code == code


def test_invalid_window_does_not_compute(surgery_rule):
    with pytest.raises(AppError) as error:
        calculate(surgery_rule, [], {"date_start": "2026-11-01", "date_end": "2026-10-01"})
    assert error.value.code == "INVALID_WINDOW"


def test_all_data_scope_ignores_rule_date_window(surgery_rule):
    case = copy.deepcopy(CASES[0])
    result = calculate(surgery_rule, case["rows"], {"scope": "ALL_DATA"})
    assert result["outcome"] == "VALUE"
    assert result["groups"][0]["displayValue"] == "3"
    assert result["audit"]["analysisScope"] == "ALL_DATA"
    assert result["audit"]["inWindowRows"] == len(case["rows"])
    assert result["audit"]["outsideWindowRows"] == 0
