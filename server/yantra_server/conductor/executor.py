"""Executor loop (SPEC §8.5): THINK → ACT → OBSERVE with guards, ledger, checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from yantra_server.agents import AgentDef, AgentRoster
from yantra_server.conductor.budgets import BudgetTracker
from yantra_server.db.models import LedgerEntryRow, StepRow
from yantra_server.gateway.engines.base import (
    ChatMessage,
    Constraint,
    Decoding,
    ReasoningDelta,
)
from yantra_server.gateway.service import ModelRequest
from yantra_server.gateway.structured import action_schema, schema_for, tighten
from yantra_server.observe.tracing import run_context, span
from yantra_server.sandbox import Sandbox
from yantra_server.tools.base import ToolContext
from yantra_server.tools.compaction import compact_observation
from yantra_server.tools.runtime import idempotency_key

from .context import ContextBuilder, StepView, describe_check, task_card_text, wrap_document
from .notify import RunNotifier
from .types import FinishArgs, LedgerState, PlanTask, StepDecision

if TYPE_CHECKING:
    from yantra_server.state import AppState

LOOP_GUARD_WINDOW = 6
CONSECUTIVE_ERROR_LIMIT = 3
LEDGER_EVERY_STEPS = 4

LEDGER_SYSTEM = """Update the progress ledger for a running task from its recent steps.
Keep entries short and factual. `stuck` is true only when the same approach failed
repeatedly and no untried approach is apparent. next_action_hint suggests ONE concrete
next action."""

HARNESS_TOOLS = ["read_artifact", "delegate", "ask_user", "calculate", "calculate_quantity"]


class TaskOutcome(BaseModel):
    status: Literal["done", "failed", "budget_exhausted", "cancelled"]
    finish: FinishArgs | None = None
    failure: str | None = None
    steps_used: int = 0


@dataclass
class TaskExecutor:
    state: AppState
    roster: AgentRoster
    notifier: RunNotifier
    run_id: str
    workspace: Path
    mode: str
    run_budget: BudgetTracker
    cancel_event: asyncio.Event
    sandbox: Sandbox
    context_builder: ContextBuilder = field(init=False)
    _last_model: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.context_builder = ContextBuilder(self.state.config.context)

    # ------------------------------------------------------------- public

    async def run_task(
        self,
        plan_task: PlanTask,
        *,
        attempt: int,
        ladder_rung: int,
        failure_context: str | None,
        pinned: list[str],
        sampling_temperature: float | None = None,
    ) -> TaskOutcome:
        agent = self.roster.get(plan_task.role) or self.roster.get("analyst")
        if agent is None:
            return TaskOutcome(status="failed", failure=f"no agent for role {plan_task.role!r}")
        with (
            run_context(run_id=self.run_id, task_id=plan_task.id),
            span(
                "task.attempt",
                kind="task",
                task_id=plan_task.id,
                attempt=attempt,
                rung=ladder_rung,
                agent=agent.name,
            ),
        ):
            return await self._loop(
                plan_task,
                agent,
                attempt=attempt,
                ladder_rung=ladder_rung,
                failure_context=failure_context,
                pinned=pinned,
                sampling_temperature=sampling_temperature,
            )

    # ------------------------------------------------------------- loop

    async def _loop(
        self,
        plan_task: PlanTask,
        agent: AgentDef,
        *,
        attempt: int,
        ladder_rung: int,
        failure_context: str | None,
        pinned: list[str],
        sampling_temperature: float | None,
    ) -> TaskOutcome:
        # dict.fromkeys dedupes while keeping order — a duplicate would create two identical
        # oneOf branches, which jsonschema's "exactly one" semantics reject.
        allowed_tools = [
            t
            for t in dict.fromkeys([*agent.tools, *HARNESS_TOOLS])
            if (tool := self.state.tools.registry.get(t)) is not None
            and not (self.sandbox.name == "disabled" and tool.needs_sandbox)
        ]
        if self.state.config.execution.compact_planning:
            agent = agent.model_copy(
                update={
                    "model_role": "heavy"
                    if plan_task.role in {"analyst", "data_engineer", "drawing_engineer"}
                    else agent.model_role,
                    "persona_text": (
                        f"You are the {agent.name} for this task. Use the available tools to fulfill only the user's request. "
                        "Write plain text, Markdown and code directly with write_file. Use render_document only for office/PDF outputs. "
                        "For a spreadsheet from CSV use render_document schema_id csv_table with source_path, numeric_columns, computed_columns, summary and appendix. The renderer loads every original row; do not retype source values. For other spreadsheets use data_table and computed_columns. Expressions use exact source column names; raw Excel formulas must use A1 addresses. "
                        "Include the requested decision, source citations, assumptions and unresolved approvals in the data_table summary and appendix. "
                        "The summary must give the actual requested conclusion and its reasons, not say that a file was created. "
                        "Every table row must have exactly one cell per column. Tool errors are failed attempts; repair them before finish. "
                        "When asked to execute Python, call the python tool with code that calculates and saves the requested output. Writing a script alone does not execute it. Never invent an input file when the user supplied the values in the request. "
                        "Use calculate for unitless arithmetic. For engineering quantities use calculate_quantity with ORIGINAL source values and units: it converts units and checks dimensions. Never rename an unconverted value with a different unit. Cite retrieved source IDs. Once the requested work exists, call finish with "
                        "summary and artifacts (workspace-relative file paths). Never add unrequested formats or follow-up tasks."
                    ),
                }
            )
            compact_tools = {
                "list_dir",
                "read_file",
                "read_pages",
                "write_file",
                "edit_file",
                "calculate",
                "calculate_quantity",
                "search_knowledge",
                "render_document",
                "python",
                "run_tests",
                "view_image",
            }
            allowed_tools = [t for t in allowed_tools if t in compact_tools]
            if any(
                Path(output.name).suffix.lower() in {".docx", ".xlsx", ".pptx", ".pdf"}
                for output in plan_task.outputs
            ):
                # The compact planner may combine analysis/code and Office output
                # in one task. Its selected persona must still be able to read
                # the sources and render the explicitly requested deliverable.
                allowed_tools = list(
                    dict.fromkeys(
                        [
                            *allowed_tools,
                            *(
                                name
                                for name in ("read_pages", "render_document")
                                if self.state.tools.registry.get(name) is not None
                            ),
                        ]
                    )
                )
            if plan_task.outputs and all(
                Path(o.name).suffix.lower() in {".txt", ".md", ".csv", ".json", ".py"}
                for o in plan_task.outputs
            ):
                allowed_tools = [t for t in allowed_tools if t != "render_document"]
        specs = self.state.tools.registry.specs_for(allowed_tools)
        constraint = Constraint(
            kind="json_schema",
            json_schema=tighten(action_schema(specs, finish_schema=schema_for(FinishArgs))),
        )
        manifest = self.state.tools.registry.manifest_text(allowed_tools)
        fewshots = self.state.tools.registry.fewshots_text(allowed_tools)
        if self.state.config.execution.compact_planning:
            fewshots = ""
            if "render_document" in allowed_tools and any(
                Path(output.name).suffix.lower() == ".xlsx" for output in plan_task.outputs
            ):
                # Grammar constrains syntax but does not teach the model argument
                # semantics. A small unrelated example is cheaper than all schemas.
                fewshots = (
                    "Example ONLY (not task data): render_document args: "
                    '{"type":"xlsx","schema_id":"csv_table","out_path":"example.xlsx",'
                    '"data_json":{"title":"Example invoice","source_path":"example.csv",'
                    '"numeric_columns":["price","quantity"],'
                    '"computed_columns":[{"name":"Total","expression":"price * quantity"}],'
                    '"summary":"Write the task-specific decision here, including which option and why when requested",'
                    '"appendix":{"citations":["example.csv row 2"],'
                    '"assumptions":["Illustrative example"],"unverified_claims":[]}}}. '
                    "Replace all example paths, columns and conclusions with this task. "
                    "Keep numeric inputs unquoted. A1 is the first header cell; data starts at row 2."
                )
        retrieved = []
        if self.state.config.knowledge.auto_index_workspace and self.state.knowledge is not None:
            from yantra_server.workbench import collection_for

            from .context import RetrievedChunk

            hits = await self.state.knowledge.search(
                plan_task.intent, collections=[collection_for(self.workspace)], k=4
            )
            retrieved = [
                RetrievedChunk(
                    chunk_id=h.chunk_id,
                    title=h.title,
                    page=h.page,
                    section=h.section,
                    text=h.text[:1600],
                    score=h.score,
                )
                for h in hits
            ]

        steps = self._load_steps(plan_task.id, attempt)
        prior_sources = self._prior_source_observations(plan_task.id, attempt)
        ledger_text = self._latest_ledger_text(plan_task.id)
        recent_action_hashes: list[str] = [s.action_hash for s in steps][-LOOP_GUARD_WINDOW:]
        consecutive_errors = 0
        extra_note = (
            f"Previous attempt failed because:\n{failure_context}" if failure_context else None
        )
        started = time.monotonic()
        task_budget = plan_task.budget

        while True:
            if self.cancel_event.is_set():
                return TaskOutcome(status="cancelled", steps_used=len(steps))
            n = len(steps) + 1
            if n > task_budget.max_steps or (time.monotonic() - started) > task_budget.max_seconds:
                return await self._force_finish(plan_task, agent, steps, "budget_exhausted")
            for warned in self.run_budget.check(raise_on_exhausted=False):
                snapshot = self.run_budget.snapshot()
                self.notifier.budget_warning(
                    warned,
                    snapshot.get(f"{warned}_used", 0.0),
                    snapshot.get(f"{warned}_max", 0.0),
                )
            if self.run_budget.exhausted():
                return await self._force_finish(plan_task, agent, steps, "budget_exhausted")

            budget_left = (
                f"{task_budget.max_steps - n + 1} steps, "
                f"{int(task_budget.max_seconds - (time.monotonic() - started))}s"
            )
            card = task_card_text(
                plan_task.id,
                plan_task.title,
                plan_task.intent,
                inputs_desc=pinned[:10],
                outputs_desc=[f"{o.name} ({o.type})" for o in plan_task.outputs],
                acceptance_desc=[describe_check(c.model_dump()) for c in plan_task.acceptance],
                budget_left=budget_left,
            )
            if failure_context:
                card += "\nREQUIRED REPAIR (preserve throughout this attempt):\n" + failure_context
            if prior_sources:
                card += "\n" + prior_sources
            assembled = self.context_builder.build_step_messages(
                agent,
                tool_manifest=manifest,
                fewshots=fewshots,
                task_card=card,
                pinned=[],
                retrieved=retrieved,
                ledger_text=ledger_text,
                steps=[s.view for s in steps],
                extra_note=extra_note,
            )
            extra_note = None

            decision = await self._think(
                agent, assembled.messages, constraint, ladder_rung, sampling_temperature
            )
            self._emit_stats(agent, assembled.approx_tokens)
            if decision is None:
                consecutive_errors += 1
                if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                    return TaskOutcome(
                        status="failed",
                        failure="model produced malformed actions repeatedly",
                        steps_used=len(steps),
                    )
                extra_note = "Your last output was not a valid action. Emit exactly one action."
                continue

            if decision.action.tool == "finish":
                finish = self._parse_finish(decision.action.args)
                if finish is None:
                    extra_note = "finish arguments were invalid; emit finish again with summary/artifacts/claims/self_check"
                    continue
                self._record_step(plan_task.id, attempt, n, decision, "(finished)", None, ok=True)
                return TaskOutcome(status="done", finish=finish, steps_used=n)

            action_hash = hashlib.sha256(
                json.dumps([decision.action.tool, decision.action.args], sort_keys=True).encode()
            ).hexdigest()[:16]
            if action_hash in recent_action_hashes:
                if recent_action_hashes.count(action_hash) >= 2:
                    return TaskOutcome(
                        status="failed",
                        steps_used=len(steps),
                        failure="Repeated identical action without progress; replan using the observed tool error.",
                    )
                previous = next((s for s in reversed(steps) if s.action_hash == action_hash), None)
                observation = (
                    "You already ran this exact call; the result was:\n"
                    f"{(previous.view.observation if previous else '(see earlier step)')[:800]}\n"
                    "Choose a different action or finish."
                )
                record = self._record_step(
                    plan_task.id, attempt, n, decision, observation, None, ok=False
                )
                steps.append(record)
                recent_action_hashes = [*recent_action_hashes, action_hash][-LOOP_GUARD_WINDOW:]
                continue

            step_row_id = self._record_step_start(plan_task.id, attempt, n, decision)
            self.notifier.tool_started(
                step_row_id, plan_task.id, decision.action.tool, _redact(decision.action.args)
            )

            def emit_output(text: str, sid: str = step_row_id) -> None:
                self.notifier.tool_output(sid, text[-2000:])

            ctx = ToolContext(
                workspace=self.workspace,
                state=self.state,
                sandbox=self.sandbox,
                mode=self.mode,
                run_id=self.run_id,
                task_id=plan_task.id,
                step_id=step_row_id,
                idempotency_key=idempotency_key(
                    f"{plan_task.id}#{attempt}", n, decision.action.tool, decision.action.args
                ),
                budget=self.run_budget,
                allowed_tools=allowed_tools,
                emit_output=emit_output,
                ask_user=None,
            )
            with run_context(step_id=step_row_id):
                result = await self.state.tools.runtime.execute(
                    decision.action.tool, decision.action.args, ctx
                )
            self.run_budget.add_tool_call()
            observation = compact_observation(result)
            self.notifier.tool_finished(
                step_row_id, decision.action.tool, result.summary, result.artifact_id, result.ok
            )
            record = self._finish_step(
                step_row_id,
                plan_task.id,
                attempt,
                n,
                decision,
                observation,
                result.artifact_id,
                result.ok,
                action_hash,
            )
            steps.append(record)
            recent_action_hashes = [*recent_action_hashes, action_hash][-LOOP_GUARD_WINDOW:]

            if result.ok:
                if self.state.config.execution.compact_planning:
                    from .verifier import (
                        completed_literal_file,
                        execution_review_candidate,
                        rendered_review_candidate,
                    )

                    complete = completed_literal_file(plan_task, self.workspace)
                    if complete is None:
                        complete = execution_review_candidate(
                            plan_task, self.workspace, decision.action.tool, result
                        )
                    if complete is None:
                        complete = rendered_review_candidate(
                            plan_task, self.workspace, decision.action.tool, result
                        )
                    if complete is not None:
                        return TaskOutcome(status="done", finish=complete, steps_used=len(steps))
                consecutive_errors = 0
            else:
                consecutive_errors += 1
                if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                    extra_note = (
                        f"The last {CONSECUTIVE_ERROR_LIMIT} actions all failed "
                        f"(last error: {(result.error or '')[:200]}). Step back: try a "
                        "different tool or approach, or finish with what you have."
                    )
                    consecutive_errors = 0

            if n % LEDGER_EVERY_STEPS == 0 or not result.ok:
                ledger_text = await self._update_ledger(plan_task, steps)

    def _emit_stats(self, agent: AgentDef, context_tokens: int) -> None:
        budget = self.context_builder.budget_for(agent.model_role)
        self.notifier.run_stats(
            tokens_in=self.run_budget.prompt_tokens,
            tokens_out=self.run_budget.completion_tokens,
            context_pct=round(100 * context_tokens / max(budget, 1), 1),
            elapsed_s=round(self.run_budget.elapsed_s(), 1),
            cost_saved_inr=0,  # No measured commercial baseline or energy cost is available.
            active_model=self._last_model,
        )

    # ------------------------------------------------------------- think

    async def _think(
        self,
        agent: AgentDef,
        messages: list[ChatMessage],
        constraint: Constraint,
        ladder_rung: int,
        sampling_temperature: float | None,
    ) -> StepDecision | None:
        decoding = Decoding(
            temperature=sampling_temperature
            if sampling_temperature is not None
            else agent.decoding.temperature,
            max_tokens=3000,
        )
        if agent.decoding.reasoning_effort:
            decoding.reasoning_effort = agent.decoding.reasoning_effort  # type: ignore[assignment]
        try:
            result = await self.state.gateway.chat(
                ModelRequest(
                    role="coder"
                    if agent.name == "coder" and "coder" in self.state.router.policy.roles
                    else agent.model_role,
                    messages=messages,
                    constraint=constraint,
                    decoding=decoding,
                    budget=self.run_budget,
                    ladder_rung=ladder_rung,
                    on_delta=lambda e: (
                        self.notifier.thinking(e.text) if isinstance(e, ReasoningDelta) else None
                    ),
                )
            )
        except Exception as exc:
            with span("step.think_error", kind="step", error=str(exc)[:300]):
                return None
        self._last_model = result.result.model or result.decision.model
        try:
            parsed = result.parsed
            if not isinstance(parsed, dict):
                parsed = json.loads(str(parsed))
            if isinstance(parsed, dict) and isinstance(parsed.get("thought"), str):
                # Some engines cannot grammar-enforce string length caps; clamp instead
                # of failing the whole step on an over-long thought.
                parsed["thought"] = parsed["thought"][:400]
            return StepDecision.model_validate(parsed)
        except (ValidationError, json.JSONDecodeError):
            return None

    def _parse_finish(self, args: dict[str, Any]) -> FinishArgs | None:
        if isinstance(args.get("summary"), str):
            args["summary"] = args["summary"][:2000]
        notes = args.get("self_check")
        if isinstance(notes, dict) and isinstance(notes.get("notes"), str):
            notes["notes"] = notes["notes"][:500]
        try:
            return FinishArgs.model_validate(args)
        except ValidationError:
            return None

    # ------------------------------------------------------------- forced finish

    async def _force_finish(
        self,
        plan_task: PlanTask,
        agent: AgentDef,
        steps: list[_StepRecord],
        reason: Literal["budget_exhausted"],
    ) -> TaskOutcome:
        finish_constraint = Constraint(
            kind="json_schema",
            json_schema=action_schema([], finish_schema=schema_for(FinishArgs)),
        )
        summary_of_steps = "\n".join(
            f"[{s.view.n}] {s.view.action_text} → {s.view.observation.splitlines()[0][:120] if s.view.observation else ''}"
            for s in steps[-10:]
        )
        try:
            result = await self.state.gateway.chat(
                ModelRequest(
                    role=agent.model_role,
                    messages=[
                        ChatMessage(role="system", content=agent.persona_text),
                        ChatMessage(
                            role="user",
                            content=f"Task {plan_task.id} ({plan_task.title}) ran out of budget. "
                            f"Recent steps:\n{summary_of_steps}\n\n"
                            "Emit finish NOW with an honest summary of what was and was not done.",
                        ),
                    ],
                    constraint=finish_constraint,
                    decoding=Decoding(temperature=0.0, max_tokens=1500),
                    budget=self.run_budget,
                )
            )
            parsed = result.parsed
            decision = StepDecision.model_validate(parsed)
            finish = self._parse_finish(decision.action.args)
        except Exception:
            finish = None
        if finish is None:
            finish = FinishArgs(
                summary=f"Task stopped: budget exhausted after {len(steps)} steps.",
                artifacts=[],
                claims=[],
            )
        finish.summary = f"[budget exhausted] {finish.summary}"
        return TaskOutcome(status="budget_exhausted", finish=finish, steps_used=len(steps))

    # ------------------------------------------------------------- ledger

    async def _update_ledger(self, plan_task: PlanTask, steps: list[_StepRecord]) -> str:
        recent = "\n".join(
            f"[{s.view.n}] {s.view.action_text} → {s.view.observation[:300]}" for s in steps[-8:]
        )
        try:
            result = await self.state.gateway.chat(
                ModelRequest(
                    role="utility",
                    messages=[
                        ChatMessage(role="system", content=LEDGER_SYSTEM),
                        ChatMessage(
                            role="user",
                            content=f"Task: {plan_task.title}\nIntent: {plan_task.intent}\n"
                            f"Recent steps:\n{recent}",
                        ),
                    ],
                    schema_model=LedgerState,
                    decoding=Decoding(temperature=0.0, max_tokens=800),
                    budget=self.run_budget,
                    priority=1,
                )
            )
            ledger = result.parsed
            assert isinstance(ledger, LedgerState)
        except Exception:
            return self._latest_ledger_text(plan_task.id)
        with self.state.db.session() as s:
            s.add(
                LedgerEntryRow(
                    task_id=plan_task.id,
                    run_id=self.run_id,
                    n=len(steps),
                    facts_established=ledger.facts_established,
                    done=ledger.done,
                    remaining=ledger.remaining,
                    blockers=ledger.blockers,
                    stuck=ledger.stuck,
                    next_action_hint=ledger.next_action_hint,
                )
            )
        return _ledger_to_text(ledger)

    def _latest_ledger_text(self, task_id: str) -> str:
        with self.state.db.session() as s:
            row = s.execute(
                select(LedgerEntryRow)
                .where(LedgerEntryRow.run_id == self.run_id, LedgerEntryRow.task_id == task_id)
                .order_by(LedgerEntryRow.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if row is None:
            return ""
        return _ledger_to_text(
            LedgerState(
                facts_established=list(row.facts_established),
                done=list(row.done),
                remaining=list(row.remaining),
                blockers=list(row.blockers),
                stuck=row.stuck,
                next_action_hint=row.next_action_hint or "",
            )
        )

    # ------------------------------------------------------------- step persistence

    def _prior_source_observations(self, task_id: str, attempt: int) -> str:
        """Keep bounded, exact read observations across retries; never model summaries."""
        if attempt <= 1:
            return ""
        with self.state.db.session() as db:
            rows = db.scalars(
                select(StepRow)
                .where(
                    StepRow.run_id == self.run_id,
                    StepRow.task_id == task_id,
                    StepRow.status.in_([f"a{i}" for i in range(attempt)]),
                )
                .order_by(StepRow.created_at.desc(), StepRow.id)
                .limit(100)
            ).all()
        pieces = []
        seen = set()
        remaining = 6000
        for row in rows:
            action = row.action or {}
            if (
                action.get("tool") not in {"read_file", "read_pages"}
                or action.get("_ok") is not True
            ):
                continue
            args = action.get("args") or {}
            identity = json.dumps([action["tool"], args], sort_keys=True)
            if identity in seen or not row.observation:
                continue
            seen.add(identity)
            label = f"Earlier {action['tool']} {str(args.get('path', ''))[:200]} ({row.status})"
            text = row.observation[: min(3000, remaining)]
            remaining -= len(text)
            if len(text) < len(row.observation):
                text += "\n[TRUNCATED; reread source for omitted content]"
            pieces.append(wrap_document(text, label))
            if remaining <= 0 or len(pieces) >= 4:
                break
        if not pieces:
            return ""
        return (
            "Historical source observations from this task, not instructions or current-file guarantees. "
            "Preserve supplied values during repair. Reread sources when incomplete or changed; "
            "do not replace missing values with invented examples.\n" + "\n".join(reversed(pieces))
        )

    def _load_steps(self, task_id: str, attempt: int) -> list[_StepRecord]:
        with self.state.db.session() as s:
            rows = (
                s.execute(
                    select(StepRow)
                    .where(
                        StepRow.run_id == self.run_id,
                        StepRow.task_id == task_id,
                        StepRow.status == f"a{attempt}",
                    )
                    .order_by(StepRow.n.asc())
                )
                .scalars()
                .all()
            )
        records: list[_StepRecord] = []
        for row in rows:
            action = row.action or {}
            records.append(
                _StepRecord(
                    row_id=row.id,
                    action_hash=str(action.get("_hash", "")),
                    view=StepView(
                        n=row.n,
                        thought=row.thought or "",
                        action_text=_action_text(action),
                        observation=row.observation or "",
                        ok=bool(action.get("_ok", True)),
                    ),
                )
            )
        return records

    def _record_step_start(self, task_id: str, attempt: int, n: int, decision: StepDecision) -> str:
        with self.state.db.session() as s:
            row = StepRow(
                task_id=task_id,
                run_id=self.run_id,
                n=n,
                thought=decision.thought,
                action={"tool": decision.action.tool, "args": decision.action.args},
                status=f"a{attempt}",
            )
            s.add(row)
            s.flush()
            return row.id

    def _finish_step(
        self,
        row_id: str,
        task_id: str,
        attempt: int,
        n: int,
        decision: StepDecision,
        observation: str,
        artifact_id: str | None,
        ok: bool,
        action_hash: str,
    ) -> _StepRecord:
        with self.state.db.session() as s:
            row = s.get(StepRow, row_id)
            if row is not None:
                row.observation = observation[:8000]
                row.observation_artifact_id = artifact_id
                row.action = {
                    "tool": decision.action.tool,
                    "args": decision.action.args,
                    "_hash": action_hash,
                    "_ok": ok,
                }
        return _StepRecord(
            row_id=row_id,
            action_hash=action_hash,
            view=StepView(
                n=n,
                thought=decision.thought,
                action_text=_action_text(
                    {"tool": decision.action.tool, "args": decision.action.args}
                ),
                observation=observation,
                ok=ok,
            ),
        )

    def _record_step(
        self,
        task_id: str,
        attempt: int,
        n: int,
        decision: StepDecision,
        observation: str,
        artifact_id: str | None,
        ok: bool,
    ) -> _StepRecord:
        row_id = self._record_step_start(task_id, attempt, n, decision)
        return self._finish_step(
            row_id, task_id, attempt, n, decision, observation, artifact_id, ok, action_hash=""
        )


@dataclass
class _StepRecord:
    row_id: str
    action_hash: str
    view: StepView


def _action_text(action: dict[str, Any]) -> str:
    tool = action.get("tool", "?")
    args = {k: v for k, v in (action.get("args") or {}).items()}
    rendered = json.dumps(args, ensure_ascii=False, default=str)
    if len(rendered) > 300:
        rendered = rendered[:300] + "…"
    return f"{tool}({rendered})"


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v)[:300] if isinstance(v, str) else v) for k, v in args.items()}


def _ledger_to_text(ledger: LedgerState) -> str:
    parts: list[str] = []
    if ledger.facts_established:
        parts.append("Facts: " + "; ".join(ledger.facts_established))
    if ledger.done:
        parts.append("Done: " + "; ".join(ledger.done))
    if ledger.remaining:
        parts.append("Remaining: " + "; ".join(ledger.remaining))
    if ledger.blockers:
        parts.append("Blockers: " + "; ".join(ledger.blockers))
    if ledger.stuck:
        parts.append("STUCK — change approach.")
    if ledger.next_action_hint:
        parts.append(f"Hint: {ledger.next_action_hint}")
    return "\n".join(parts)
