"""Recovery boundary tests with persisted state and controlled interruption points."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import make_ctx, make_state
from yantra_server.app import create_app
from yantra_server.conductor.budgets import BudgetExceeded, BudgetTracker
from yantra_server.conductor.types import FileExistsCheck, GoalSpec, Plan, PlanTask
from yantra_server.config import load_config
from yantra_server.db.models import RunRow, SessionRow, ToolCallRow


def seed_run(state, workspace: Path, *, status="running", mode="auto") -> str:
    with state.db.session() as s:
        session = SessionRow(workspace_path=str(workspace), mode=mode)
        s.add(session)
        s.flush()
        run = RunRow(
            session_id=session.id,
            workspace_path=str(workspace),
            goal_text="Write a report",
            status=status,
            mode=mode,
            budgets={"max_tokens": 1000, "max_seconds": 90},
            budget_used={
                "tokens_used": 73,
                "prompt_tokens": 60,
                "completion_tokens": 13,
                "seconds_used": 7,
                "tool_calls_used": 2,
            },
        )
        s.add(run)
        s.flush()
        return run.id


def test_startup_reconciles_only_unfinished_runs(tmp_path: Path) -> None:
    app = create_app(load_config())
    state = app.state.yantra
    orphan = seed_run(state, tmp_path)
    completed = seed_run(state, tmp_path, status="done")
    with TestClient(app) as client:
        info = client.get(f"/api/workbench/runs/{orphan}/recovery").json()
        assert info["status"] == "interrupted" and info["can_resume"]
        assert info["budget_used"]["tokens_used"] == 73
        assert client.get(f"/api/workbench/runs/{completed}").json()["status"] == "done"
        assert client.post(f"/api/workbench/runs/{completed}/resume").status_code == 409
        # A browser carrying the previous process's sequence number must still finish.
        events = client.get(
            f"/api/workbench/runs/{orphan}/events", headers={"last-event-id": "999"}
        )
        assert '"status": "interrupted"' in events.text


async def test_resume_guard_and_cumulative_budget(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YANTRA_SEALED", "0")
    state = make_state()
    run_id = seed_run(state, tmp_path, status="interrupted")
    with state.db.session() as s:
        row = s.get(RunRow, run_id)
        row.budgets = {**row.budgets, "model": "operator-selected-model"}
    entered = asyncio.Event()

    async def hold(self, controller, goal_text, attachments, *, resume):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(type(state.conductor), "_prepare", hold)
    await state.conductor.resume_run(run_id)
    driver = state.conductor._active[run_id].driver
    await asyncio.wait_for(entered.wait(), 3)
    with pytest.raises(ValueError, match="already running"):
        await state.conductor.resume_run(run_id)
    budget = state.conductor._active[run_id].controller.budget
    assert budget.max_tokens == 1000 and budget.tokens_used == 73
    assert state.gateway.run_models[run_id] == "operator-selected-model"
    assert budget.elapsed_s() >= 7 and budget.tool_calls_used == 2
    budget.add_usage(5, 2)
    driver.cancel()
    await asyncio.gather(driver, return_exceptions=True)
    with state.db.session() as s:
        row = s.get(RunRow, run_id)
        assert row.status == "interrupted"
        assert row.budget_used["tokens_used"] == 80
    await state.conductor.resume_run(run_id)
    active = state.conductor._active[run_id]
    assert active.controller.budget.tokens_used == 80
    assert active.controller.budget.max_seconds == 90
    active.driver.cancel()
    await asyncio.gather(active.driver, return_exceptions=True)


async def test_uncertain_write_blocks_resume(tmp_path: Path) -> None:
    state = make_state()
    run_id = seed_run(state, tmp_path, status="interrupted")
    with state.db.session() as s:
        s.add(
            ToolCallRow(
                run_id=run_id,
                tool="write_file",
                args={"path": "report.md"},
                idempotency_key="uncertain",
                status="running",
            )
        )
    with pytest.raises(ValueError, match="Recovery paused"):
        await state.conductor.resume_run(run_id)
    assert not state.conductor.list_active()
    assert state.conductor.uncertain_operations(run_id)[0]["args"]["path"] == "report.md"


async def test_recovery_renews_plan_approval(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YANTRA_SEALED", "0")
    state = make_state()
    run_id = seed_run(state, tmp_path, status="interrupted", mode="ask")
    plan = Plan(
        tasks=[
            PlanTask(
                id="t1",
                title="Write report",
                intent="Write",
                role="writer",
                acceptance=[FileExistsCheck(path="report.md")],
            )
        ]
    )
    state.conductor._store_plan(run_id, GoalSpec(objective="Write a report"), plan)
    state.tools.broker._session_grants[run_id] = {"old grant"}

    async def pending_plan():
        for _ in range(100):
            ids = state.conductor.pending_request_ids(run_id)
            if ids:
                return ids[0]
            await asyncio.sleep(0.01)
        pytest.fail("No plan approval was requested")

    await state.conductor.resume_run(run_id)
    old_id = await pending_plan()
    assert run_id not in state.tools.broker._session_grants
    active = state.conductor._active[run_id]
    assert active.controller.tasks["t1"].status == "pending"
    active.driver.cancel()
    await asyncio.gather(active.driver, return_exceptions=True)
    await state.conductor.resume_run(run_id)
    new_id = await pending_plan()
    assert new_id != old_id
    assert not state.conductor.resolve_plan_approval(run_id, "once", request_id=old_id)
    assert not state.conductor.resolve_plan_approval("another-run", "once", request_id=new_id)
    assert state.conductor.resolve_plan_approval(run_id, "deny", request_id=new_id)
    await state.conductor.wait_for_run(run_id, timeout_s=3)
    with state.db.session() as s:
        assert s.get(RunRow, run_id).status == "cancelled"
    assert not list(tmp_path.glob("*.md"))


@pytest.mark.parametrize("status", ["running", "done", "error"])
async def test_missing_result_never_repeats_write(tmp_path: Path, status: str) -> None:
    state = make_state()
    ctx = make_ctx(state, tmp_path, idempotency_key="same")
    args = {"path": "protected.txt", "content": "replacement"}
    tool = state.tools.registry.get("write_file")
    normalized = tool.Args.model_validate(args).model_dump(mode="json")
    with state.db.session() as s:
        s.add(
            ToolCallRow(
                run_id=ctx.run_id,
                tool="write_file",
                args=normalized,
                idempotency_key="same",
                status=status,
            )
        )
    target = tmp_path / "protected.txt"
    target.write_text("original", encoding="utf-8")
    result = await state.tools.runtime.execute("write_file", args, ctx)
    assert not result.ok and "repetition is blocked" in result.error
    assert target.read_text(encoding="utf-8") == "original"


async def test_replay_rejects_different_arguments(tmp_path: Path) -> None:
    state = make_state()
    ctx = make_ctx(state, tmp_path, idempotency_key="same")
    assert (
        await state.tools.runtime.execute("write_file", {"path": "a", "content": "one"}, ctx)
    ).ok
    result = await state.tools.runtime.execute("write_file", {"path": "b", "content": "two"}, ctx)
    assert not result.ok and "conflicts" in result.error
    assert not (tmp_path / "b").exists()


def test_restored_budget_stays_exhausted() -> None:
    budget = BudgetTracker(max_tokens=10)
    budget.restore({"tokens_used": 10})
    with pytest.raises(BudgetExceeded):
        budget.check()


@pytest.mark.parametrize("payload", [None, b"not valid JSON"])
async def test_damaged_result_never_repeats_write(tmp_path: Path, payload: bytes | None) -> None:
    state = make_state()
    ctx = make_ctx(state, tmp_path, idempotency_key="damaged")
    args = {"path": "report.md", "content": "new report"}
    tool = state.tools.registry.get("write_file")
    normalized = tool.Args.model_validate(args).model_dump(mode="json")
    artifact_id = state.artifacts.put_bytes(payload, kind="tool_output") if payload else "missing"
    with state.db.session() as s:
        s.add(
            ToolCallRow(
                run_id=ctx.run_id,
                tool="write_file",
                args=normalized,
                idempotency_key="damaged",
                status="done",
                result_artifact_id=artifact_id,
            )
        )
    result = await state.tools.runtime.execute("write_file", args, ctx)
    assert not result.ok and "repetition is blocked" in result.error
    assert not (tmp_path / "report.md").exists()


def test_recovery_api_reports_uncertain_operation(tmp_path: Path) -> None:
    app = create_app(load_config())
    state = app.state.yantra
    run_id = seed_run(state, tmp_path)
    with state.db.session() as s:
        s.add(
            ToolCallRow(
                run_id=run_id,
                tool="write_file",
                args={"path": "report.md"},
                idempotency_key="uncertain",
                status="running",
            )
        )
    with TestClient(app) as client:
        recovery = client.get(f"/api/workbench/runs/{run_id}/recovery").json()
        assert not recovery["can_resume"]
        assert recovery["uncertain_operations"][0]["tool"] == "write_file"
        response = client.post(f"/api/workbench/runs/{run_id}/resume")
        assert response.status_code == 409
        assert "Recovery paused" in response.json()["detail"]
        assert not state.conductor.list_active()
