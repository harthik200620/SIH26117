"""Regression: small models must not duplicate a single deliverable across tasks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yantra_server.conductor.planner import CompactPlan, CompactTask, _compact_plan
from yantra_server.conductor.types import GoalSpec
from yantra_server.config import YantraConfig


async def test_long_display_title_does_not_discard_full_task_instruction(monkeypatch):
    from tests.helpers import make_state

    state = make_state()
    try:
        instruction = "Read source.csv and create result.xlsx using the complete controlled rule."
        draft = CompactPlan(
            tasks=[
                CompactTask(
                    title="Detailed title " * 15,
                    instruction=instruction,
                    role="writer",
                    output_file="result.xlsx",
                )
            ]
        )
        monkeypatch.setattr(
            "yantra_server.conductor.planner._draft_compact", AsyncMock(return_value=draft)
        )
        plan = await _compact_plan(state, GoalSpec(objective=instruction))
        assert len(plan.tasks[0].title) == 100
        assert plan.tasks[0].intent.startswith(instruction)
        assert plan.tasks[0].outputs[0].name == "result.xlsx"
        assert "maxLength" not in CompactTask.model_json_schema()["properties"]["title"]
    finally:
        state.db.dispose()




def test_compact_instruction_accepts_realistic_source_and_output_constraints() -> None:
    instruction = "Read the source and preserve its units and revision. " * 15
    task = CompactTask(
        title="Review", instruction=instruction, role="writer", output_file="review.docx"
    )
    assert task.instruction == instruction


async def test_single_deliverable_steps_are_combined_and_constraints_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = CompactPlan(
        tasks=[
            CompactTask(
                title="Read documents",
                instruction="Read inputs",
                role="analyst",
                output_file="report.json",
            ),
            CompactTask(
                title="Calculate", instruction="Calculate results", role="analyst", output_file=""
            ),
        ]
    )
    monkeypatch.setattr(
        "yantra_server.conductor.planner._draft_compact", AsyncMock(return_value=draft)
    )
    objective = (
        "Read and calculate. " + "Preserve this constraint. " * 50 + "Do not invent OEM limits."
    )
    plan = await _compact_plan(
        SimpleNamespace(config=YantraConfig()), GoalSpec(objective=objective)
    )
    assert len(plan.tasks) == 1
    assert plan.tasks[0].outputs[0].name == "report.json"
    assert objective in plan.tasks[0].intent
    assert plan.edges == []


async def test_multiple_deliverables_retain_workflow_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = CompactPlan(
        tasks=[
            CompactTask(
                title="Prepare data",
                instruction="Extract measurements",
                role="analyst",
                output_file="data.csv",
            ),
            CompactTask(
                title="Report", instruction="Use data.csv", role="writer", output_file="report.md"
            ),
        ]
    )
    monkeypatch.setattr(
        "yantra_server.conductor.planner._draft_compact", AsyncMock(return_value=draft)
    )
    plan = await _compact_plan(
        SimpleNamespace(config=YantraConfig()), GoalSpec(objective="Create data.csv and report.md")
    )
    assert len(plan.tasks) == 2
    assert plan.edges == [["t1", "t2"]]


async def test_invented_scratch_files_do_not_expand_one_requested_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = CompactPlan(
        tasks=[
            CompactTask(
                title="Read input",
                instruction="Read missing.txt",
                role="analyst",
                output_file="missing.txt",
            ),
            CompactTask(
                title="Calculate",
                instruction="Write scratch.txt",
                role="writer",
                output_file="scratch.txt",
            ),
            CompactTask(
                title="Save", instruction="Save output", role="writer", output_file="result.json"
            ),
        ]
    )
    monkeypatch.setattr(
        "yantra_server.conductor.planner._draft_compact", AsyncMock(return_value=draft)
    )
    objective = "Use the Python tool to calculate the median of 4, 7, 12 and write result.json."
    plan = await _compact_plan(
        SimpleNamespace(config=YantraConfig()), GoalSpec(objective=objective)
    )
    assert len(plan.tasks) == 1
    task = plan.tasks[0]
    assert task.role == "coder"
    assert task.outputs[0].name == "result.json"
    assert "missing.txt" not in task.intent
    assert objective in task.intent
    assert any(c.kind == "tool_succeeded" for c in task.acceptance)


async def test_negative_python_instruction_does_not_require_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = CompactPlan(
        tasks=[CompactTask(title="Answer", instruction="Explain", role="writer", output_file="")]
    )
    monkeypatch.setattr(
        "yantra_server.conductor.planner._draft_compact", AsyncMock(return_value=draft)
    )
    plan = await _compact_plan(
        SimpleNamespace(config=YantraConfig()),
        GoalSpec(objective="Do not use Python to explain this calculation."),
    )
    assert all(c.kind != "tool_succeeded" for c in plan.tasks[0].acceptance)


async def test_explicit_read_paths_are_not_deliverables(monkeypatch):
    draft = CompactPlan(
        tasks=[
            CompactTask(
                title="Read CSV", instruction="Read offers", role="writer", output_file="offers.csv"
            ),
            CompactTask(
                title="Read PDF", instruction="Read rules", role="writer", output_file="rules.pdf"
            ),
            CompactTask(
                title="Render",
                instruction="Render workbook",
                role="coder",
                output_file="result.xlsx",
            ),
        ]
    )
    monkeypatch.setattr(
        "yantra_server.conductor.planner._draft_compact", AsyncMock(return_value=draft)
    )
    objective = "First call read_file with path offers.csv. Then call read_pages with path rules.pdf and pages 1. Create result.xlsx."
    plan = await _compact_plan(
        SimpleNamespace(config=YantraConfig()), GoalSpec(objective=objective)
    )
    assert len(plan.tasks) == 1 and plan.edges == []
    assert [o.name for o in plan.tasks[0].outputs] == ["result.xlsx"]
    assert plan.tasks[0].intent.endswith(objective)


async def test_explicit_read_then_edit_keeps_requested_output(monkeypatch):
    draft = CompactPlan(
        tasks=[
            CompactTask(
                title="Edit input", instruction="Edit", role="writer", output_file="input.csv"
            ),
            CompactTask(
                title="Report", instruction="Report", role="writer", output_file="report.md"
            ),
        ]
    )
    monkeypatch.setattr(
        "yantra_server.conductor.planner._draft_compact", AsyncMock(return_value=draft)
    )
    plan = await _compact_plan(
        SimpleNamespace(config=YantraConfig()),
        GoalSpec(objective="Read input.csv. Update input.csv and create report.md."),
    )
    assert len(plan.tasks) == 2
    assert [o.name for task in plan.tasks for o in task.outputs] == ["input.csv", "report.md"]
