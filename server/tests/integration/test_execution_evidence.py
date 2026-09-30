"""A success claim cannot substitute for a task-scoped persisted execution result."""

from pathlib import Path

import pytest

from tests.helpers import make_ctx, make_state
from yantra_server.conductor.types import FinishArgs, PlanTask, ToolSucceededCheck
from yantra_server.conductor.verifier import Verifier
from yantra_server.db.models import ToolCallRow
from yantra_server.tools.base import ToolResult


@pytest.mark.parametrize(
    "case", ["missing", "wrong_run", "wrong_task", "failed", "timeout", "corrupt", "passed"]
)
async def test_execution_gate_requires_scoped_successful_evidence(
    tmp_path: Path, case: str
) -> None:
    state = make_state()
    ctx = make_ctx(state, tmp_path / "workspace")
    check = ToolSucceededCheck(tool="python")
    task = PlanTask(
        id="t1",
        title="Execute",
        intent="Execute the requested code",
        role="coder",
        acceptance=[check],
    )
    verifier = Verifier(state, state.extras["roster"], ctx.workspace, "this-run", ctx.sandbox)
    if case != "missing":
        result = ToolResult(
            ok=case != "failed",
            summary="executed",
            data={
                "sandbox": {
                    "exit_code": 1 if case == "failed" else 0,
                    "timed_out": case == "timeout",
                }
            },
        )
        ref = state.artifacts.put_text(
            "not JSON" if case == "corrupt" else result.model_dump_json(), kind="tool_output"
        )
        with state.db.session() as db:
            db.add(
                ToolCallRow(
                    run_id="different" if case == "wrong_run" else "this-run",
                    task_id="t2" if case == "wrong_task" else "t1",
                    tool="python",
                    status="done",
                    idempotency_key="case",
                    result_artifact_id=ref,
                )
            )
    outcome = await verifier._check_tool_succeeded(
        check.model_dump(), task, FinishArgs(summary="I executed it successfully")
    )
    assert outcome.passed is (case == "passed")
    state.db.dispose()
