"""Conductor facade: run lifecycle — intake → questions → plan → approval → execute →
final answer. Crash-only: every transition is persisted before it is acted on."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from yantra_server.agents import AgentRoster
from yantra_server.conductor.budgets import BudgetTracker
from yantra_server.db.base import utcnow
from yantra_server.db.models import RunRow, SessionRow, ToolCallRow
from yantra_server.observe.tracing import run_context, span
from yantra_server.sandbox import SandboxError, select_sandbox

from .conversation import route_request
from .intake import build_goal_spec
from .notify import RunNotifier
from .planner import make_plan
from .scheduler import RunController
from .types import GoalSpec, Plan

if TYPE_CHECKING:
    from yantra_server.state import AppState

PLAN_APPROVAL_TIMEOUT_S = 1800.0
QUESTION_TIMEOUT_S = 900.0
UNFINISHED_STATUSES = ("created", "intake", "planning", "running")
TERMINAL_STATUSES = ("done", "done_with_gaps", "cancelled", "planned", "failed")


@dataclass
class ActiveRun:
    controller: RunController
    driver: asyncio.Task[None]
    notifier: RunNotifier


@dataclass
class Conductor:
    state: AppState
    roster: AgentRoster
    _active: dict[str, ActiveRun] = field(default_factory=dict)
    _questions: dict[str, asyncio.Future[list[str]]] = field(default_factory=dict)
    _question_runs: dict[str, str] = field(default_factory=dict)
    _approvals: dict[str, asyncio.Future[str]] = field(default_factory=dict)

    # ------------------------------------------------------------- lifecycle

    async def start_run(
        self,
        session_id: str,
        goal_text: str,
        attachments: list[str],
        mode_override: str | None = None,
        budget_overrides: dict[str, int] | None = None,
    ) -> str:
        from yantra_server.security import workspace_path

        if self.state.config.profile in {"portable", "laptop"} and self._active:
            raise ValueError("A workflow is already running")
        with self.state.db.session() as s:
            session = s.get(SessionRow, session_id)
            if session is None:
                raise ValueError(f"unknown session {session_id}")
            workspace_path(session.workspace_path, self.state.config.paths.workspace_roots)
            mode = mode_override or session.mode
            run = RunRow(
                session_id=session_id,
                goal_text=goal_text,
                status="created",
                mode=mode,
                workspace_path=session.workspace_path,
                collections=list(session.collections),
                budgets={
                    "max_tokens": self.state.config.budgets.max_tokens,
                    "max_seconds": self.state.config.budgets.max_seconds,
                    "max_tool_calls": self.state.config.budgets.max_tool_calls,
                    "max_sandbox_cpu_s": self.state.config.budgets.max_sandbox_cpu_s,
                    **(budget_overrides or {}),
                },
            )
            s.add(run)
            s.flush()
            run_id = run.id
        self.state.audit.append(
            "user", "run.start", {"run_id": run_id, "goal": goal_text[:300], "mode": mode}
        )
        self._launch(
            run_id,
            goal_text,
            Path(str(run.workspace_path)),
            mode,
            attachments,
            resume=False,
            budget_overrides=budget_overrides,
        )
        return run_id

    async def resume_run(self, run_id: str) -> str:
        from yantra_server.security import workspace_path

        if run_id in self._active:
            raise ValueError("This workflow is already running")
        if self.state.config.profile in {"portable", "laptop"} and self._active:
            raise ValueError("A workflow is already running")
        with self.state.db.session() as s:
            run = s.get(RunRow, run_id)
            if run is None:
                raise ValueError(f"unknown run {run_id}")
            if run.status in TERMINAL_STATUSES:
                return run_id
            goal_text = run.goal_text
            workspace = workspace_path(run.workspace_path, self.state.config.paths.workspace_roots)
            mode = run.mode
        uncertain = self.uncertain_operations(run_id)
        if uncertain:
            raise ValueError(
                "Recovery paused: an interrupted operation may have changed files. "
                "Inspect its outcome before starting new work; it will not be repeated automatically."
            )
        self.state.audit.append("user", "run.resume", {"run_id": run_id})
        self.state.tools.broker.revoke_run(run_id)
        self._launch(run_id, goal_text, workspace, mode, [], resume=True)
        return run_id

    def uncertain_operations(self, run_id: str) -> list[dict[str, Any]]:
        with self.state.db.session() as s:
            rows = (
                s.execute(
                    select(ToolCallRow).where(
                        ToolCallRow.run_id == run_id, ToolCallRow.status.in_(("pending", "running"))
                    )
                )
                .scalars()
                .all()
            )
            return [
                {"call_id": row.id, "tool": row.tool, "args": row.args}
                for row in rows
                if (tool := self.state.tools.registry.get(row.tool)) is None
                or tool.side_effects not in ("none", "read")
            ]

    def reconcile_interrupted(self) -> list[str]:
        """Called once at startup, before accepting requests. Never rerun work implicitly."""
        recovered: list[str] = []
        with self.state.db.session() as s:
            for run in s.execute(
                select(RunRow).where(RunRow.status.in_(UNFINISHED_STATUSES))
            ).scalars():
                if run.id not in self._active:
                    run.status = "interrupted"
                    recovered.append(run.id)
        for run_id in recovered:
            self.state.audit.append("system", "run.interrupted", {"run_id": run_id})
        return recovered

    def _launch(
        self,
        run_id: str,
        goal_text: str,
        workspace: Path,
        mode: str,
        attachments: list[str],
        *,
        resume: bool,
        budget_overrides: dict[str, int] | None = None,
    ) -> None:
        if run_id in self._active:
            raise ValueError("This workflow is already running")
        notifier = RunNotifier(self.state.bus, run_id)
        overrides = budget_overrides or {}
        used: dict[str, float] = {}
        if resume:
            with self.state.db.session() as s:
                row = s.get(RunRow, run_id)
                if row is not None:
                    overrides = row.budgets
                    used = row.budget_used
                    if model := row.budgets.get("model"):
                        self.state.gateway.run_models[run_id] = str(model)
        budget = BudgetTracker(
            max_tokens=int(overrides.get("max_tokens", self.state.config.budgets.max_tokens)),
            max_seconds=float(overrides.get("max_seconds", self.state.config.budgets.max_seconds)),
            max_tool_calls=int(
                overrides.get("max_tool_calls", self.state.config.budgets.max_tool_calls)
            ),
            max_sandbox_cpu_s=float(
                overrides.get("max_sandbox_cpu_s", self.state.config.budgets.max_sandbox_cpu_s)
            ),
            warn_ratio=self.state.config.budgets.warn_ratio,
        )
        budget.restore(used)
        budget.on_change = lambda usage: self._persist_budget(run_id, usage)
        try:
            sandbox = select_sandbox(
                self.state.config.sandbox, workspace, sealed=self.state.config.sealed()
            )
        except SandboxError as exc:
            notifier.error("sandbox_unavailable", str(exc))
            self._set_run_status(run_id, "failed", final={"summary": str(exc)})
            notifier.finished("failed", str(exc), [], [], [], {})
            return
        controller = RunController(
            state=self.state,
            roster=self.roster,
            notifier=notifier,
            run_id=run_id,
            workspace=workspace,
            mode=mode,
            budget=budget,
            sandbox=sandbox,
        )
        self.state.gateway.run_budgets[run_id] = budget
        driver = asyncio.create_task(
            self._drive(controller, goal_text, attachments, resume=resume),
            name=f"run-{run_id[:8]}",
        )
        self._active[run_id] = ActiveRun(controller=controller, driver=driver, notifier=notifier)
        self._set_run_status(run_id, "created")

        def remove_driver(done: asyncio.Task[None]) -> None:
            current = self._active.get(run_id)
            if current is not None and current.driver is done:
                self._active.pop(run_id, None)

        driver.add_done_callback(remove_driver)

    def cancel(self, run_id: str) -> bool:
        active = self._active.get(run_id)
        if active is None:
            return False
        active.controller.cancel_event.set()
        active.driver.cancel()
        self.state.audit.append("user", "run.cancel", {"run_id": run_id})
        return True

    # ------------------------------------------------------------- driver

    async def _drive(
        self,
        controller: RunController,
        goal_text: str,
        attachments: list[str],
        *,
        resume: bool,
    ) -> None:
        run_id = controller.run_id
        notifier = controller.notifier
        with run_context(run_id=run_id), span("run", kind="run", mode=controller.mode):
            try:
                controller.budget.check()
                remaining = controller.budget.max_seconds - controller.budget.elapsed_s()
                async with asyncio.timeout(
                    remaining if controller.budget.max_seconds > 0 else None
                ):
                    goal_spec, plan = await self._prepare(
                        controller, goal_text, attachments, resume=resume
                    )
                    if plan is None:  # direct reply or plan-only mode
                        return
                    controller.goal_spec = goal_spec  # type: ignore[attr-defined]
                    self._set_run_status(run_id, "running")
                    await controller.run()
                    await self._finalize(controller, goal_spec)
            except asyncio.CancelledError:
                # Preserve checkpoints on shutdown; explicit cancellation is terminal.
                if controller.cancel_event.is_set():
                    self._set_run_status(
                        run_id,
                        "cancelled",
                        final={
                            "summary": "Stopped by user.",
                            "budget_used": controller.budget.snapshot(),
                        },
                    )
                    notifier.finished(
                        "cancelled", "Stopped by user.", [], [], [], controller.budget.snapshot()
                    )
                else:
                    self._set_run_status(run_id, "interrupted")
                raise
            except Exception as exc:
                notifier.error("run_failed", f"{type(exc).__name__}: {exc}")
                self._set_run_status(
                    run_id,
                    "failed",
                    final={
                        "summary": f"run failed: {exc}",
                        "budget_used": controller.budget.snapshot(),
                    },
                )
                notifier.finished(
                    "failed", f"Run failed: {exc}", [], [], [], controller.budget.snapshot()
                )
            finally:
                for child in controller.children:
                    child.cancel()
                await asyncio.gather(*controller.children, return_exceptions=True)
                controller.budget.persist()
                self.state.tools.broker.revoke_run(run_id)
                self.state.gateway.run_models.pop(run_id, None)
                self.state.gateway.run_budgets.pop(run_id, None)

    async def _prepare(
        self,
        controller: RunController,
        goal_text: str,
        attachments: list[str],
        *,
        resume: bool,
    ) -> tuple[GoalSpec, Plan | None]:
        run_id = controller.run_id
        stored_spec, stored_plan = self._stored_plan(run_id)
        if resume and stored_spec is not None and stored_plan is not None:
            controller.restore_from_db(stored_plan)
            controller.goal_spec = stored_spec  # type: ignore[attr-defined]
            controller.notifier.plan_updated(stored_plan.model_dump(mode="json"))
            if controller.mode == "ask":
                decision = await self._await_plan_approval(controller, stored_plan)
                if decision == "deny":
                    self._set_run_status(run_id, "cancelled")
                    controller.notifier.finished("cancelled", "Plan rejected.", [], [], [], {})
                    return stored_spec, None
            return stored_spec, stored_plan

        self._set_run_status(run_id, "intake")
        disposition = await route_request(self.state, goal_text, attachments)
        if disposition.route == "conversation":
            summary = disposition.reply.strip()
            budget_used = controller.budget.snapshot()
            self._set_run_status(
                run_id,
                "done",
                final={
                    "response_kind": "conversation",
                    "summary": summary,
                    "artifacts": [],
                    "assumptions": [],
                    "unverified": [],
                    "budget_used": budget_used,
                },
            )
            controller.notifier.finished(
                "done", summary, [], [], [], budget_used, response_kind="conversation"
            )
            self.state.audit.append(
                "system", "run.finished",
                {"run_id": run_id, "status": "done", "response_kind": "conversation"},
            )
            return GoalSpec(objective=goal_text), None

        if self.state.config.knowledge.auto_index_workspace and self.state.knowledge is not None:
            from yantra_server.workbench import collection_for

            collection = collection_for(controller.workspace)
            controller.notifier.assistant("Indexing the selected local workspace…\n")
            stats = await self.state.knowledge.ingest_path(controller.workspace, collection)
            with self.state.db.session() as s:
                row = s.get(RunRow, run_id)
                if row:
                    row.collections = list(dict.fromkeys([*row.collections, collection]))
            controller.notifier.assistant(
                f"Local index: {stats.documents} updated, {stats.skipped} unchanged, "
                f"{stats.chunks} chunks, {stats.errors} errors.\n"
            )
        goal_spec = await build_goal_spec(
            self.state,
            goal_text,
            controller.workspace,
            list(self._run_collections(run_id)),
            attachments,
        )
        if goal_spec.open_questions and controller.mode == "ask":
            answers = await self.ask_questions(run_id, goal_spec.open_questions)
            if answers:
                goal_spec.constraints.extend(
                    f"user answered {q!r}: {a}"
                    for q, a in zip(goal_spec.open_questions, answers, strict=False)
                )
                goal_spec.open_questions = []

        self._set_run_status(run_id, "planning")
        plan = await make_plan(self.state, self.roster, goal_spec)
        controller.notifier.plan_updated(plan.model_dump(mode="json"))
        self._store_plan(run_id, goal_spec, plan)

        if controller.mode == "plan":
            self._set_run_status(run_id, "planned")
            controller.notifier.finished(
                "planned",
                "Plan ready. Switch mode (Tab) and prompt again, or edit with /plan edit.",
                [],
                goal_spec.assumptions,
                [],
                {},
            )
            return goal_spec, None

        if controller.mode == "ask":
            decision = await self._await_plan_approval(controller, plan)
            if decision == "deny":
                self._set_run_status(run_id, "cancelled")
                controller.notifier.finished("cancelled", "Plan rejected.", [], [], [], {})
                return goal_spec, None

        controller.install_plan(plan)
        return goal_spec, plan

    async def _await_plan_approval(self, controller: RunController, plan: Plan) -> str:
        request_id = f"plan:{controller.run_id}:{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._approvals[request_id] = future
        from yantra_server.protocol.messages import PermissionRequest

        controller.notifier._publish(
            PermissionRequest(
                request_id=request_id,
                tool="plan",
                args={"tasks": len(plan.tasks)},
                rule={"reason": "plan approval (ask mode)"},
                explanation="Approve the plan to start execution.",
            )
        )
        try:
            return await asyncio.wait_for(future, timeout=PLAN_APPROVAL_TIMEOUT_S)
        except TimeoutError:
            return "deny"
        finally:
            self._approvals.pop(request_id, None)

    async def _finalize(self, controller: RunController, goal_spec: GoalSpec) -> None:
        run_id = controller.run_id
        artifacts: list[dict[str, Any]] = []
        summaries: list[str] = []
        unverified: list[str] = []
        gaps: list[str] = []
        for task_id, task_state in controller.tasks.items():
            if task_state.finish is None:
                if task_state.status in ("failed", "partial"):
                    gaps.append(
                        f"{task_id} ({task_state.plan_task.title}): {task_state.failure_summary or task_state.status}"
                    )
                continue
            summaries.append(f"{task_id} {task_state.plan_task.title}: {task_state.finish.summary}")
            for ref in task_state.finish.artifacts:
                path = controller.workspace / ref
                artifacts.append(
                    {
                        "name": ref,
                        "path": str(path if path.exists() else ref),
                        "task_id": task_id,
                    }
                )
            for claim in task_state.finish.claims:
                if claim.kind == "fact" and not claim.citations:
                    unverified.append(claim.text[:200])
            if task_state.status == "partial":
                gaps.append(
                    f"{task_id} ({task_state.plan_task.title}): delivered partially — "
                    f"{(task_state.failure_summary or 'budget exhausted')[:200]}"
                )
        statuses = {ts.status for ts in controller.tasks.values()}
        status = (
            "done"
            if statuses <= {"done"}
            else ("cancelled" if "cancelled" in statuses else "done_with_gaps")
        )
        summary_lines = summaries[:12]
        if gaps:
            summary_lines.append("Gaps:")
            summary_lines.extend(f"- {g}" for g in gaps[:8])
        summary = "\n".join(summary_lines) or "No tasks produced output."
        budget_used = controller.budget.snapshot()
        final = {
            "summary": summary,
            "artifacts": artifacts,
            "assumptions": goal_spec.assumptions,
            "unverified": unverified,
            "gaps": gaps,
            "budget_used": budget_used,
        }
        self._set_run_status(run_id, status, final=final)
        controller.notifier.finished(
            status, summary, artifacts, goal_spec.assumptions, unverified, budget_used
        )
        self.state.audit.append(
            "system",
            "run.finished",
            {"run_id": run_id, "status": status, "artifacts": [a["name"] for a in artifacts]},
        )
        await self._consolidate_memory(controller, goal_spec, status)

    async def _consolidate_memory(
        self, controller: RunController, goal_spec: GoalSpec, status: str
    ) -> None:
        """Record an episode and propose a skill after a clean run (SPEC §13)."""
        if self.state.memory is None:
            return
        try:
            done = [t for t in controller.tasks.values() if t.status == "done"]
            summary = "; ".join(f"{t.plan_task.title}" for t in done[:8])
            await self.state.memory.record_episode(controller.run_id, goal_spec.objective, summary)
            if status == "done" and len(done) >= 2:
                outline = [t.plan_task.title for t in done]
                tool_set: set[str] = set()
                for t in done:
                    agent = self.roster.get(t.plan_task.role)
                    if agent is not None:
                        tool_set.update(agent.tools)
                await self.state.memory.propose_skill(
                    controller.run_id, goal_spec.objective, outline, sorted(tool_set)
                )
        except Exception:
            pass

    # ------------------------------------------------------------- questions / approvals

    async def ask_questions(self, run_id: str, questions: list[str]) -> list[str] | None:
        active = self._active.get(run_id)
        if active is None:
            return None
        request_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[str]] = loop.create_future()
        self._questions[request_id] = future
        self._question_runs[request_id] = run_id
        active.notifier.question(request_id, questions)
        try:
            return await asyncio.wait_for(future, timeout=QUESTION_TIMEOUT_S)
        except TimeoutError:
            return None
        finally:
            self._questions.pop(request_id, None)
            self._question_runs.pop(request_id, None)

    def pending_request_ids(self, run_id: str) -> list[str]:
        ids = [
            key
            for key, future in self._questions.items()
            if not future.done() and self._question_runs.get(key) == run_id
        ]
        ids.extend(
            key
            for key, future in self._approvals.items()
            if key.startswith(f"plan:{run_id}:") and not future.done()
        )
        return ids

    def resolve_question(
        self, request_id: str, answers: list[str], *, run_id: str | None = None
    ) -> bool:
        if run_id is not None and self._question_runs.get(request_id) != run_id:
            return False
        future = self._questions.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(answers)
        return True

    def resolve_plan_approval(self, run_id: str, decision: str, *, request_id: str) -> bool:
        if not request_id.startswith(f"plan:{run_id}:"):
            return False
        future = self._approvals.get(request_id)
        if future is None or future.done():
            return False
        future.set_result("allow" if decision in ("once", "always", "allow") else "deny")
        return True

    async def delegate(
        self,
        run_id: str,
        parent_task_id: str,
        *,
        title: str,
        intent: str,
        role: str,
        acceptance: list[dict[str, Any]] | None,
    ) -> tuple[bool, str]:
        active = self._active.get(run_id)
        if active is None:
            return False, "run is not active"
        return await active.controller.delegate(
            parent_task_id, title=title, intent=intent, role=role, acceptance=acceptance
        )

    async def update_plan(self, run_id: str, plan_data: dict[str, Any]) -> bool:
        active = self._active.get(run_id)
        plan = Plan.model_validate(plan_data)
        if active is not None:
            active.controller.install_plan(plan)
            active.notifier.plan_updated(plan.model_dump(mode="json"))
        spec, _old = self._stored_plan(run_id)
        if spec is not None:
            self._store_plan(run_id, spec, plan)
        return True

    # ------------------------------------------------------------- persistence helpers

    def _persist_budget(self, run_id: str, usage: dict[str, float]) -> None:
        with self.state.db.session() as s:
            row = s.get(RunRow, run_id)
            if row is not None:
                row.budget_used = usage

    def _set_run_status(
        self, run_id: str, status: str, final: dict[str, Any] | None = None
    ) -> None:
        with self.state.db.session() as s:
            run = s.get(RunRow, run_id)
            if run is None:
                return
            run.status = status
            if status not in TERMINAL_STATUSES:
                run.finished_at = None
                run.final = None
            if final is not None:
                run.final = final
                run.budget_used = final.get("budget_used", {})
            if status in ("done", "done_with_gaps", "failed", "cancelled"):
                run.finished_at = utcnow()

    def _store_plan(self, run_id: str, goal_spec: GoalSpec, plan: Plan) -> None:
        with self.state.db.session() as s:
            run = s.get(RunRow, run_id)
            if run is not None:
                run.goal_spec = goal_spec.model_dump(mode="json")
                run.plan = plan.model_dump(mode="json")

    def _stored_plan(self, run_id: str) -> tuple[GoalSpec | None, Plan | None]:
        with self.state.db.session() as s:
            run = s.get(RunRow, run_id)
            if run is None:
                return None, None
            spec = GoalSpec.model_validate(run.goal_spec) if run.goal_spec else None
            plan = Plan.model_validate(run.plan) if run.plan else None
            return spec, plan

    def _run_collections(self, run_id: str) -> list[str]:
        with self.state.db.session() as s:
            run = s.get(RunRow, run_id)
            return [str(c) for c in (run.collections or [])] if run else []

    async def wait_for_run(self, run_id: str, timeout_s: float | None = None) -> None:
        active = self._active.get(run_id)
        if active is None:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(active.driver), timeout=timeout_s)

    def list_active(self) -> list[str]:
        return list(self._active)


def latest_run_id(state: AppState, session_id: str) -> str | None:
    with state.db.session() as s:
        return s.execute(
            select(RunRow.id)
            .where(RunRow.session_id == session_id)
            .order_by(RunRow.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
