"""Reviews must retain criterion evidence and fail closed on incomplete verification."""

import pytest
from pydantic import ValidationError

from yantra_server.conductor.types import EvidenceReview, ReviewedCriterion
from yantra_server.conductor.verifier import evidence_report


def test_office_format_rejects_renamed_text_and_accepts_real_workbook(tmp_path):
    from types import SimpleNamespace

    import openpyxl

    from yantra_server.conductor.verifier import Verifier

    context = SimpleNamespace(_resolve=lambda name: tmp_path / name)
    path = tmp_path / "offers.xlsx"
    path.write_text("{'title': 'Offers'}")
    assert not Verifier._check_office_format(context, path.name).passed
    book = openpyxl.Workbook()
    book.active.append(["Basic", "Freight", "Total"])
    book.active.append([10, 2, "=A2+B2"])
    book.save(path)
    assert Verifier._check_office_format(context, path.name).passed
    book.active["C2"] = "=(basic_INR+freight_INR)*(1+tax_fraction)"
    book.save(path)
    result = Verifier._check_office_format(context, path.name)
    assert not result.passed and "undefined formula name basic_INR" in result.detail
    book.active["C2"] = "=SUM(A2:B2)"
    book.save(path)
    assert Verifier._check_office_format(context, path.name).passed


@pytest.mark.parametrize(
    "intent,required",
    [
        ("Use spreadsheet formulas referencing source cells.", True),
        ("Include Excel formulas in the output.", True),
        ("Do not use spreadsheet formulas.", False),
        ("Never use Excel formulas.", False),
        ("Copy these numbers into a spreadsheet.", False),
    ],
)
def test_formula_requirement_uses_only_explicit_positive_instructions(intent, required):
    from yantra_server.conductor.verifier import explicitly_requests_spreadsheet_formulas

    assert explicitly_requests_spreadsheet_formulas(intent) is required


async def test_optimistic_review_cannot_pass_missing_requested_formulas(tmp_path, monkeypatch):
    import openpyxl

    from tests.helpers import make_ctx, make_state
    from yantra_server.conductor.types import ArtifactSpec, FinishArgs, PlanTask, ReviewerReport
    from yantra_server.conductor.verifier import Verifier

    state = make_state()
    try:
        ctx = make_ctx(state, tmp_path / "workspace")
        book = openpyxl.Workbook()
        book.active.append(["Input", "Total"])
        book.active.append([25, 100])
        book.save(ctx.workspace / "result.xlsx")

        async def optimistic_review(*args):
            return ReviewerReport(score=100, verdict="pass")

        monkeypatch.setattr(Verifier, "_review", optimistic_review)
        task = PlanTask(
            id="t1",
            title="Calculate",
            intent="Use spreadsheet formulas referencing inputs.",
            role="analyst",
            outputs=[ArtifactSpec(name="result.xlsx")],
            acceptance=[{"kind": "file_exists", "path": "result.xlsx"}],
        )
        verifier = Verifier(
            state, state.extras["roster"], ctx.workspace, "formula-test", ctx.sandbox
        )
        result = await verifier.verify(
            task, FinishArgs(summary="Complete", artifacts=["result.xlsx"]), 0
        )
        assert result.verdict == "fail"
        assert any("zero formula cells" in c.detail and not c.passed for c in result.checks)
        book.active["B2"] = "=A2*4"
        book.save(ctx.workspace / "result.xlsx")
        assert verifier._check_office_format("result.xlsx", require_formulas=True).passed
        excerpt = verifier._artifact_excerpts(FinishArgs(summary="", artifacts=["result.xlsx"]))
        assert "1 formula cells" in excerpt and "B2 [f]: =A2*4" in excerpt
        assert "not recalculated results" in excerpt
    finally:
        state.db.dispose()


async def test_positive_model_review_cannot_override_unplanned_invalid_artifact(
    tmp_path, monkeypatch
):
    from tests.helpers import make_ctx, make_state
    from yantra_server.conductor.types import FinishArgs, PlanTask, ReviewerReport
    from yantra_server.conductor.verifier import Verifier

    state = make_state()
    try:
        ctx = make_ctx(state, tmp_path / "workspace")
        (ctx.workspace / "result.xlsx").write_text("not an Office file")

        async def optimistic_review(*args):
            return ReviewerReport(score=100, verdict="pass")

        monkeypatch.setattr(Verifier, "_review", optimistic_review)
        task = PlanTask(
            id="t1",
            title="Compare",
            intent="Compare offers",
            role="analyst",
            acceptance=[{"kind": "file_exists", "path": "result.xlsx"}],
        )
        verifier = Verifier(
            state, state.extras["roster"], ctx.workspace, "review-test", ctx.sandbox
        )
        result = await verifier.verify(
            task, FinishArgs(summary="Complete", artifacts=["result.xlsx"]), 0
        )
        assert result.reviewer.verdict == "pass"
        assert result.verdict == "fail"
        assert any(c.check["kind"] == "office_format" and not c.passed for c in result.checks)
    finally:
        state.db.dispose()


def test_render_handoff_is_scoped_to_the_fresh_sole_deliverable(tmp_path):
    from yantra_server.conductor.types import ArtifactSpec, PlanTask
    from yantra_server.conductor.verifier import rendered_review_candidate
    from yantra_server.tools.base import ToolResult

    task = PlanTask(
        id="t1",
        title="Render",
        intent="Compare",
        role="analyst",
        outputs=[ArtifactSpec(name="result.xlsx")],
        acceptance=[{"kind": "file_exists", "path": "result.xlsx"}],
    )
    result = ToolResult(ok=True, summary="rendered", data={"path": "result.xlsx"})
    assert rendered_review_candidate(task, tmp_path, "render_document", result) is None
    (tmp_path / "result.xlsx").write_text("invalid file to be rejected by normal verifier")
    candidate = rendered_review_candidate(task, tmp_path, "render_document", result)
    assert candidate.artifacts == ["result.xlsx"]
    assert "require verification" in candidate.summary
    assert rendered_review_candidate(task, tmp_path, "read_file", result) is None
    result.ok = False
    assert rendered_review_candidate(task, tmp_path, "render_document", result) is None
    result.ok = True
    task.outputs.append(ArtifactSpec(name="other.xlsx"))
    assert rendered_review_candidate(task, tmp_path, "render_document", result) is None


def test_execution_handoff_requires_new_valid_outputs(tmp_path) -> None:
    from yantra_server.conductor.types import ArtifactSpec, PlanTask, ToolSucceededCheck
    from yantra_server.conductor.verifier import execution_review_candidate
    from yantra_server.tools.base import ToolResult

    task = PlanTask(
        id="t1",
        title="Calculate",
        intent="Use Python",
        role="coder",
        outputs=[ArtifactSpec(name="out.json")],
        acceptance=[ToolSucceededCheck(tool="python")],
    )
    result = ToolResult(ok=True, summary="exit 0", data={"files_changed": ["out.json"]})
    assert execution_review_candidate(task, tmp_path, "python", result) is None
    (tmp_path / "out.json").write_text("{'wrong': 5}")
    assert execution_review_candidate(task, tmp_path, "python", result) is None
    (tmp_path / "out.json").write_text('{"measured": 42}')
    candidate = execution_review_candidate(task, tmp_path, "python", result)
    assert candidate is not None and candidate.artifacts == ["out.json"]
    assert "subject to validation" in candidate.summary
    assert "42" in candidate.summary
    assert execution_review_candidate(task, tmp_path, "write_file", result) is None
    result.data["files_changed"] = []
    assert execution_review_candidate(task, tmp_path, "python", result) is None
    result.data["files_changed"] = ["out.json"]
    result.ok = False
    assert execution_review_candidate(task, tmp_path, "python", result) is None


@pytest.mark.parametrize("status", ["missing", "incorrect", "unverifiable"])
def test_one_unresolved_requirement_prevents_acceptance(status: str) -> None:
    review = EvidenceReview.model_validate(
        {
            "criteria": [
                {"requirement": "valid JSON", "evidence": "parsed successfully", "status": "met"},
                {
                    "requirement": "traceable values",
                    "evidence": "source not provided",
                    "status": status,
                },
            ],
            "all_requirements_covered": True,
        }
    )
    result = evidence_report(review)
    assert result.verdict == "fail"
    assert len(result.failures) == 1
    assert "source not provided" in result.fix_instructions[0]
    assert result.criteria == review.criteria


def test_incomplete_review_never_passes() -> None:
    result = evidence_report(
        EvidenceReview(
            criteria=[ReviewedCriterion(requirement="file", evidence="present", status="met")],
            all_requirements_covered=False,
        )
    )
    assert result.verdict == "fail"
    with pytest.raises(ValidationError):
        EvidenceReview(criteria=[], all_requirements_covered=True)


def test_supported_complete_review_preserves_evidence() -> None:
    review = EvidenceReview(
        criteria=[ReviewedCriterion(requirement="file", evidence="present", status="met")],
        all_requirements_covered=True,
    )
    result = evidence_report(review)
    assert result.verdict == "pass"
    assert result.failures == []
    assert result.criteria == review.criteria
