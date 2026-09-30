"""Conductor end-to-end on the MockEngine (M3 DoD scenarios)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from tests.helpers import make_state
from yantra_server.db.models import RunRow, StepRow, TaskRow, ToolCallRow
from yantra_server.gateway.engines.mock import MockEngine
from yantra_server.rpc import Connection
from yantra_server.state import AppState

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------ scripting helpers


def step(tool: str, args: dict[str, Any], thought: str = "next") -> dict[str, Any]:
    return {"json": {"thought": thought, "action": {"tool": tool, "args": args}}}


def finish(
    summary: str, artifacts: list[str] | None = None, claims: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "json": {
            "thought": "done",
            "action": {
                "tool": "finish",
                "args": {
                    "summary": summary,
                    "artifacts": artifacts or [],
                    "claims": claims or [],
                    "self_check": {"score": 85, "notes": ""},
                },
            },
        }
    }


def goal_spec_for(*names: str) -> dict[str, Any]:
    return {
        "json": {
            "objective": "Produce " + ", ".join(names),
            "deliverables": [
                {"type": "code" if n.endswith(".py") else "md", "name": n} for n in names
            ],
            "constraints": [],
            "success_criteria": ["files exist"],
            "context_refs": [],
            "assumptions": ["workspace is empty"],
            "open_questions": [],
        }
    }


GOAL_SPEC = {
    "json": {
        "objective": "Produce greeting module and report",
        "deliverables": [
            {"type": "code", "name": "hello.py"},
            {"type": "md", "name": "report.md"},
        ],
        "constraints": [],
        "success_criteria": ["files exist"],
        "context_refs": [],
        "assumptions": ["workspace is empty"],
        "open_questions": [],
    }
}


def two_task_plan() -> dict[str, Any]:
    return {
        "json": {
            "tasks": [
                {
                    "id": "t1",
                    "title": "Write hello module",
                    "intent": "Create hello.py printing a greeting",
                    "role": "coder",
                    "inputs": [],
                    "outputs": [{"name": "hello.py", "type": "code"}],
                    "acceptance": [{"kind": "file_exists", "path": "hello.py"}],
                    "budget": {
                        "max_steps": 6,
                        "max_tokens": 20000,
                        "max_seconds": 120,
                        "max_retries": 2,
                    },
                },
                {
                    "id": "t2",
                    "title": "Write report",
                    "intent": "Summarise the module in report.md",
                    "role": "writer",
                    "inputs": ["t1"],
                    "outputs": [{"name": "report.md", "type": "md"}],
                    "acceptance": [{"kind": "file_exists", "path": "report.md"}],
                    "budget": {
                        "max_steps": 6,
                        "max_tokens": 20000,
                        "max_seconds": 120,
                        "max_retries": 2,
                    },
                },
            ],
            "edges": [["t1", "t2"]],
            "rationale": "write then report",
            "version": 1,
        }
    }


REVIEW_PASS = {"json": {"score": 92, "failures": [], "fix_instructions": [], "verdict": "pass"}}
CRITIC_OK = {"json": {"ok": True, "findings": []}}


class Harness:
    """A wired state + scripted mock + event capture for one conductor test."""

    def __init__(self, state: AppState, workspace: Path) -> None:
        self.state = state
        self.workspace = workspace
        self.events: list[dict[str, Any]] = []
        self.conn = Connection(id="test")
        state.bus.attach(self.conn)

    @property
    def mock(self) -> MockEngine:
        engine = self.state.supervisor.processes[0].engine
        assert isinstance(engine, MockEngine)
        return engine

    def drain_events(self) -> list[dict[str, Any]]:
        while not self.conn.outbound.empty():
            self.events.append(self.conn.outbound.get_nowait())
        return self.events

    def notes(self, method: str) -> list[dict[str, Any]]:
        return [e["params"] for e in self.drain_events() if e.get("method") == method]

    async def start_session(self, mode: str = "auto") -> str:
        with self.state.db.session() as s:
            from yantra_server.db.models import SessionRow

            row = SessionRow(workspace_path=str(self.workspace), collections=[], mode=mode)
            s.add(row)
            s.flush()
            return row.id


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("YANTRA_SEALED", "0")  # local sandbox on this GPU-less dev box
    state = make_state()
    state.bus.bind_loop(asyncio.get_running_loop())
    await state.supervisor.start_all()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    h = Harness(state, workspace)
    h.mock.add_canned({"json": {"route": "workflow"}}, role="planner", contains="Request routing:")
    h.mock.add_canned(CRITIC_OK, role="utility", contains="GoalSpec deliverables")
    h.mock.add_canned(REVIEW_PASS, role="reviewer")
    yield h
    await state.supervisor.stop_all()


async def run_and_wait(
    h: Harness, mode: str = "auto", goal: str = "make greeting and report"
) -> str:
    session_id = await h.start_session(mode)
    run_id = await h.state.conductor.start_run(session_id, goal, [], mode)
    await h.state.conductor.wait_for_run(run_id, timeout_s=60)
    return run_id


def run_status(h: Harness, run_id: str) -> str:
    with h.state.db.session() as s:
        run = s.get(RunRow, run_id)
        assert run is not None
        return run.status


# ------------------------------------------------------------------ scenarios


async def test_two_task_plan_completes(harness: Harness) -> None:
    h = harness
    report_summary = (
        "Checked the generated report. " * 15 + "OEM limits required; compliance not determined."
    )
    h.mock.add_canned(GOAL_SPEC, role="planner", contains="Goal:")
    h.mock.add_canned(two_task_plan(), role="planner", contains="GoalSpec:")
    h.mock.add_canned_sequence(
        [
            step("write_file", {"path": "hello.py", "content": "print('namaste')\n"}),
            finish("hello.py written", artifacts=["hello.py"]),
        ],
        role="executor",
        contains="Task t1:",
    )
    h.mock.add_canned_sequence(
        [
            step("write_file", {"path": "report.md", "content": "# Report\nModule works.\n"}),
            finish(report_summary, artifacts=["report.md"]),
        ],
        role="executor",
        contains="Task t2:",
    )
    run_id = await run_and_wait(h)
    assert run_status(h, run_id) == "done"
    assert (h.workspace / "hello.py").read_text() == "print('namaste')\n"
    assert (h.workspace / "report.md").exists()

    with h.state.db.session() as s:
        tasks = list(s.execute(select(TaskRow).where(TaskRow.run_id == run_id)).scalars())
        assert {t.status for t in tasks} == {"done"}
        steps = list(s.execute(select(StepRow).where(StepRow.run_id == run_id)).scalars())
        assert len(steps) == 4  # 2 steps per task

    finished = h.notes("run.finished")
    assert finished and finished[-1]["status"] == "done"
    assert report_summary in finished[-1]["summary"]
    artifact_names = {a["name"] for a in finished[-1]["artifacts"]}
    assert artifact_names == {"hello.py", "report.md"}
    assert h.state.audit.verify().ok


async def test_failure_retries_with_escalation(harness: Harness) -> None:
    h = harness
    h.mock.add_canned(goal_spec_for("hello.py"), role="planner", contains="Goal:")
    plan = two_task_plan()
    plan["json"]["tasks"] = [plan["json"]["tasks"][0]]
    plan["json"]["edges"] = []
    h.mock.add_canned(plan, role="planner", contains="GoalSpec:")
    h.mock.add_canned_sequence(
        [
            finish("claims done but wrote nothing"),  # attempt 1 → file_exists fails
            step("write_file", {"path": "hello.py", "content": "print('hi')\n"}),
            finish("actually written now", artifacts=["hello.py"]),
        ],
        role="executor",
        contains="Task t1:",
    )
    run_id = await run_and_wait(h)
    assert run_status(h, run_id) == "done"
    with h.state.db.session() as s:
        task = s.execute(select(TaskRow).where(TaskRow.run_id == run_id)).scalar_one()
        assert task.status == "done"
        assert task.attempt == 2
        assert task.ladder_rung == 1  # raise_effort applied
    escalations = h.notes("escalation")
    assert escalations and escalations[0]["rung"] == "raise_effort"
    verifies = h.notes("verify.result")
    assert verifies[0]["report"]["verdict"] == "fail"
    assert verifies[-1]["report"]["verdict"] == "pass"


async def test_literal_contract_rejects_mismatch_then_finishes_without_model_loop(
    harness: Harness,
) -> None:
    h = harness
    h.state.config.execution.compact_planning = True
    h.mock.add_canned_sequence(
        [
            step("write_file", {"path": "exact.txt", "content": "wrong"}),
            finish("claims done", artifacts=["exact.txt"]),
            step(
                "write_file", {"path": "exact.txt", "content": "required bytes", "overwrite": True}
            ),
        ],
        role="executor",
        contains="Task t1:",
    )
    run_id = await run_and_wait(h, goal="Write exact.txt containing exactly required bytes")
    assert run_status(h, run_id) == "done"
    assert (h.workspace / "exact.txt").read_bytes() == b"required bytes"
    reviews = h.notes("verify.result")
    assert reviews[0]["report"]["verdict"] == "fail"
    assert reviews[-1]["report"]["verdict"] == "pass"
    assert reviews[-1]["report"]["reviewer"]["method"] == "deterministic"


async def test_generated_json_handoff_still_requires_review(harness: Harness) -> None:
    h = harness
    h.state.config.execution.compact_planning = True
    h.mock.add_canned(
        {
            "json": {
                "tasks": [
                    {
                        "title": "Calculate",
                        "instruction": "Compute and save",
                        "role": "coder",
                        "output_file": "answer.json",
                    }
                ]
            }
        },
        role="planner",
    )
    h.mock.add_canned_sequence(
        [
            step(
                "python",
                {"code": "import json\nopen('answer.json','w').write(json.dumps({'total': 8}))"},
            ),
            step(
                "python",
                {"code": "import json\nopen('answer.json','w').write(json.dumps({'total': 2*5}))"},
            ),
        ],
        role="executor",
        contains="Task t1:",
    )
    h.mock.add_canned_sequence(
        [
            {
                "json": {
                    "criteria": [
                        {
                            "requirement": "2 times 5",
                            "evidence": "Observed 8, expected 10",
                            "status": "incorrect",
                        }
                    ],
                    "all_requirements_covered": True,
                }
            },
            {
                "json": {
                    "criteria": [
                        {
                            "requirement": "2 times 5",
                            "evidence": "Observed 10 and recorded execution",
                            "status": "met",
                        }
                    ],
                    "all_requirements_covered": True,
                }
            },
        ],
        role="reviewer",
    )
    run_id = await run_and_wait(
        h, goal="Use the Python tool to calculate 2 times 5 and write answer.json."
    )
    assert run_status(h, run_id) == "done"
    reviews = h.notes("verify.result")
    assert reviews[0]["report"]["verdict"] == "fail"
    assert reviews[-1]["report"]["verdict"] == "pass"
    with h.state.db.session() as db:
        calls = list(db.execute(select(ToolCallRow).where(ToolCallRow.run_id == run_id)).scalars())
    assert [c.tool for c in calls] == ["python", "python"]


async def test_compact_coder_can_read_pdf_and_render_requested_workbook(harness: Harness) -> None:
    import openpyxl
    import pymupdf

    h = harness
    h.state.config.execution.compact_planning = True
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Basic 10; freight 2")
        document.save(h.workspace / "source.pdf")
    h.mock.add_canned(
        {
            "json": {
                "tasks": [
                    {
                        "title": "Compare",
                        "instruction": "Read and render",
                        "role": "coder",
                        "output_file": "comparison.xlsx",
                    }
                ]
            }
        },
        role="planner",
    )
    h.mock.add_canned_sequence(
        [
            step("read_pages", {"path": "source.pdf", "pages": "1"}),
            step(
                "render_document",
                {
                    "type": "xlsx",
                    "schema_id": "data_table",
                    "out_path": "comparison.xlsx",
                    "data_json": {
                        "title": "Comparison",
                        "sheets": [
                            {"columns": ["Basic", "Freight", "Total"], "rows": [[10, 2, "=A2+B2"]]}
                        ],
                    },
                },
            ),
            finish("Created comparison", artifacts=["comparison.xlsx"]),
        ],
        role="executor",
        contains="Task t1:",
    )
    h.mock.add_canned_sequence(
        [
            {
                "json": {
                    "criteria": [
                        {
                            "requirement": "Read and render",
                            "evidence": "PDF read and numeric workbook rendered",
                            "status": "met",
                        }
                    ],
                    "all_requirements_covered": True,
                }
            }
        ],
        role="reviewer",
    )
    run_id = await run_and_wait(h, goal="Read page 1 of source.pdf and render comparison.xlsx.")
    assert run_status(h, run_id) == "done", str(h.notes("verify.result"))
    with h.state.db.session() as db:
        calls = list(db.execute(select(ToolCallRow).where(ToolCallRow.run_id == run_id)).scalars())
    assert [(c.tool, c.status) for c in calls] == [
        ("read_pages", "done"),
        ("render_document", "done"),
    ]
    sheet = openpyxl.load_workbook(h.workspace / "comparison.xlsx").active
    assert sheet["A2"].value == 10 and sheet["C2"].value == "=A2+B2"


async def test_ladder_reaches_replan(harness: Harness) -> None:
    h = harness
    h.mock.add_canned(goal_spec_for("hello.py"), role="planner", contains="Goal:")
    plan_v1 = two_task_plan()
    task_v1 = dict(plan_v1["json"]["tasks"][0])
    task_v1["budget"] = dict(task_v1["budget"], max_retries=3)
    plan_v1["json"]["tasks"] = [task_v1]
    plan_v1["json"]["edges"] = []
    plan_v2 = two_task_plan()
    recovery = dict(plan_v2["json"]["tasks"][0])
    recovery["title"] = "Recovery task"
    recovery["intent"] = "Write hello.py directly, minimal content"
    plan_v2["json"]["tasks"] = [recovery]
    plan_v2["json"]["edges"] = []

    h.mock.add_canned_sequence([plan_v1, plan_v2], role="planner", contains="GoalSpec:")
    # t1 always finishes without producing the file → fails through the whole ladder.
    h.mock.add_canned(finish("still nothing written"), role="executor", contains="Task t1:")
    # best-of-N re-drafts (executor role, no task card, "Draft" marker) also fail review …
    h.mock.add_canned(finish("re-draft, still nothing"), role="executor", contains="Draft 1/")
    h.mock.add_canned(finish("re-draft, still nothing"), role="executor", contains="Draft 2/")
    h.mock.add_canned(finish("re-draft, still nothing"), role="executor", contains="Draft 3/")
    # … the recovery task from the replan succeeds.
    h.mock.add_canned_sequence(
        [
            step("write_file", {"path": "hello.py", "content": "# saved\n"}),
            finish("hello.py written by recovery", artifacts=["hello.py"]),
        ],
        role="executor",
        contains="Recovery task",
    )
    run_id = await run_and_wait(h)
    assert run_status(h, run_id) in ("done", "done_with_gaps")
    assert (h.workspace / "hello.py").exists()
    rungs = [e["rung"] for e in h.notes("escalation")]
    assert "raise_effort" in rungs and "best_of_n" in rungs and "replan" in rungs
    with h.state.db.session() as s:
        tasks = {
            row.id.split(":")[1]: row
            for row in s.execute(select(TaskRow).where(TaskRow.run_id == run_id)).scalars()
        }
    assert tasks["t1"].status == "done"  # the replan reused the id with the recovery definition


async def test_loop_guard_blocks_repeat_calls(harness: Harness) -> None:
    h = harness
    h.mock.add_canned(goal_spec_for("hello.py"), role="planner", contains="Goal:")
    plan = two_task_plan()
    plan["json"]["tasks"] = [dict(plan["json"]["tasks"][0])]
    plan["json"]["tasks"][0]["acceptance"] = [
        {"kind": "rubric", "rubric_id": "default", "min_score": 50}
    ]
    plan["json"]["edges"] = []
    h.mock.add_canned(plan, role="planner", contains="GoalSpec:")
    (h.workspace / "a.txt").write_text("content")
    h.mock.add_canned_sequence(
        [
            step("read_file", {"path": "a.txt"}),
            step("read_file", {"path": "a.txt"}),  # identical → guard, not executed
            finish("read the file", artifacts=[]),
        ],
        role="executor",
        contains="Task t1:",
    )
    run_id = await run_and_wait(h)
    assert run_status(h, run_id) == "done"
    with h.state.db.session() as s:
        calls = list(
            s.execute(
                select(ToolCallRow).where(
                    ToolCallRow.run_id == run_id, ToolCallRow.tool == "read_file"
                )
            ).scalars()
        )
        assert len(calls) == 1  # second one never executed
        steps = list(
            s.execute(select(StepRow).where(StepRow.run_id == run_id).order_by(StepRow.n)).scalars()
        )
        assert any("already ran this" in (st.observation or "") for st in steps)


async def test_budget_exhaustion_forces_finish_with_gaps(harness: Harness) -> None:
    h = harness
    h.mock.add_canned(goal_spec_for("hello.py"), role="planner", contains="Goal:")
    plan = two_task_plan()
    task = dict(plan["json"]["tasks"][0])
    task["budget"] = {"max_steps": 2, "max_tokens": 20000, "max_seconds": 120, "max_retries": 0}
    plan["json"]["tasks"] = [task]
    plan["json"]["edges"] = []
    h.mock.add_canned(plan, role="planner", contains="GoalSpec:")
    h.mock.add_canned_sequence(
        [
            step("write_file", {"path": "f1.txt", "content": "1"}),
            step("write_file", {"path": "f2.txt", "content": "2"}),
        ],
        role="executor",
        contains="Task t1:",
    )
    h.mock.add_canned(
        finish("partial work only: two files written, hello.py never produced"),
        role="executor",
        contains="ran out of budget",
    )
    run_id = await run_and_wait(h)
    assert run_status(h, run_id) == "done_with_gaps"
    finished = h.notes("run.finished")[-1]
    assert "Gaps" in finished["summary"] or finished["status"] == "done_with_gaps"
    with h.state.db.session() as s:
        task_row = s.execute(select(TaskRow).where(TaskRow.run_id == run_id)).scalar_one()
        assert task_row.status == "partial"


async def test_delegate_spawns_child_task(harness: Harness) -> None:
    h = harness
    h.mock.add_canned(goal_spec_for("hello.py"), role="planner", contains="Goal:")
    plan = two_task_plan()
    plan["json"]["tasks"] = [dict(plan["json"]["tasks"][0])]
    plan["json"]["tasks"][0]["role"] = "analyst"
    plan["json"]["tasks"][0]["acceptance"] = [
        {"kind": "rubric", "rubric_id": "default", "min_score": 50}
    ]
    plan["json"]["edges"] = []
    h.mock.add_canned(plan, role="planner", contains="GoalSpec:")
    h.mock.add_canned_sequence(
        [
            step(
                "delegate",
                {
                    "title": "Child extraction",
                    "intent": "extract the numbers",
                    "role": "data_engineer",
                    "acceptance": [],
                },
            ),
            finish("used child result", artifacts=[]),
        ],
        role="executor",
        contains="Task t1:",
    )
    h.mock.add_canned_sequence(
        [finish("child done: extracted 3 numbers")],
        role="executor",
        contains="Child extraction",
    )
    run_id = await run_and_wait(h)
    assert run_status(h, run_id) == "done"
    with h.state.db.session() as s:
        ids = [
            t.id.split(":", 1)[1]
            for t in s.execute(select(TaskRow).where(TaskRow.run_id == run_id)).scalars()
        ]
    assert "t1.c1" in ids


async def test_plan_mode_stops_after_planning(harness: Harness) -> None:
    h = harness
    h.mock.add_canned(GOAL_SPEC, role="planner", contains="Goal:")
    h.mock.add_canned(two_task_plan(), role="planner", contains="GoalSpec:")
    run_id = await run_and_wait(h, mode="plan")
    assert run_status(h, run_id) == "planned"
    with h.state.db.session() as s:
        run = s.get(RunRow, run_id)
        assert run is not None and run.plan is not None
        assert len(run.plan["tasks"]) == 2
        steps = list(s.execute(select(StepRow).where(StepRow.run_id == run_id)).scalars())
        assert steps == []  # nothing executed


async def test_ask_mode_waits_for_plan_approval(harness: Harness) -> None:
    h = harness
    h.mock.add_canned(GOAL_SPEC, role="planner", contains="Goal:")
    h.mock.add_canned(two_task_plan(), role="planner", contains="GoalSpec:")
    h.mock.add_canned_sequence(
        [
            step("write_file", {"path": "hello.py", "content": "print('x')\n"}),
            finish("done", artifacts=["hello.py"]),
        ],
        role="executor",
        contains="Task t1:",
    )
    h.mock.add_canned_sequence(
        [
            step("write_file", {"path": "report.md", "content": "r\n"}),
            finish("done", artifacts=["report.md"]),
        ],
        role="executor",
        contains="Task t2:",
    )

    async def approve_everything() -> None:
        approved_plan = False
        for _ in range(600):
            while not h.conn.outbound.empty():
                frame = h.conn.outbound.get_nowait()
                h.events.append(frame)
                if frame.get("method") == "permission.request":
                    request_id = frame["params"]["request_id"]
                    if request_id.startswith("plan:"):
                        approved_plan = True
                        h.state.conductor.resolve_plan_approval(
                            run_id, "once", request_id=request_id
                        )
                    else:
                        h.state.tools.broker.resolve(request_id, "always", None)
            await asyncio.sleep(0.02)
            if approved_plan and not h.state.conductor.list_active():
                return

    session_id = await h.start_session("ask")
    run_id = await h.state.conductor.start_run(session_id, "greeting please", [], "ask")
    approver = asyncio.create_task(approve_everything())
    await h.state.conductor.wait_for_run(run_id, timeout_s=60)
    approver.cancel()
    assert run_status(h, run_id) == "done"
    assert (h.workspace / "hello.py").exists()


async def test_crash_and_resume_no_duplicate_side_effects(harness: Harness) -> None:
    h = harness
    h.mock.add_canned(GOAL_SPEC, role="planner", contains="Goal:")
    plan = two_task_plan()
    h.mock.add_canned(plan, role="planner", contains="GoalSpec:")
    t1_steps = [
        step("write_file", {"path": "hello.py", "content": "print('namaste')\n"}),
        finish("hello written", artifacts=["hello.py"]),
    ]
    t2_steps = [
        step("write_file", {"path": "report.md", "content": "# r\n"}),
        finish("report written", artifacts=["report.md"]),
    ]
    h.mock.add_canned_sequence(list(t1_steps), role="executor", contains="Task t1:")
    h.mock.add_canned_sequence(list(t2_steps), role="executor", contains="Task t2:")

    session_id = await h.start_session("auto")
    run_id = await h.state.conductor.start_run(session_id, "make greeting and report", [], "auto")

    # Crash the driver the moment the first side effect lands.
    for _ in range(1000):
        await asyncio.sleep(0.02)
        if (h.workspace / "hello.py").exists():
            break
    active = h.state.conductor._active.get(run_id)
    assert active is not None, "run finished before the crash point"
    active.driver.cancel()
    await asyncio.sleep(0.05)

    # Re-script the engine for the resumed run (fresh process ≈ fresh scripts; the cache
    # and idempotency records live in the DB and drive replay).
    h.mock.add_canned_sequence(list(t1_steps), role="executor", contains="Task t1:")
    h.mock.add_canned_sequence(list(t2_steps), role="executor", contains="Task t2:")

    await h.state.conductor.resume_run(run_id)
    await h.state.conductor.wait_for_run(run_id, timeout_s=60)
    assert run_status(h, run_id) in ("done", "done_with_gaps")
    assert (h.workspace / "hello.py").read_text() == "print('namaste')\n"
    assert (h.workspace / "report.md").exists()
    with h.state.db.session() as s:
        writes = list(
            s.execute(
                select(ToolCallRow).where(
                    ToolCallRow.run_id == run_id,
                    ToolCallRow.tool == "write_file",
                    ToolCallRow.status == "done",
                )
            ).scalars()
        )
        # hello.py write executed once; the resumed attempt replayed the recorded result.
        hello_writes = [w for w in writes if w.args.get("path") == "hello.py"]
        assert len(hello_writes) == 1


async def test_structural_critic_catches_missing_deliverable(harness: Harness) -> None:
    from yantra_server.agents import AgentRoster
    from yantra_server.conductor.planner import _structural_findings
    from yantra_server.conductor.types import GoalSpec, Plan

    h = harness
    roster = AgentRoster(h.state.loaded.assets_dir / "agents")
    plan = Plan.model_validate(two_task_plan()["json"])
    spec = GoalSpec.model_validate(GOAL_SPEC["json"])
    assert _structural_findings(plan, spec, roster) == []

    plan_bad = Plan.model_validate(two_task_plan()["json"])
    plan_bad.tasks = plan_bad.tasks[:1]
    findings = _structural_findings(plan_bad, spec, roster)
    assert any("report.md" in f for f in findings)

    plan_cycle = Plan.model_validate(two_task_plan()["json"])
    plan_cycle.edges = [["t1", "t2"], ["t2", "t1"]]
    assert any("cycle" in f for f in _structural_findings(plan_cycle, spec, roster))
