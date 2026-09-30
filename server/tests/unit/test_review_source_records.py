import pytest

from tests.helpers import make_ctx, make_state
from yantra_server.conductor.verifier import Verifier
from yantra_server.db.models import ToolCallRow
from yantra_server.tools.base import ToolResult


@pytest.mark.parametrize(
    "case", ["source", "output", "outside", "failed", "other_run", "other_task"]
)
def test_review_reads_are_scoped_and_outputs_are_not_source_truth(tmp_path, case):
    state = make_state()
    try:
        ctx = make_ctx(state, tmp_path / "workspace")
        path = (
            "../outside.pdf"
            if case == "outside"
            else "result.xlsx"
            if case == "output"
            else "rules.pdf"
        )
        result = ToolResult(
            ok=case != "failed", summary="1 page", content="UNIQUE RULE TEXT", data={"pages": [1]}
        )
        ref = state.artifacts.put_text(result.model_dump_json(), kind="tool_output")
        with state.db.session() as db:
            db.add(
                ToolCallRow(
                    run_id="other" if case == "other_run" else "r1",
                    task_id="other" if case == "other_task" else "t1",
                    tool="read_pages",
                    args={"path": path, "pages": "1"},
                    status="done",
                    idempotency_key="read",
                    result_artifact_id=ref,
                )
            )
        verifier = Verifier(state, state.extras["roster"], ctx.workspace, "r1", ctx.sandbox)
        actions, sources = verifier._recorded_read_evidence(
            "t1", {str(ctx.workspace / "result.xlsx")}
        )
        assert ("UNIQUE RULE TEXT" in sources) is (case == "source")
        if case == "source":
            assert "read_pages" in actions and "rules.pdf" in sources
        if case in {"other_run", "other_task"}:
            assert not actions and not sources
    finally:
        state.db.dispose()


def test_review_read_excerpts_are_bounded_and_marked(tmp_path):
    state = make_state()
    try:
        ctx = make_ctx(state, tmp_path / "workspace")
        ref = state.artifacts.put_text(
            ToolResult(ok=True, summary="1 page", content="x" * 12000).model_dump_json(),
            kind="tool_output",
        )
        with state.db.session() as db:
            db.add(
                ToolCallRow(
                    run_id="r1",
                    task_id="t1",
                    tool="read_file",
                    args={"path": "source.csv"},
                    status="done",
                    idempotency_key="read",
                    result_artifact_id=ref,
                )
            )
        verifier = Verifier(state, state.extras["roster"], ctx.workspace, "r1", ctx.sandbox)
        _, sources = verifier._recorded_read_evidence("t1", set())
        assert len(sources) < 6500 and "TRUNCATED" in sources
    finally:
        state.db.dispose()
