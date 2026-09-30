"""Typed notification emission for one run (thin wrapper over the EventBus)."""

from __future__ import annotations

from typing import Any, Literal

from yantra_server.protocol import messages as msg
from yantra_server.rpc import EventBus


class RunNotifier:
    def __init__(self, bus: EventBus | None, run_id: str) -> None:
        self.bus = bus
        self.run_id = run_id

    def _publish(self, note: msg.Notification) -> None:
        if self.bus is not None:
            note.run_id = self.run_id
            self.bus.publish(note)

    def assistant(self, text: str) -> None:
        self._publish(msg.AssistantDelta(text=text))

    def thinking(self, text: str) -> None:
        self._publish(msg.ThinkingDelta(text=text))

    def plan_updated(self, plan: dict[str, Any]) -> None:
        self._publish(msg.PlanUpdated(plan=plan))

    def task_updated(self, task: dict[str, Any]) -> None:
        self._publish(msg.TaskUpdated(task=task))

    def tool_started(
        self, step_id: str, task_id: str | None, tool: str, args: dict[str, Any]
    ) -> None:
        self._publish(msg.ToolStarted(step_id=step_id, task_id=task_id, tool=tool, args=args))

    def tool_output(self, step_id: str, text: str) -> None:
        self._publish(msg.ToolOutputDelta(step_id=step_id, text=text))

    def tool_finished(
        self, step_id: str, tool: str, summary: str, artifact_id: str | None, ok: bool
    ) -> None:
        self._publish(
            msg.ToolFinished(
                step_id=step_id, tool=tool, summary=summary, artifact_id=artifact_id, ok=ok
            )
        )

    def verify_result(self, task_id: str, report: dict[str, Any]) -> None:
        self._publish(msg.VerifyResultNote(task_id=task_id, report=report))

    def escalation(self, task_id: str, rung: str, attempt: int) -> None:
        self._publish(msg.EscalationNote(task_id=task_id, rung=rung, attempt=attempt))

    def question(self, request_id: str, questions: list[str]) -> None:
        self._publish(msg.QuestionNote(request_id=request_id, questions=questions))

    def budget_warning(self, budget: str, used: float, limit: float) -> None:
        self._publish(msg.BudgetWarning(budget=budget, used=used, limit=limit))

    def run_stats(
        self,
        *,
        tokens_in: int,
        tokens_out: int,
        context_pct: float,
        elapsed_s: float,
        cost_saved_inr: float,
        active_model: str,
    ) -> None:
        self._publish(
            msg.RunStats(
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                context_pct=context_pct,
                elapsed_s=elapsed_s,
                cost_saved_inr=cost_saved_inr,
                active_model=active_model,
            )
        )

    def finished(
        self,
        status: str,
        summary: str,
        artifacts: list[dict[str, Any]],
        assumptions: list[str],
        unverified: list[str],
        budget_used: dict[str, Any],
        *,
        response_kind: Literal["workflow", "conversation"] = "workflow",
    ) -> None:
        self._publish(
            msg.RunFinished(
                response_kind=response_kind,
                status=status,
                summary=summary,
                artifacts=artifacts,
                assumptions=assumptions,
                unverified=unverified,
                budget_used=budget_used,
            )
        )

    def error(self, code: str, message: str, hint: str | None = None) -> None:
        self._publish(msg.ErrorNote(code=code, message=message, hint=hint))
