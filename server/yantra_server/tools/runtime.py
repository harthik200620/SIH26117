"""ToolRuntime: one door for executing a tool call — validation, permission, idempotent
replay, sandbox, artifacts, spans, audit (SPEC §9, ACT stage of §3)."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from pydantic import ValidationError

from yantra_server.artifacts.store import ArtifactNotFound
from yantra_server.db.base import Database, utcnow
from yantra_server.db.models import ToolCallRow
from yantra_server.observe.audit_chain import AuditChain
from yantra_server.observe.tracing import span

from .base import ToolContext, ToolError, ToolResult
from .permissions import PermissionBroker
from .registry import ToolRegistry

log = logging.getLogger(__name__)

CONTENT_ARTIFACT_THRESHOLD = 8_000  # chars; larger tool output goes to the artifact store


def idempotency_key(task_id: str | None, step_n: int, tool: str, args: dict[str, Any]) -> str:
    material = json.dumps([task_id, step_n, tool, args], sort_keys=True, default=str)
    return hashlib.sha256(material.encode()).hexdigest()[:32]


class ToolRuntime:
    def __init__(
        self,
        registry: ToolRegistry,
        broker: PermissionBroker,
        db: Database,
        audit: AuditChain,
    ) -> None:
        self.registry = registry
        self.broker = broker
        self.db = db
        self.audit = audit

    async def execute(
        self, tool_name: str, raw_args: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        tool = self.registry.get(tool_name)
        if tool is None:
            return ToolResult.fail(f"unknown tool {tool_name!r}")
        if ctx.allowed_tools and tool_name not in ctx.allowed_tools:
            return ToolResult.fail(f"tool {tool_name!r} is not in this agent's manifest")

        try:
            args = tool.Args.model_validate(raw_args)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:3]
            )
            return ToolResult.fail(f"invalid arguments for {tool_name}: {details}")
        args_dict = args.model_dump(mode="json")

        with span("tool.call", kind="tool.call", tool=tool_name) as sp:
            sp.set("args", args_dict)
            sp.set("side_effects", tool.side_effects)

            side_effecting = tool.side_effects not in ("none", "read")
            if side_effecting and ctx.idempotency_key:
                replayed = self._replayed_result(ctx, tool_name, args_dict)
                if replayed is not None:
                    sp.set("replayed", True)
                    return replayed

            allowed, record = await self.broker.check(
                tool, args_dict, mode=ctx.mode, run_id=ctx.run_id, task_id=ctx.task_id
            )
            sp.set("permission", {k: v for k, v in record.items() if k != "rule"})
            if not allowed:
                return ToolResult.fail(
                    f"permission denied for {tool_name} ({record.get('reason', '')}) — "
                    "choose a different action",
                    summary=f"{tool_name}: denied",
                )

            row_id = self._record_start(tool_name, args_dict, record, ctx)
            try:
                result = await tool.run(args, ctx)
            except ToolError as exc:
                result = ToolResult.fail(str(exc))
            except Exception as exc:
                log.exception("tool %s crashed", tool_name)
                result = ToolResult.fail(f"{type(exc).__name__}: {exc}")

            result = self._offload_content(result, ctx)
            self._record_finish(row_id, result, ctx)
            sp.set("ok", result.ok)
            sp.set("summary", result.summary)
            if result.artifact_id:
                sp.set("result_artifact", result.artifact_id)
            if result.error:
                sp.set("error", result.error[:500])
            if "sandbox" in result.data:
                sp.set("sandbox", result.data["sandbox"])
            if side_effecting:
                self.audit.append(
                    "agent",
                    f"tool.{tool.side_effects}",
                    {
                        "tool": tool_name,
                        "ok": result.ok,
                        "summary": result.summary[:200],
                        "run_id": ctx.run_id,
                        "task_id": ctx.task_id,
                        "idempotency_key": ctx.idempotency_key,
                    },
                )
            return result

    # ------------------------------------------------------------- persistence

    def _replayed_result(
        self, ctx: ToolContext, tool_name: str, args: dict[str, Any]
    ) -> ToolResult | None:
        from sqlalchemy import select

        with self.db.session() as s:
            row = s.execute(
                select(ToolCallRow).where(
                    ToolCallRow.run_id == (ctx.run_id or ""),
                    ToolCallRow.idempotency_key == ctx.idempotency_key,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if row.tool != tool_name or row.args != args:
                return ToolResult.fail("Idempotency key conflicts with a different operation")
            if row.status not in ("done", "error") or row.result_artifact_id is None:
                return ToolResult.fail(
                    "Prior operation has no confirmed outcome; automatic repetition is blocked"
                )
            try:
                payload = ctx.state.artifacts.read_bytes(row.result_artifact_id)
                return ToolResult.model_validate_json(payload)
            except (ArtifactNotFound, OSError, ValueError, KeyError):
                return ToolResult.fail(
                    "Recorded result is unavailable; automatic repetition is blocked"
                )

    def _record_start(
        self, tool_name: str, args: dict[str, Any], permission: dict[str, Any], ctx: ToolContext
    ) -> str:
        with self.db.session() as s:
            row = ToolCallRow(
                step_id=ctx.step_id,
                run_id=ctx.run_id or "",
                task_id=ctx.task_id,
                tool=tool_name,
                args=args,
                idempotency_key=ctx.idempotency_key
                or idempotency_key(ctx.task_id, 0, tool_name, args),
                permission={k: v for k, v in permission.items() if k != "rule"},
                status="running",
            )
            s.add(row)
            s.flush()
            return row.id

    def _record_finish(self, row_id: str, result: ToolResult, ctx: ToolContext) -> None:
        # The full ToolResult is stored as an artifact so resume can replay it verbatim.
        artifact_id = ctx.state.artifacts.put_bytes(
            result.model_dump_json().encode("utf-8"),
            kind="tool_output",
            run_id=ctx.run_id,
            task_id=ctx.task_id,
            meta={"tool_result": True},
        )
        with self.db.session() as s:
            row = s.get(ToolCallRow, row_id)
            if row is not None:
                row.status = "done" if result.ok else "error"
                row.error = result.error
                row.result_artifact_id = artifact_id
                row.finished_at = utcnow()
                row.sandbox = result.data.get("sandbox")

    def _offload_content(self, result: ToolResult, ctx: ToolContext) -> ToolResult:
        if len(result.content) > CONTENT_ARTIFACT_THRESHOLD and result.artifact_id is None:
            result.artifact_id = ctx.state.artifacts.put_text(
                result.content,
                kind="tool_output",
                run_id=ctx.run_id,
                task_id=ctx.task_id,
            )
        return result
