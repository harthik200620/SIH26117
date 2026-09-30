from tests.helpers import make_ctx, make_state
from yantra_server.conductor.types import ArtifactSpec, FinishArgs, PlanTask, ReviewerReport
from yantra_server.conductor.verifier import Verifier


async def test_compact_failure_repairs_before_inference_but_valid_artifact_still_needs_review(
    tmp_path, monkeypatch
):
    state = make_state()
    try:
        state.config.execution.compact_planning = True
        ctx = make_ctx(state, tmp_path / "workspace")
        calls = []

        async def review(*args):
            calls.append(True)
            return ReviewerReport(score=90, verdict="pass")

        monkeypatch.setattr(Verifier, "_review", review)
        task = PlanTask(
            id="t1",
            title="Report",
            intent="Create report.json from the sources",
            role="analyst",
            outputs=[ArtifactSpec(name="report.json")],
            acceptance=[
                {"kind": "file_exists", "path": "report.json"},
                {"kind": "file_exists", "path": "source.csv"},
            ],
        )
        verifier = Verifier(state, state.extras["roster"], ctx.workspace, "r1", ctx.sandbox)
        finish = FinishArgs(summary="Done", artifacts=["report.json"])
        outcome = await verifier.verify(task, finish, 1)
        assert outcome.verdict == "fail" and outcome.reviewer.method == "deterministic"
        assert not calls
        assert len(outcome.reviewer.fix_instructions) >= 2
        assert {"report.json", "source.csv"}.issubset(
            {r.check.get("path") for r in outcome.checks if not r.passed}
        )
        (ctx.workspace / "report.json").write_text('{"claim": "needs semantic review"}')
        (ctx.workspace / "source.csv").write_text("x\n1\n")
        outcome = await verifier.verify(task, finish, 2)
        assert calls == [True] and outcome.verdict == "pass"
    finally:
        state.db.dispose()
