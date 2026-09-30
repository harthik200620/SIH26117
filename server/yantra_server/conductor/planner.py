"""Planner + plan critic (SPEC §8.3): constrained Plan, cheap critique, ≤2 revisions."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from yantra_server.agents import AgentRoster
from yantra_server.gateway.engines.base import ChatMessage, Decoding
from yantra_server.gateway.service import ModelRequest
from yantra_server.observe.tracing import span

from .types import GoalSpec, Plan, PlanCritique

if TYPE_CHECKING:
    from yantra_server.state import AppState

MAX_REVISIONS = 2

PLANNER_INSTRUCTION = """Produce the Plan as JSON. Task ids are t1, t2, … in a sensible
execution order. Every task needs at least one machine-checkable acceptance check. Bind
each GoalSpec deliverable to exactly one task whose outputs include it."""

CRITIC_SYSTEM = """You are a plan critic. Check the plan mechanically and list ONLY real
defects, one line each:
- a GoalSpec deliverable no task produces
- a dependency cycle, or a task consuming an output no earlier task produces
- a task with no machine-checkable acceptance check
- a task assigned to an agent that does not exist in the roster
- an obviously oversized task (mixes discovery, analysis and deliverable production)
- planned verification/review tasks (verification is automatic; they must be removed)
If the plan is sound, ok=true with no findings. Do not suggest stylistic changes."""


async def make_plan(
    state: AppState,
    roster: AgentRoster,
    goal_spec: GoalSpec,
    *,
    failure_context: str | None = None,
    previous_plan: Plan | None = None,
) -> Plan:
    """Plan → critic → up to MAX_REVISIONS repair rounds; structural checks always enforced."""
    if state.config.execution.compact_planning:
        plan = await _compact_plan(state, goal_spec, failure_context=failure_context)
        structural = _structural_findings(plan, goal_spec, roster)
        if structural:
            raise ValueError("Invalid compact plan: " + "; ".join(structural))
        plan.version = previous_plan.version + 1 if previous_plan else 1
        return plan
    with span("plan", kind="plan") as sp:
        exemplars = _plan_exemplars(state, goal=goal_spec.objective)
        plan = await _draft_plan(
            state, roster, goal_spec, exemplars, failure_context, previous_plan
        )
        for round_no in range(MAX_REVISIONS + 1):
            structural = _structural_findings(plan, goal_spec, roster)
            critique = await _critique(state, roster, goal_spec, plan)
            findings = structural + [f for f in critique.findings if f not in structural]
            sp.set(f"round_{round_no}_findings", findings)
            if round_no == MAX_REVISIONS and structural:
                raise ValueError("Plan has unresolved structural errors: " + "; ".join(structural))
            if not findings or round_no == MAX_REVISIONS:
                break
            plan = await _draft_plan(
                state,
                roster,
                goal_spec,
                exemplars,
                failure_context,
                plan,
                critic_findings=findings,
            )
        plan.version = (previous_plan.version + 1) if previous_plan else 1
        sp.set("tasks", [t.id for t in plan.tasks])
        return plan


class CompactTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Presentation metadata must not discard an otherwise valid bounded plan.
    # Some grammar engines do not enforce string lengths; trim only the UI title.
    title: str
    instruction: str = Field(max_length=2000)
    role: str = Field(pattern="^(analyst|coder|writer|data_engineer|drawing_engineer)$")
    output_file: str = Field(max_length=200)


class CompactPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[CompactTask] = Field(min_length=1, max_length=4)


async def _compact_plan(
    state: AppState, goal: GoalSpec, failure_context: str | None = None
) -> Plan:
    from .types import (
        ArtifactSpec,
        Check,
        FileExistsCheck,
        PlanTask,
        RubricCheck,
        TaskBudget,
        ToolSucceededCheck,
    )

    # Only recognize an explicit leading instruction, not mentions in quoted documents
    # or negative instructions such as "Do not use Python".
    requires_python = bool(
        re.match(
            r"\s*(?:please\s+)?use\s+(?:the\s+)?python\s+(?:tool\b|to\b)", goal.objective, re.I
        )
    )

    simple = re.fullmatch(
        r"(?:write|create)\s+[\"'`]?([\w./-]+\.(?:txt|md|csv|json|py))[\"'`]?\s+containing\s+(?:exactly\s+)?(.+)",
        goal.objective.strip(),
        re.I | re.S,
    )
    if simple:
        draft = CompactPlan(
            tasks=[
                CompactTask(
                    title="Write the requested file",
                    instruction=goal.objective[:500],
                    role="writer",
                    output_file=simple[1],
                )
            ]
        )
    else:
        draft = await _draft_compact(state, goal, failure_context)
        outputs = {task.output_file for task in draft.tasks if task.output_file}
        source_only = set()
        for path in outputs:
            suffix = re.escape(path) + r"[\"'`]?(?![\w/\\-]|\.[\w])"
            read = re.search(
                r"\b(?:read_file|read_pages)\s+(?:with\s+)?path\s+[\"'`]?" + suffix,
                goal.objective,
                re.I,
            ) or re.search(r"\bread\s+[\"'`]?" + suffix, goal.objective, re.I)
            write = re.search(
                r"\b(?:write|create|save|produce|edit|update|rewrite|overwrite)\s+(?:to\s+)?[\"'`]?"
                + suffix,
                goal.objective,
                re.I,
            )
            if read and not write:
                source_only.add(path)
        # A planner may put read inputs in output_file. Explicitly read-only
        # paths cannot become completion criteria or authorize source rewrites.
        outputs -= source_only
        for task in draft.tasks:
            if task.output_file in source_only:
                task.output_file = ""
        grounded_outputs = {
            path
            for path in outputs
            if re.search(
                r"(?<![\w./\\-])" + re.escape(path) + r"(?![\w/\\-]|\.[\w])", goal.objective
            )
        }
        # Intermediate scratch files must not become invented user requirements.
        # When exactly one planned output is explicitly named, keep the full objective
        # in one task; its executor can still use intermediate files as needed.
        if len(grounded_outputs) == 1 and re.search(
            r"\b(?:write|create|save|produce)\s+(?:to\s+)?[\"'`]?"
            + re.escape(next(iter(grounded_outputs)))
            + r"(?![\w/\\-]|\.[\w])",
            goal.objective,
            re.I,
        ):
            outputs = grounded_outputs
        if len(draft.tasks) > 1 and len(outputs) == 1:
            # Reading/calculating/writing a single deliverable is one transaction.
            # Weak planners otherwise duplicate the objective across several agents.
            draft = CompactPlan(
                tasks=[
                    CompactTask(
                        title="Produce and check the requested deliverable",
                        instruction=goal.objective[:500],
                        role="coder"
                        if requires_python or any(t.role == "coder" for t in draft.tasks)
                        else "analyst",
                        output_file=next(iter(outputs)),
                    )
                ]
            )
    tasks = []
    for i, task in enumerate(draft.tasks):
        from pathlib import PurePosixPath, PureWindowsPath

        path = task.output_file
        if path and (
            PurePosixPath(path).is_absolute()
            or PureWindowsPath(path).is_absolute()
            or ".." in PurePosixPath(path.replace("\\", "/")).parts
        ):
            raise ValueError("Planned outputs must use workspace-relative paths")
        checks: list[Check] = [FileExistsCheck(path=path)] if path else [RubricCheck(min_score=80)]
        if requires_python and i == 0:
            task.role = "coder"
            checks.append(ToolSucceededCheck(tool="python"))
        tasks.append(
            PlanTask(
                id=f"t{i + 1}",
                title=task.title[:100],
                intent=task.instruction + "\nOriginal objective: " + goal.objective,
                role=task.role,
                outputs=[ArtifactSpec(name=path)] if path else [],
                acceptance=checks,
                budget=TaskBudget(
                    max_steps=state.config.execution.max_steps_per_task,
                    max_seconds=min(900, state.config.budgets.max_seconds),
                    max_tokens=12000,
                    max_retries=state.config.execution.max_task_retries,
                ),
            )
        )
    for deliverable in goal.deliverables:
        if not any(o.name == deliverable.name for t in tasks for o in t.outputs):
            tasks[-1].outputs.append(ArtifactSpec(name=deliverable.name, type=deliverable.type))
            tasks[-1].acceptance.append(FileExistsCheck(path=deliverable.name))
    return Plan(tasks=tasks, edges=[[tasks[i].id, tasks[i + 1].id] for i in range(len(tasks) - 1)])


async def _draft_compact(
    state: AppState, goal: GoalSpec, failure_context: str | None
) -> CompactPlan:
    result = await state.gateway.chat(
        ModelRequest(
            role="planner",
            schema_model=CompactPlan,
            messages=[
                ChatMessage(
                    role="system",
                    content=(
                        "Create the smallest execution plan. Usually ONE task. At most four tasks for complex goals. "
                        "Reading files, calculation and writing an output can be one task. Never add generic research, "
                        "review or verification tasks. Verification is automatic. Use writer for files/reports, coder for "
                        "code, analyst for calculations. The instruction MUST preserve the exact user requirements and "
                        "literal requested text. output_file is the requested filename or empty for a chat answer. "
                        "Do not invent input files or create tasks for intermediate scratch files. "
                        "If the user asks to execute Python, use coder even when the output is JSON. "
                        "Do not invent facts or unrelated work. Return only JSON."
                    ),
                ),
                ChatMessage(
                    role="user",
                    content=goal.model_dump_json()
                    + ("\nRepair these failures: " + failure_context if failure_context else ""),
                ),
            ],
            decoding=Decoding(temperature=0, max_tokens=900),
        )
    )
    draft = result.parsed
    assert isinstance(draft, CompactPlan)
    return draft


async def _draft_plan(
    state: AppState,
    roster: AgentRoster,
    goal_spec: GoalSpec,
    exemplars: str,
    failure_context: str | None,
    previous_plan: Plan | None,
    critic_findings: list[str] | None = None,
) -> Plan:
    planner = roster.get("planner")
    system = (planner.persona_text if planner else "") + "\n\n" + PLANNER_INSTRUCTION
    parts = [f"GoalSpec:\n{goal_spec.model_dump_json(indent=2)}"]
    parts.append(f"Agent roster:\n{roster.roster_text()}")
    manifest = state.tools.registry.manifest_text(state.tools.registry.names())
    parts.append(f"Tools that exist (summary):\n{manifest}")
    if exemplars:
        parts.append(f"Example plans for similar work:\n{exemplars}")
    if previous_plan is not None:
        parts.append(f"Previous plan (revise, do not restart):\n{previous_plan.model_dump_json()}")
    if failure_context:
        parts.append(f"Execution failures to address:\n{failure_context}")
    if critic_findings:
        parts.append("Critic findings to fix:\n" + "\n".join(f"- {f}" for f in critic_findings))
    result = await state.gateway.chat(
        ModelRequest(
            role="planner",
            messages=[
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content="\n\n".join(parts)),
            ],
            schema_model=Plan,
            decoding=Decoding(temperature=0.0, max_tokens=6000),
        )
    )
    plan = result.parsed
    assert isinstance(plan, Plan)
    return plan


async def _critique(
    state: AppState, roster: AgentRoster, goal_spec: GoalSpec, plan: Plan
) -> PlanCritique:
    result = await state.gateway.chat(
        ModelRequest(
            role="utility",
            messages=[
                ChatMessage(role="system", content=CRITIC_SYSTEM),
                ChatMessage(
                    role="user",
                    content=f"Roster:\n{roster.roster_text()}\n\n"
                    f"GoalSpec deliverables: {[d.name for d in goal_spec.deliverables]}\n\n"
                    f"Plan:\n{plan.model_dump_json(indent=2)}",
                ),
            ],
            schema_model=PlanCritique,
            decoding=Decoding(temperature=0.0, max_tokens=1000),
        )
    )
    critique = result.parsed
    assert isinstance(critique, PlanCritique)
    return critique


def _structural_findings(plan: Plan, goal_spec: GoalSpec, roster: AgentRoster) -> list[str]:
    """Deterministic checks the critic model cannot be trusted to always catch."""
    findings: list[str] = []
    ids = {t.id for t in plan.tasks}
    if len(ids) != len(plan.tasks):
        findings.append("duplicate task ids")
    known_agents = set(roster.names())
    for task in plan.tasks:
        if task.role not in known_agents:
            findings.append(f"{task.id}: unknown agent {task.role!r}")
        for input_ref in task.inputs:
            if input_ref.startswith("t") and input_ref not in ids:
                findings.append(f"{task.id}: input {input_ref} is not a task id in the plan")
    for edge in plan.edges:
        if len(edge) != 2 or edge[0] not in ids or edge[1] not in ids:
            findings.append(f"invalid edge {edge}")
    if _has_cycle(plan):
        findings.append("dependency cycle")
    produced = {o.name.lower() for t in plan.tasks for o in t.outputs}
    for deliverable in goal_spec.deliverables:
        tokens = {deliverable.name.lower()}
        if deliverable.path_hint:
            tokens.add(deliverable.path_hint.lower())
        if not any(any(tok in p or p in tok for tok in tokens) for p in produced):
            findings.append(f"deliverable {deliverable.name!r} is not produced by any task")
    return findings


def dependency_map(plan: Plan) -> dict[str, set[str]]:
    """task id → prerequisite ids, from `inputs` (t-refs) plus explicit edges."""
    deps: dict[str, set[str]] = {t.id: set() for t in plan.tasks}
    ids = set(deps)
    for task in plan.tasks:
        for input_ref in task.inputs:
            if input_ref in ids:
                deps[task.id].add(input_ref)
    for edge in plan.edges:
        if len(edge) == 2 and edge[0] in ids and edge[1] in ids:
            deps[edge[1]].add(edge[0])
    return deps


def _has_cycle(plan: Plan) -> bool:
    deps = dependency_map(plan)
    visited: dict[str, int] = {}

    def visit(node: str) -> bool:
        state_ = visited.get(node, 0)
        if state_ == 1:
            return True
        if state_ == 2:
            return False
        visited[node] = 1
        if any(visit(dep) for dep in deps.get(node, ())):
            return True
        visited[node] = 2
        return False

    return any(visit(t) for t in deps)


def _plan_exemplars(state: AppState, goal: str = "", limit: int = 2) -> str:
    """The `limit` exemplar plans most relevant to the goal (word overlap, name-order ties).

    Relevance matters more than it looks: small models copy whatever exemplar they see, so
    an analysis-shaped example on a trivial file-write goal produces a parroted 5-task plan.
    """
    exemplar_dir = state.loaded.assets_dir / "tools" / "fewshot" / "plans"
    if not exemplar_dir.is_dir():
        return ""
    goal_words = {w for w in re.findall(r"[a-z]+", goal.lower()) if len(w) > 2}
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for path in sorted(exemplar_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        exemplar_words = {
            w for w in re.findall(r"[a-z]+", str(data.get("goal", "")).lower()) if len(w) > 2
        }
        overlap = len(goal_words & exemplar_words) / (len(exemplar_words) or 1)
        scored.append((-overlap, path.stem, data))
    scored.sort()
    return "\n\n".join(
        f"# {data.get('goal', stem)}\n{json.dumps(data.get('plan', data))[:2500]}"
        for _, stem, data in scored[:limit]
    )
