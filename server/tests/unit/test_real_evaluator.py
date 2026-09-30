"""Independent evaluation must inspect required fields, not incidental text."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "real_evaluator", ROOT / "scripts/evaluate_workbench.py"
)
assert spec and spec.loader
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


def test_misplaced_evidence_does_not_pass(tmp_path: Path) -> None:
    path = tmp_path / "answer.json"
    path.write_text(
        json.dumps(
            {
                "hydraulic_kw": "2.1582",
                "shaft_kw": 3.08314285714,
                "synthetic": True,
                "assumptions": "assumed density",
                "notes": "inspection-P101.md maintenance-procedure.md OEM limits required; compliance not determined",
            }
        ),
        encoding="utf-8",
    )
    checks = evaluator.check_output("pump", path)
    assert not checks["hydraulic_power"]
    assert not checks["source_files"]
    assert not checks["oem_limit_unknown"]
    assert not checks["assumptions_present"]


def test_exact_output_rejects_extra_text(tmp_path: Path) -> None:
    path = tmp_path / "answer.txt"
    path.write_text("Hello from YANTRA\nExtra claim", encoding="utf-8")
    assert not evaluator.check_output("exact", path)["exact_bytes"]


def test_pump_output_matches_independent_reference(tmp_path: Path) -> None:
    path = tmp_path / "answer.json"
    path.write_text(
        json.dumps(
            {
                "hydraulic_kw": 2.1582,
                "shaft_kw": 3.08314285714,
                "synthetic": True,
                "assumptions": ["estimated efficiency 0.70"],
                "sources": ["inspection-P101.md", "maintenance-procedure.md"],
                "oem_status": "OEM limits required; compliance not determined",
            }
        ),
        encoding="utf-8",
    )
    assert all(evaluator.check_output("pump", path).values())


@pytest.mark.parametrize("value,passed", [(385, True), (55, False), ("385", False), (True, False)])
def test_generated_code_reference(tmp_path: Path, value, passed: bool) -> None:
    path = tmp_path / "result.json"
    path.write_text(json.dumps({"sum_of_squares": value}), encoding="utf-8")
    assert evaluator.check_output("code", path)["sum_of_squares"] is passed


def test_code_evaluator_requires_real_isolated_execution() -> None:
    call = dict(
        tool="python",
        status="done",
        result_available=True,
        ok=True,
        exit_code=0,
        timed_out=False,
        backend="bwrap",
    )
    assert evaluator.check_execution({"calls": [call]})
    for key, value in [
        ("tool", "write_file"),
        ("backend", "local"),
        ("result_available", False),
        ("ok", False),
        ("timed_out", True),
        ("exit_code", 1),
    ]:
        assert not evaluator.check_execution({"calls": [{**call, key: value}]})
