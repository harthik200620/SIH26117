"""Demo scenarios (SPEC §20.2) as scripted MockEngine drives of the real pipeline.

On a GPU host these run on the real brain model straight from the goal text. On the
GPU-less profile the model's *reasoning* is scripted here, but every tool call is real:
the corpus is really searched, charts/reports/spreadsheets are really rendered, code is
really executed in the sandbox, and delegate really fans out. So these prove the harness,
tools, knowledge plane, vision and rendering end-to-end — everything except the model.

Each scenario registers canned responses keyed by (role, task-card substring). Task cards
are `Task {id}: {title}` and delegate children get deterministic ids `{parent}.c{n}`, so
the whole plan — including fan-out — is scriptable with static args.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from yantra_server.gateway.engines.mock import MockEngine

# --------------------------------------------------------------------- builders

REVIEW_PASS = {"json": {"score": 92, "failures": [], "fix_instructions": [], "verdict": "pass"}}
CRITIC_OK = {"json": {"ok": True, "findings": []}}


def _step(tool: str, args: dict[str, Any], thought: str = "next") -> dict[str, Any]:
    return {"json": {"thought": thought, "action": {"tool": tool, "args": args}}}


def _finish(
    summary: str,
    artifacts: list[str] | None = None,
    claims: list[dict[str, Any]] | None = None,
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
                    "self_check": {"score": 88, "notes": ""},
                },
            },
        }
    }


def _goal_spec(objective: str, deliverables: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "json": {
            "objective": objective,
            "deliverables": [{"type": t, "name": n} for t, n in deliverables],
            "constraints": [],
            "success_criteria": ["deliverables exist and are cited"],
            "context_refs": [],
            "assumptions": ["corpus collection is indexed"],
            "open_questions": [],
        }
    }


def _task(
    task_id: str,
    title: str,
    intent: str,
    role: str,
    outputs: list[tuple[str, str]],
    deps: list[str] | None = None,
    acceptance: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if acceptance is None:
        acceptance = (
            [{"kind": "file_exists", "path": outputs[0][0]}]
            if outputs
            else [{"kind": "rubric", "rubric_id": "default", "min_score": 80}]
        )
    return {
        "id": task_id,
        "title": title,
        "intent": intent,
        "role": role,
        "inputs": deps or [],
        "outputs": [{"name": n, "type": t} for n, t in outputs],
        "acceptance": acceptance,
        "budget": {"max_steps": 8, "max_tokens": 40000, "max_seconds": 180, "max_retries": 2},
    }


def _plan(tasks: list[dict[str, Any]], edges: list[list[str]], rationale: str) -> dict[str, Any]:
    return {"json": {"tasks": tasks, "edges": edges, "rationale": rationale, "version": 1}}


def _register_common(mock: MockEngine) -> None:
    mock.add_canned({"json": {"route": "workflow"}}, role="planner", contains="Request routing:")
    mock.add_canned(CRITIC_OK, role="utility", contains="GoalSpec deliverables")
    mock.add_canned(REVIEW_PASS, role="reviewer")


@dataclass
class Scenario:
    id: str
    title: str
    goal: str
    mode: str
    expect_files: list[str]
    build: Callable[[MockEngine, ScenarioContext], None]
    copy_paths: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)


@dataclass
class ScenarioContext:
    collection: str = "demo"
    pid_image: str = ""
    rulepack: str = "knowledge/pid/rules.yaml"
    legacy_src: str = "tank_gauging.py"


# --------------------------------------------------------------------- scenario 2


def _build_pump_reliability(mock: MockEngine, ctx: ScenarioContext) -> None:
    _register_common(mock)
    mock.add_canned(
        _goal_spec(
            "Investigate P-3101A repeated failures and produce a cited root-cause pack",
            [("docx", "root_cause_report.docx"), ("xlsx", "equipment_register.xlsx")],
        ),
        role="planner",
        contains="Goal:",
    )
    plan = _plan(
        tasks=[
            _task(
                "t1",
                "Investigate failure history",
                "Search logs, manuals and SOPs for P-3101A",
                "analyst",
                [],
            ),
            _task(
                "t2",
                "Plot MTBF trend",
                "Render an MTBF-over-time chart",
                "data_engineer",
                [("mtbf.png", "png")],
                deps=["t1"],
            ),
            _task(
                "t3",
                "Write root-cause report",
                "Produce the cited DOCX root-cause report",
                "writer",
                [("root_cause_report.docx", "docx")],
                deps=["t1", "t2"],
            ),
            _task(
                "t4",
                "Build equipment register",
                "Produce the XLSX equipment register",
                "writer",
                [("equipment_register.xlsx", "xlsx")],
                deps=["t1"],
            ),
            _task(
                "t5",
                "Draft work order",
                "Draft the corrective work order",
                "writer",
                [("work_order.md", "md")],
                deps=["t3"],
            ),
        ],
        edges=[["t1", "t2"], ["t1", "t3"], ["t2", "t3"], ["t1", "t4"], ["t3", "t5"]],
        rationale="investigate -> quantify -> report -> register -> act",
    )
    mock.add_canned(plan, role="planner", contains="GoalSpec:")

    mock.add_canned_sequence(
        [
            _step(
                "search_knowledge",
                {
                    "query": "P-3101A seal failure vibration history",
                    "collections": [ctx.collection],
                    "k": 8,
                },
            ),
            _finish(
                "Found seal-flush and vibration history for P-3101A.",
                claims=[
                    {"text": "P-3101A shows repeated mechanical seal failures", "kind": "fact"}
                ],
            ),
        ],
        role="executor",
        contains="Task t1:",
    )
    mock.add_canned_sequence(
        [
            _step(
                "render_chart",
                {
                    "spec": {
                        "type": "line",
                        "title": "P-3101A MTBF trend",
                        "x_label": "Quarter",
                        "y_label": "MTBF (days)",
                        "series": [
                            {"name": "MTBF", "x": ["Q1", "Q2", "Q3", "Q4"], "y": [180, 120, 90, 60]}
                        ],
                    },
                    "out_path": "mtbf.png",
                },
            ),
            _finish("MTBF trend rendered, declining 180 to 60 days.", artifacts=["mtbf.png"]),
        ],
        role="executor",
        contains="Task t2:",
    )
    report_data = {
        "title": "P-3101A Root-Cause Report",
        "metadata": {"equipment": "P-3101A", "unit": "Unit 3 — LPG Recovery"},
        "executive_summary": "Repeated mechanical seal failures traced to seal-flush plan gaps.",
        "sections": [
            {
                "heading": "Findings",
                "paragraphs": ["MTBF has declined from 180 to 60 days over four quarters."],
            }
        ],
        "findings": [
            {
                "id": "F1",
                "severity": "high",
                "statement": "Seal-flush plan inadequate for service",
                "evidence": "log trend",
            }
        ],
        "recommendations": [
            {"id": "R1", "text": "Upgrade to API Plan 53B seal flush", "priority": "high"}
        ],
        "appendix": {"citations": [], "assumptions": [], "unverified_claims": []},
    }
    mock.add_canned_sequence(
        [
            _step(
                "render_document",
                {
                    "type": "docx",
                    "schema_id": "report",
                    "data_json": json.dumps(report_data),
                    "out_path": "root_cause_report.docx",
                },
            ),
            _finish("Root-cause report written.", artifacts=["root_cause_report.docx"]),
        ],
        role="executor",
        contains="Task t3:",
    )
    register_data = {
        "title": "Unit 3 Rotating Equipment Register",
        "equipment": [
            {"tag": "P-3101A", "service": "LPG feed pump", "status": "repeated failures"},
            {"tag": "P-3101B", "service": "LPG feed pump (spare)", "status": "standby"},
        ],
    }
    mock.add_canned_sequence(
        [
            _step(
                "render_document",
                {
                    "type": "xlsx",
                    "schema_id": "equipment_list",
                    "data_json": json.dumps(register_data),
                    "out_path": "equipment_register.xlsx",
                },
            ),
            _finish("Equipment register written.", artifacts=["equipment_register.xlsx"]),
        ],
        role="executor",
        contains="Task t4:",
    )
    work_order = {
        "title": "Corrective WO — P-3101A seal flush upgrade",
        "equipment": "P-3101A",
        "priority": "high",
        "description": "Upgrade mechanical seal flush to API Plan 53B and re-baseline vibration.",
        "steps": [
            "Isolate and drain",
            "Replace seal + flush plan",
            "Re-align",
            "Baseline vibration",
        ],
        "safety_notes": [
            "LPG service — verify zero energy and gas-free before breaking containment"
        ],
    }
    mock.add_canned_sequence(
        [
            _step(
                "render_document",
                {
                    "type": "md",
                    "schema_id": "work_order_draft",
                    "data_json": json.dumps(work_order),
                    "out_path": "work_order.md",
                },
            ),
            _finish("Work order drafted.", artifacts=["work_order.md"]),
        ],
        role="executor",
        contains="Task t5:",
    )


# --------------------------------------------------------------------- scenario 3


def _build_pid_review(mock: MockEngine, ctx: ScenarioContext) -> None:
    _register_common(mock)
    mock.add_canned(
        _goal_spec("Review the P&ID against pump and PSV checklists", [("md", "pid_analysis.md")]),
        role="planner",
        contains="Goal:",
    )
    plan = _plan(
        tasks=[
            _task(
                "t1",
                "Analyse the P&ID",
                "Extract the graph and coverage from the drawing",
                "drawing_engineer",
                [("pid_analysis.md", "md")],
            )
        ],
        edges=[],
        rationale="analyse the drawing and record coverage",
    )
    mock.add_canned(plan, role="planner", contains="GoalSpec:")
    # pid_rules_check/annotate need the runtime graph_ref, which static scripting can't supply;
    # the dedicated `pid` eval suite validates rules + deviation recall directly. Here we prove
    # the real vision pipeline runs on the generated sheet and we persist its counts.
    # drawing_engineer runs under the 'vision' model role (see agents/drawing_engineer.yaml).
    mock.add_canned_sequence(
        [
            _step("pid_analyze", {"path": ctx.pid_image}),
            _step(
                "write_file",
                {
                    "path": "pid_analysis.md",
                    "content": "# P&ID analysis\nGraph extracted; see pid suite for rule findings.\n",
                },
            ),
            _finish("P&ID analysed; graph and coverage extracted.", artifacts=["pid_analysis.md"]),
        ],
        role="vision",
        contains="Task t1:",
    )


# --------------------------------------------------------------------- scenario 4

_FIXED_MODULE = '''"""Tank gauging (unit bug fixed: level returned in metres)."""

from __future__ import annotations

import argparse


def tank_level(raw_ma: float) -> float:
    """Convert a 4-20 mA signal to level in metres (0-10 m span)."""
    span_m = 10.0
    return (raw_ma - 4) / 16 * span_m


def main() -> None:
    parser = argparse.ArgumentParser(description="Tank level from a 4-20 mA reading")
    parser.add_argument("ma", type=float, help="loop current in mA")
    args = parser.parse_args()
    print(f"{tank_level(args.ma):.3f} m")


if __name__ == "__main__":
    main()
'''

_SELFTEST = (
    "import sys; sys.path.insert(0, '.')\n"
    "from tank_gauging_fixed import tank_level\n"
    "assert abs(tank_level(12) - 5.0) < 1e-9, tank_level(12)\n"
    "assert abs(tank_level(4) - 0.0) < 1e-9\n"
    "assert abs(tank_level(20) - 10.0) < 1e-9\n"
    "print('OK: 3 assertions passed')\n"
)


def _build_code_modernisation(mock: MockEngine, ctx: ScenarioContext) -> None:
    _register_common(mock)
    mock.add_canned(
        _goal_spec(
            "Modernise tank_gauging.py: fix the unit bug, add types + CLI + tests",
            [("code", "tank_gauging_fixed.py"), ("md", "change_summary.md")],
        ),
        role="planner",
        contains="Goal:",
    )
    plan = _plan(
        tasks=[
            _task(
                "t1",
                "Rewrite module",
                "Fix the unit bug, add type hints and a CLI",
                "coder",
                [("tank_gauging_fixed.py", "code")],
            ),
            _task(
                "t2",
                "Run the tests",
                "Execute the module self-test in the sandbox",
                "coder",
                [],
                deps=["t1"],
                acceptance=[{"kind": "file_exists", "path": "tank_gauging_fixed.py"}],
            ),
            _task(
                "t3",
                "Write change summary",
                "Produce the code_change_summary",
                "writer",
                [("change_summary.md", "md")],
                deps=["t2"],
            ),
        ],
        edges=[["t1", "t2"], ["t2", "t3"]],
        rationale="fix -> verify -> document",
    )
    mock.add_canned(plan, role="planner", contains="GoalSpec:")
    mock.add_canned_sequence(
        [
            _step("write_file", {"path": "tank_gauging_fixed.py", "content": _FIXED_MODULE}),
            _finish(
                "Module rewritten: bug fixed, types + CLI added.",
                artifacts=["tank_gauging_fixed.py"],
            ),
        ],
        role="executor",
        contains="Task t1:",
    )
    mock.add_canned_sequence(
        [
            _step("python", {"code": _SELFTEST}),
            _finish("Self-test passed (3 assertions)."),
        ],
        role="executor",
        contains="Task t2:",
    )
    summary_data = {
        "title": "tank_gauging modernisation",
        "files": ["tank_gauging_fixed.py"],
        "rationale": "Fixed the cm-to-m unit bug (removed the x100), added type hints and an argparse CLI.",
        "tests": "3 assertions on the 4/12/20 mA endpoints pass in the sandbox.",
        "risks": ["Downstream consumers expecting centimetres must be updated."],
    }
    mock.add_canned_sequence(
        [
            _step(
                "render_document",
                {
                    "type": "md",
                    "schema_id": "code_change_summary",
                    "data_json": json.dumps(summary_data),
                    "out_path": "change_summary.md",
                },
            ),
            _finish("Change summary written.", artifacts=["change_summary.md"]),
        ],
        role="executor",
        contains="Task t3:",
    )


# --------------------------------------------------------------------- scenario 5


def _build_consolidation(mock: MockEngine, ctx: ScenarioContext) -> None:
    _register_common(mock)
    mock.add_canned(
        _goal_spec(
            "Consolidate inspection reports into a register and draft follow-ups",
            [("xlsx", "inspection_register.xlsx"), ("md", "followup_email.md")],
        ),
        role="planner",
        contains="Goal:",
    )
    plan = _plan(
        tasks=[
            _task(
                "t1",
                "Fan out over reports",
                "Delegate extraction of each 2025 inspection report",
                "analyst",
                [],
                acceptance=[{"kind": "rubric", "rubric_id": "default", "min_score": 80}],
            ),
            _task(
                "t2",
                "Merge and draft",
                "Merge the register and draft the follow-up email",
                "writer",
                [("inspection_register.xlsx", "xlsx"), ("followup_email.md", "md")],
                deps=["t1"],
            ),
        ],
        edges=[["t1", "t2"]],
        rationale="delegate per report, then merge and draft",
    )
    mock.add_canned(plan, role="planner", contains="GoalSpec:")

    register_data = {
        "title": "2025 Inspection Register",
        "equipment": [
            {"tag": "V-3110", "item": "vessel wall thickness", "status": "OK"},
            {"tag": "E-3105", "item": "exchanger tubes", "status": "overdue"},
            {"tag": "T-3120", "item": "tank floor", "status": "due"},
        ],
        "findings": [{"tag": "E-3105", "finding": "inspection overdue", "action": "schedule"}],
    }
    email_data = {
        "to": "inspection.lead@example.internal",
        "subject": "Overdue inspection — E-3105",
        "body": "E-3105 tube inspection is overdue. Please schedule within two weeks.",
    }
    # t1 (analyst) fans out to three children (has delegate, not render).
    mock.add_canned_sequence(
        [
            _step(
                "delegate",
                {
                    "title": "Extract V-3110",
                    "intent": "Extract findings for V-3110",
                    "role": "analyst",
                },
            ),
            _step(
                "delegate",
                {
                    "title": "Extract E-3105",
                    "intent": "Extract findings for E-3105",
                    "role": "analyst",
                },
            ),
            _step(
                "delegate",
                {
                    "title": "Extract T-3120",
                    "intent": "Extract findings for T-3120",
                    "role": "analyst",
                },
            ),
            _finish("Extracted findings from three 2025 inspection reports via fan-out."),
        ],
        role="executor",
        contains="Task t1:",
    )
    # t2 (writer) renders the register and the follow-up email (has render_document).
    mock.add_canned_sequence(
        [
            _step(
                "render_document",
                {
                    "type": "xlsx",
                    "schema_id": "data_table",
                    "data_json": json.dumps(_register_as_table(register_data)),
                    "out_path": "inspection_register.xlsx",
                },
            ),
            _step(
                "render_document",
                {
                    "type": "md",
                    "schema_id": "email_draft",
                    "data_json": json.dumps(email_data),
                    "out_path": "followup_email.md",
                },
            ),
            _finish(
                "Register consolidated and follow-up drafted.",
                artifacts=["inspection_register.xlsx", "followup_email.md"],
            ),
        ],
        role="executor",
        contains="Task t2:",
    )
    # Every child (t1.c1, t1.c2, t1.c3) matches this role-only canned response.
    mock.add_canned(
        _finish("Findings extracted for the assigned equipment."),
        role="executor",
        contains="Intent: Extract findings",
    )


def _register_as_table(register: dict[str, Any]) -> dict[str, Any]:
    rows = [[e["tag"], e["item"], e["status"]] for e in register["equipment"]]
    return {
        "title": register["title"],
        "sheets": [{"caption": "Inspections", "columns": ["Tag", "Item", "Status"], "rows": rows}],
    }


# --------------------------------------------------------------------- registry

SCENARIOS: dict[str, Scenario] = {
    "s2_pump_reliability": Scenario(
        id="s2_pump_reliability",
        title="Pump reliability investigation",
        goal="Investigate why pump P-3101A keeps failing; produce a cited root-cause report (DOCX), "
        "an equipment register (XLSX) and a corrective work-order draft.",
        mode="auto",
        expect_files=[
            "root_cause_report.docx",
            "equipment_register.xlsx",
            "work_order.md",
            "mtbf.png",
        ],
        build=_build_pump_reliability,
        collections=["demo"],
    ),
    "s3_pid_review": Scenario(
        id="s3_pid_review",
        title="P&ID review",
        goal="Analyse P&ID 3-1201 and record the equipment/instrument counts and coverage.",
        mode="auto",
        expect_files=["pid_analysis.md"],
        build=_build_pid_review,
    ),
    "s4_code_modernisation": Scenario(
        id="s4_code_modernisation",
        title="Code modernisation",
        goal="Convert tank_gauging.py into a typed module with a CLI, fix the unit bug, test it, "
        "and produce a change summary.",
        mode="auto",
        expect_files=["tank_gauging_fixed.py", "change_summary.md"],
        build=_build_code_modernisation,
        copy_paths=["corpus/generated/small/docs/tank_gauging.py"],
    ),
    "s5_consolidation": Scenario(
        id="s5_consolidation",
        title="Consolidation at scale",
        goal="Consolidate the 2025 inspection reports into a register, flag overdue items, and "
        "draft the follow-up emails.",
        mode="auto",
        expect_files=["inspection_register.xlsx", "followup_email.md"],
        build=_build_consolidation,
        collections=["demo"],
    ),
}


def setup_scenario(mock: MockEngine, scenario_id: str, ctx: ScenarioContext) -> Scenario:
    """Reset the mock and register the scenario's scripted responses. Returns the Scenario."""
    if scenario_id not in SCENARIOS:
        raise KeyError(f"unknown scenario {scenario_id!r} (have {sorted(SCENARIOS)})")
    scenario = SCENARIOS[scenario_id]
    mock.reset()
    scenario.build(mock, ctx)
    return scenario
