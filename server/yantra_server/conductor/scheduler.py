"""Scheduler (SPEC §8.4): topological, parallel, write-ahead, resumable, cancellable.

The escalation ladder (SPEC §8.6) runs here: retry → raise effort → best-of-N re-draft →
heavy model → replan → partial-with-gaps. Ladder rungs advance one per failed attempt and
every escalation is visible (notification + span). Best-of-N re-drafts the finish over the
frozen artifacts rather than re-running side-effecting tools (ADR 0008).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from yantra_server.agents import AgentRoster
from yantra_server.conductor.budgets import BudgetTracker
from yantra_server.db.models import TaskRow
from yantra_server.gateway.engines.base import ChatMessage, Constraint, Decoding
from yantra_server.gateway.service import ModelRequest
from yantra_server.gateway.structured import action_schema, schema_for
from yantra_server.observe.tracing import span
from yantra_server.sandbox import Sandbox

from .executor import TaskExecutor, TaskOutcome
from .notify import RunNotifier
from .planner import dependency_map
from .types import FinishArgs, Plan, PlanTask, StepDecision, VerificationOutcome
from .verifier import Verifier

if TYPE_CHECKING:
    from yantra_server.state import AppState

MAX_REPLANS = 1
DELEGATE_MAX_DEPTH = 2
DELEGATE_MAX_CHILDREN = 6


class ReplanNeeded(Exception):
    def __init__(self, task_id: str, failure_context: str) -> None:
        super().__init__(f"replan needed after {task_id}")
        self.task_id = task_id
        self.failure_context = failure_context


@dataclass
class TaskState:
    plan_task: PlanTask
    status: str = "pending"  # pending|running|done|partial|failed|cancelled
    finish: FinishArgs | None = None
    verification: VerificationOutcome | None = None
    attempt: int = 0
    ladder_rung: int = 0
    failure_summary: str | None = None
    depth: int = 0
    children: int = 0


@dataclass
class RunController:
    state: AppState
    roster: AgentRoster
    notifier: RunNotifier
    run_id: str
    workspace: Path
    mode: str
    budget: BudgetTracker
    sandbox: Sandbox
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: dict[str, TaskState] = field(default_factory=dict)
    replans_used: int = 0

    def __post_init__(self) -> None:
        self.children: set[asyncio.Task[None]] = set()
        self.executor = TaskExecutor(
            state=self.state,
            roster=self.roster,
            notifier=self.notifier,
            run_id=self.run_id,
            workspace=self.workspace,
            mode=self.mode,
            run_budget=self.budget,
            cancel_event=self.cancel_event,
            sandbox=self.sandbox,
        )
        self.verifier = Verifier(
            state=self.state,
            roster=self.roster,
            workspace=self.workspace,
            run_id=self.run_id,
            sandbox=self.sandbox,
        )

    # ------------------------------------------------------------- plan install / resume

    def install_plan(self, plan: Plan) -> None:
        for order, plan_task in enumerate(plan.tasks):
            existing = self.tasks.get(plan_task.id)
            if existing and existing.status in ("done", "partial"):
                existing.plan_task = plan_task  # keep completed work on replan
                continue
            self.tasks[plan_task.id] = TaskState(plan_task=plan_task)
            self._persist_task(plan_task, order)
        self.plan = plan

    def restore_from_db(self, plan: Plan) -> None:
        self.plan = plan
        by_id = {t.id: t for t in plan.tasks}
        with self.state.db.session() as s:
            rows = s.execute(select(TaskRow).where(TaskRow.run_id == self.run_id)).scalars().all()
        for row in rows:
            short_id = row.id.split(":", 1)[1] if ":" in row.id else row.id
            plan_task = by_id.get(short_id)
            if plan_task is None:
                continue
            task_state = TaskState(
                plan_task=plan_task,
                status="pending" if row.status == "running" else row.status,
                attempt=row.attempt if row.status != "running" else row.attempt - 1,
                ladder_rung=row.ladder_rung,
                failure_summary=row.failure_summary,
            )
            if row.status in ("done", "partial") and row.result:
                try:
                    task_state.finish = FinishArgs.model_validate(row.result.get("finish", {}))
                except Exception:
                    task_state.finish = None
            self.tasks[short_id] = task_state
        for plan_task in plan.tasks:  # tasks added by a replan after the crash
            if plan_task.id not in self.tasks:
                self.tasks[plan_task.id] = TaskState(plan_task=plan_task)
                self._persist_task(plan_task, plan.tasks.index(plan_task))

    def _db_task_id(self, task_id: str) -> str:
        return f"{self.run_id[:12]}:{task_id}"

    def _persist_task(self, plan_task: PlanTask, order: int = 0) -> None:
        with self.state.db.session() as s:
            s.merge(
                TaskRow(
                    id=self._db_task_id(plan_task.id),
                    run_id=self.run_id,
                    title=plan_task.title,
                    intent=plan_task.intent,
                    role=plan_task.role,
                    inputs=list(plan_task.inputs),
                    outputs=[o.model_dump(mode="json") for o in plan_task.outputs],
                    acceptance=[c.model_dump(mode="json") for c in plan_task.acceptance],
                    budget=plan_task.budget.model_dump(mode="json"),
                    model_hint=plan_task.model_hint,
                    status="pending",
                    order_index=order,
                )
            )

    def _transition(self, task_id: str, **updates: object) -> None:
        """Write-ahead: persist the transition before acting on it."""
        with self.state.db.session() as s:
            row = s.get(TaskRow, self._db_task_id(task_id))
            if row is None:
                return
            for key, value in updates.items():
                setattr(row, key, value)
        task_state = self.tasks[task_id]
        self.notifier.task_updated(
            {
                "task_id": task_id,
                "title": task_state.plan_task.title,
                "role": task_state.plan_task.role,
                "status": str(updates.get("status", task_state.status)),
                "attempt": int(str(updates.get("attempt", task_state.attempt))),
                "ladder_rung": int(str(updates.get("ladder_rung", task_state.ladder_rung))),
            }
        )

    # ------------------------------------------------------------- run

    async def run(self) -> None:
        """Execute the installed plan to completion (or cancellation/replan exhaustion)."""
        while True:
            try:
                await self._run_dag()
                return
            except ReplanNeeded as need:
                if self.replans_used >= MAX_REPLANS:
                    failed = self.tasks.get(need.task_id)
                    if failed is not None:
                        failed.status = "partial"
                        self._transition(need.task_id, status="partial")
                    continue_after = True
                else:
                    self.replans_used += 1
                    continue_after = await self._replan(need)
                if not continue_after:
                    return

    async def _replan(self, need: ReplanNeeded) -> bool:
        from .planner import make_plan

        self.notifier.escalation(need.task_id, "replan", self.tasks[need.task_id].attempt)
        goal_spec = getattr(self, "goal_spec", None)
        if goal_spec is None:
            return True
        try:
            new_plan = await make_plan(
                self.state,
                self.roster,
                goal_spec,
                failure_context=f"task {need.task_id} kept failing:\n{need.failure_context}",
                previous_plan=self.plan,
            )
        except Exception as exc:
            self.notifier.error("replan_failed", str(exc))
            self.tasks[need.task_id].status = "partial"
            self._transition(need.task_id, status="partial")
            return True
        self.install_plan(new_plan)
        self.notifier.plan_updated(new_plan.model_dump(mode="json"))
        return True

    async def _run_dag(self) -> None:
        parallelism = max(1, int(self.state.config.execution.max_parallel_tasks))
        semaphore = asyncio.Semaphore(parallelism)
        deps = dependency_map(self.plan)
        running: dict[str, asyncio.Task[None]] = {}
        replan_exc: list[ReplanNeeded] = []

        async def run_one(task_id: str) -> None:
            async with semaphore:
                try:
                    await self._execute_with_ladder(task_id)
                except ReplanNeeded as need:
                    replan_exc.append(need)

        while True:
            if self.cancel_event.is_set():
                for pending in running.values():
                    pending.cancel()
                for task_state in self.tasks.values():
                    if task_state.status in ("pending", "running"):
                        task_state.status = "cancelled"
                        self._transition(task_state.plan_task.id, status="cancelled")
                return
            if replan_exc:
                for pending in running.values():
                    await pending
                raise replan_exc[0]
            ready = [
                tid
                for tid, ts in self.tasks.items()
                if ts.status == "pending"
                and tid not in running
                and all(
                    self.tasks.get(d) is None or self.tasks[d].status in ("done", "partial")
                    for d in deps.get(tid, set())
                )
            ]
            ready.sort(
                key=lambda tid: next((i for i, t in enumerate(self.plan.tasks) if t.id == tid), 99)
            )
            for task_id in ready:
                running[task_id] = asyncio.create_task(run_one(task_id), name=f"task-{task_id}")
                self.children.add(running[task_id])
                running[task_id].add_done_callback(self.children.discard)
            if not running:
                blocked = [t for t, ts in self.tasks.items() if ts.status == "pending"]
                for task_id in blocked:  # dependency failed permanently
                    self.tasks[task_id].status = "failed"
                    self.tasks[task_id].failure_summary = "blocked: a dependency failed"
                    self._transition(task_id, status="failed", failure_summary="dependency failed")
                return
            done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for completed in done:
                await completed
            running = {tid: t for tid, t in running.items() if t not in done}

    # ------------------------------------------------------------- ladder

    async def _execute_with_ladder(self, task_id: str) -> None:
        task_state = self.tasks[task_id]
        plan_task = task_state.plan_task
        policy = self.state.router.policy
        max_attempts = plan_task.budget.max_retries + 1
        pinned = self._pinned_inputs(plan_task)

        while task_state.attempt < max_attempts:
            if self.cancel_event.is_set():
                task_state.status = "cancelled"
                self._transition(task_id, status="cancelled")
                return
            task_state.attempt += 1
            task_state.status = "running"
            self._transition(
                task_id,
                status="running",
                attempt=task_state.attempt,
                ladder_rung=task_state.ladder_rung,
            )
            outcome = await self.executor.run_task(
                plan_task,
                attempt=task_state.attempt,
                ladder_rung=task_state.ladder_rung,
                failure_context=task_state.failure_summary,
                pinned=pinned,
            )
            if outcome.status == "cancelled":
                task_state.status = "cancelled"
                self._transition(task_id, status="cancelled")
                return
            if outcome.finish is None:
                task_state.failure_summary = outcome.failure or "task produced no finish"
                verification = None
            else:
                verification = await self.verifier.verify(
                    plan_task, outcome.finish, task_state.attempt
                )
                self.notifier.verify_result(task_id, verification.model_dump(mode="json"))
                task_state.verification = verification
                if verification.verdict == "pass":
                    self._complete(task_state, outcome, verification)
                    return
                task_state.failure_summary = verification.failure_summary()

            if outcome.status == "budget_exhausted":
                self._partial(task_state, outcome)
                return

            rung_index = task_state.ladder_rung  # 0-based into policy.escalation
            if rung_index >= len(policy.escalation):
                self._partial(task_state, outcome)
                return
            action = policy.escalation[rung_index]
            task_state.ladder_rung += 1
            self.notifier.escalation(task_id, action.action, task_state.attempt)
            with span(
                "escalation",
                kind="verify",
                task_id=task_id,
                action=action.action,
                rung=task_state.ladder_rung,
            ):
                pass
            if action.action == "best_of_n" and outcome.finish is not None:
                best = await self._best_of_n_redraft(
                    plan_task, outcome.finish, action.n, task_state
                )
                if best is not None:
                    verification = await self.verifier.verify(plan_task, best, task_state.attempt)
                    self.notifier.verify_result(task_id, verification.model_dump(mode="json"))
                    if verification.verdict == "pass":
                        outcome.finish = best
                        self._complete(task_state, outcome, verification)
                        return
                    task_state.failure_summary = verification.failure_summary()
            if action.action == "replan":
                raise ReplanNeeded(task_id, task_state.failure_summary or "")
        # attempts exhausted → partial with gaps
        self._partial(task_state, None)

    def _complete(
        self, task_state: TaskState, outcome: TaskOutcome, verification: VerificationOutcome
    ) -> None:
        task_state.status = "done"
        task_state.finish = outcome.finish
        self._transition(
            task_state.plan_task.id,
            status="done",
            result={
                "finish": outcome.finish.model_dump(mode="json") if outcome.finish else None,
                "reviewer_score": verification.reviewer.score if verification.reviewer else None,
            },
            failure_summary=None,
        )

    def _partial(self, task_state: TaskState, outcome: TaskOutcome | None) -> None:
        task_state.status = "partial"
        if outcome is not None and outcome.finish is not None:
            task_state.finish = outcome.finish
        self._transition(
            task_state.plan_task.id,
            status="partial",
            result={
                "finish": task_state.finish.model_dump(mode="json") if task_state.finish else None
            },
            failure_summary=task_state.failure_summary,
        )

    def _pinned_inputs(self, plan_task: PlanTask) -> list[str]:
        pinned: list[str] = []
        for ref in plan_task.inputs:
            dep = self.tasks.get(ref)
            if dep is not None and dep.finish is not None:
                artifacts = ", ".join(dep.finish.artifacts[:6]) or "no artifacts"
                pinned.append(
                    f"{ref} ({dep.plan_task.title}): {dep.finish.summary[:400]} [{artifacts}]"
                )
            elif not ref.startswith("t"):
                pinned.append(f"context: {ref}")
        return pinned

    # ------------------------------------------------------------- best-of-N re-draft

    async def _best_of_n_redraft(
        self, plan_task: PlanTask, base_finish: FinishArgs, n: int, task_state: TaskState
    ) -> FinishArgs | None:
        """N sampled re-drafts of the finish over frozen artifacts; reviewer picks (ADR 0008)."""
        agent = self.roster.get(plan_task.role)
        if agent is None:
            return None
        constraint = Constraint(
            kind="json_schema", json_schema=action_schema([], finish_schema=schema_for(FinishArgs))
        )
        evidence = self.verifier._artifact_excerpts(base_finish)
        candidates: list[FinishArgs] = []
        for i in range(n):
            try:
                result = await self.state.gateway.chat(
                    ModelRequest(
                        role=agent.model_role,
                        messages=[
                            ChatMessage(role="system", content=agent.persona_text),
                            ChatMessage(
                                role="user",
                                content=(
                                    f"Task: {plan_task.title}\nIntent: {plan_task.intent}\n"
                                    f"Previous finish failed review:\n{task_state.failure_summary}\n\n"
                                    f"Artifacts (fixed, do not claim new ones):\n{evidence[:8000]}\n\n"
                                    f"Draft {i + 1}/{n}: emit an improved finish over these artifacts."
                                ),
                            ),
                        ],
                        constraint=constraint,
                        decoding=Decoding(temperature=0.7, max_tokens=2500, seed=i),
                        budget=self.budget,
                    )
                )
                decision = StepDecision.model_validate(result.parsed)
                candidate = FinishArgs.model_validate(decision.action.args)
                candidate.artifacts = base_finish.artifacts  # frozen
                candidates.append(candidate)
            except Exception:
                continue
        if not candidates:
            return None
        scored: list[tuple[int, FinishArgs]] = []
        for candidate in candidates:
            report = await self.verifier._review(plan_task, candidate, [])
            scored.append((report.score, candidate))
        scored.sort(key=lambda pair: -pair[0])
        return scored[0][1]

    # ------------------------------------------------------------- delegation (SPEC §8.9)

    async def delegate(
        self,
        parent_task_id: str,
        *,
        title: str,
        intent: str,
        role: str,
        acceptance: list[dict[str, object]] | None,
    ) -> tuple[bool, str]:
        parent = self.tasks.get(parent_task_id)
        depth = (parent.depth if parent else 0) + 1
        if depth > DELEGATE_MAX_DEPTH:
            return False, f"delegation depth limit ({DELEGATE_MAX_DEPTH}) reached"
        if parent is not None:
            if parent.children >= DELEGATE_MAX_CHILDREN:
                return False, f"child limit ({DELEGATE_MAX_CHILDREN}) reached for {parent_task_id}"
            parent.children += 1
        child_index = parent.children if parent else 1
        child_id = f"{parent_task_id}.c{child_index}"
        from .types import PlanTask as PT
        from .types import RubricCheck

        checks = acceptance or []
        plan_task = PT.model_validate(
            {
                "id": "t9",  # placeholder to satisfy the id pattern; real id set below
                "title": title,
                "intent": intent,
                "role": role if self.roster.get(role) else "analyst",
                "acceptance": checks or [RubricCheck().model_dump()],
            }
        )
        plan_task.id = child_id  # child ids extend the parent id (nested display)
        child_state = TaskState(plan_task=plan_task, depth=depth)
        self.tasks[child_id] = child_state
        self._persist_task(plan_task)
        await self._execute_with_ladder(child_id)
        final = self.tasks[child_id]
        if final.status == "done" and final.finish is not None:
            return True, f"{final.finish.summary}\nartifacts: {', '.join(final.finish.artifacts)}"
        return False, final.failure_summary or f"child task {child_id} ended {final.status}"
