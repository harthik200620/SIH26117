"""The seal smoke: a short agent run that must complete with all guards active (SPEC §14.5)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yantra_server.db.models import RunRow, SessionRow
from yantra_server.gateway.engines.mock import MockEngine

if TYPE_CHECKING:
    from yantra_server.state import AppState

GOAL = "Create smoke.txt containing exactly the line SEALED OK"


def _script_mock(engine: MockEngine) -> None:
    engine.reset()  # clear any scripts left by a prior scenario when suites share one state
    engine.add_canned({"json": {"route": "workflow"}}, role="planner", contains="Request routing:")
    engine.add_canned(
        {
            "json": {
                "objective": "Create smoke.txt with SEALED OK",
                "deliverables": [{"type": "text", "name": "smoke.txt"}],
                "constraints": [],
                "success_criteria": ["smoke.txt exists"],
                "context_refs": [],
                "assumptions": [],
                "open_questions": [],
            }
        },
        role="planner",
        contains="Goal:",
    )
    engine.add_canned(
        {
            "json": {
                "tasks": [
                    {
                        "id": "t1",
                        "title": "Write smoke.txt",
                        "intent": "Write the file with the exact line",
                        "role": "coder",
                        "inputs": [],
                        "outputs": [{"name": "smoke.txt", "type": "text"}],
                        "acceptance": [{"kind": "file_exists", "path": "smoke.txt"}],
                        "budget": {
                            "max_steps": 4,
                            "max_tokens": 8000,
                            "max_seconds": 60,
                            "max_retries": 1,
                        },
                    }
                ],
                "edges": [],
                "rationale": "single write",
                "version": 1,
            }
        },
        role="planner",
        contains="GoalSpec:",
    )
    engine.add_canned(
        {"json": {"ok": True, "findings": []}}, role="utility", contains="GoalSpec deliverables"
    )
    engine.add_canned(
        {"json": {"score": 95, "failures": [], "fix_instructions": [], "verdict": "pass"}},
        role="reviewer",
    )
    engine.add_canned_sequence(
        [
            {
                "json": {
                    "thought": "write the file",
                    "action": {
                        "tool": "write_file",
                        "args": {"path": "smoke.txt", "content": "SEALED OK\n"},
                    },
                }
            },
            {
                "json": {
                    "thought": "done",
                    "action": {
                        "tool": "finish",
                        "args": {
                            "summary": "smoke.txt written",
                            "artifacts": ["smoke.txt"],
                            "claims": [],
                            "self_check": {"score": 90, "notes": ""},
                        },
                    },
                }
            },
        ],
        role="executor",
        contains="Task t1:",
    )


async def run_seal_smoke(state: AppState) -> Any:  # CheckOutcome (avoids an import cycle)
    from yantra_server.sandbox import SandboxError, select_sandbox
    from yantra_server.seal.verify import CheckOutcome

    with tempfile.TemporaryDirectory(prefix="yantra-smoke-") as tmp:
        workspace = Path(tmp)
        try:
            select_sandbox(state.config.sandbox, workspace, sealed=state.config.sealed())
        except SandboxError as exc:
            return CheckOutcome("agent_smoke", True, f"skipped: {exc}", skipped=True)

        for process in state.supervisor.processes:
            if isinstance(process.engine, MockEngine):
                _script_mock(process.engine)

        with state.db.session() as s:
            session = SessionRow(workspace_path=str(workspace), collections=[], mode="auto")
            s.add(session)
            s.flush()
            session_id = session.id
        run_id = await state.conductor.start_run(session_id, GOAL, [], "auto")
        await state.conductor.wait_for_run(run_id, timeout_s=120)
        with state.db.session() as s:
            run = s.get(RunRow, run_id)
            status = run.status if run else "missing"
        produced = (workspace / "smoke.txt").is_file()
        ok = status in ("done", "done_with_gaps") and produced
        return CheckOutcome(
            "agent_smoke", ok, f"run {status}; smoke.txt {'written' if produced else 'MISSING'}"
        )
