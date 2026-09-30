"""Permission policy engine + interactive broker (SPEC §9.4)."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from yantra_server.config import PermissionRule
from yantra_server.db.base import Database
from yantra_server.db.models import PermissionLogRow
from yantra_server.observe.audit_chain import AuditChain
from yantra_server.observe.tracing import span
from yantra_server.protocol.messages import PermissionRequest
from yantra_server.rpc import EventBus

from .base import Tool

PATHISH_ARGS = ("path", "cwd", "out_path", "source", "dest", "db_path")
CMDISH_ARGS = ("cmd", "code", "command", "query")


@dataclass
class PolicyDecision:
    decision: str  # allow | ask | deny
    rule: PermissionRule | None
    reason: str


class PermissionPolicy:
    def __init__(self, rules: list[PermissionRule]) -> None:
        self.rules = rules

    def evaluate(self, tool: Tool, args: dict[str, Any], mode: str) -> PolicyDecision:
        for rule in self.rules:
            if not self._matches(rule, tool, args, mode):
                continue
            return PolicyDecision(
                decision=rule.decision,
                rule=rule,
                reason=self._describe(rule),
            )
        return PolicyDecision(
            decision="ask" if mode in {"ask", "auto"} else "deny",
            rule=None,
            reason="no rule matched (safe default)",
        )

    def _matches(self, rule: PermissionRule, tool: Tool, args: dict[str, Any], mode: str) -> bool:
        if rule.modes is not None and mode not in rule.modes:
            return False
        if rule.tool != "*" and not fnmatch.fnmatch(tool.name, rule.tool):
            return False
        if rule.side_effects is not None and tool.side_effects != rule.side_effects:
            return False
        if rule.path_glob is not None:
            paths = [str(args[a]) for a in PATHISH_ARGS if args.get(a)]
            if not paths:
                return False
            normalized = [p.replace("\\", "/") for p in paths]
            glob = rule.path_glob
            if not any(
                fnmatch.fnmatch(p, glob) or fnmatch.fnmatch(p.lstrip("./"), glob)
                for p in normalized
            ):
                return False
        if rule.cmd_regex is not None:
            commands = [str(args[a]) for a in CMDISH_ARGS if args.get(a)]
            if not commands:
                return False
            if not any(re.search(rule.cmd_regex, c) for c in commands):
                return False
        return True

    def _describe(self, rule: PermissionRule) -> str:
        bits = [f"tool={rule.tool}"]
        if rule.side_effects:
            bits.append(f"side_effects={rule.side_effects}")
        if rule.path_glob:
            bits.append(f"path={rule.path_glob}")
        if rule.cmd_regex:
            bits.append(f"cmd~/{rule.cmd_regex}/")
        if rule.modes:
            bits.append(f"modes={','.join(rule.modes)}")
        return f"rule({', '.join(bits)}) → {rule.decision}"


class PermissionBroker:
    """Resolves `ask` decisions: prompts the user over the event bus, remembers
    always-this-session grants, records every decision (span + audit + DB)."""

    def __init__(
        self,
        policy: PermissionPolicy,
        bus: EventBus | None,
        db: Database,
        audit: AuditChain,
        *,
        prompt_timeout_s: float = 600.0,
    ) -> None:
        self.policy = policy
        self.bus = bus
        self.db = db
        self.audit = audit
        self.prompt_timeout_s = prompt_timeout_s
        self._pending: dict[str, asyncio.Future[tuple[str, str | None]]] = {}
        self._pending_details: dict[str, dict[str, Any]] = {}
        self._session_grants: dict[str, set[str]] = {}  # run_id -> {grant keys}

    def _grant_key(self, tool: Tool, rule: PermissionRule | None, args: dict[str, Any]) -> str:
        scope = json.dumps(args, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(scope.encode()).hexdigest()
        return f"{tool.name}|{rule.model_dump_json() if rule else '-'}|{digest}"

    def pending_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._pending_details.values() if item["run_id"] == run_id]

    def revoke_run(self, run_id: str) -> None:
        self._session_grants.pop(run_id, None)

    async def check(
        self,
        tool: Tool,
        args: dict[str, Any],
        *,
        mode: str,
        run_id: str | None,
        task_id: str | None,
        explanation: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Returns (allowed, record). Blocks on the user in ask mode."""
        decision = self.policy.evaluate(tool, args, mode)
        record: dict[str, Any] = {
            "rule": decision.rule.model_dump(mode="json") if decision.rule else None,
            "reason": decision.reason,
            "decision": decision.decision,
            "by": "policy",
        }
        outcome = decision.decision
        if outcome == "ask":
            grants = self._session_grants.get(run_id or "", set())
            if self._grant_key(tool, decision.rule, args) in grants:
                outcome, record["by"], record["decision"] = "allow", "session_grant", "allow"
            else:
                outcome = await self._prompt_user(tool, args, decision, run_id, explanation)
                record["by"] = "user"
                record["decision"] = outcome
        self._log(tool, args, record, run_id, task_id)
        return outcome == "allow", record

    async def _prompt_user(
        self,
        tool: Tool,
        args: dict[str, Any],
        decision: PolicyDecision,
        run_id: str | None,
        explanation: str | None,
    ) -> str:
        if self.bus is None:
            return "deny"
        request_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, str | None]] = loop.create_future()
        self._pending[request_id] = future
        self._pending_details[request_id] = {
            "request_id": request_id,
            "run_id": run_id,
            "tool": tool.name,
            "args": args,
            "reason": decision.reason,
            "risk": tool.risk,
            "expires_at": time.time() + self.prompt_timeout_s,
        }
        with span("permission.prompt", tool=tool.name, request_id=request_id) as sp:
            self.bus.publish(
                PermissionRequest(
                    run_id=run_id,
                    request_id=request_id,
                    tool=tool.name,
                    args=args,
                    rule={"reason": decision.reason, "risk": tool.risk},
                    explanation=explanation,
                )
            )
            try:
                answer, _note = await asyncio.wait_for(future, timeout=self.prompt_timeout_s)
            except TimeoutError:
                sp.set("outcome", "timeout→deny")
                return "deny"
            finally:
                self._pending.pop(request_id, None)
                self._pending_details.pop(request_id, None)
            sp.set("outcome", answer)
            if answer == "always":
                self._session_grants.setdefault(run_id or "", set()).add(
                    self._grant_key(tool, decision.rule, args)
                )
                return "allow"
            return "allow" if answer == "once" else "deny"

    def resolve(
        self, request_id: str, decision: str, note: str | None = None, *, run_id: str | None = None
    ) -> bool:
        """Called by the run.approve RPC handler."""
        future = self._pending.get(request_id)
        if decision not in {"once", "always", "deny"}:
            return False
        if run_id is not None and self._pending_details.get(request_id, {}).get("run_id") != run_id:
            return False
        if future is None or future.done():
            return False
        future.set_result((decision, note))
        return True

    def _log(
        self,
        tool: Tool,
        args: dict[str, Any],
        record: dict[str, Any],
        run_id: str | None,
        task_id: str | None,
    ) -> None:
        summary = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(args.items())[:4])
        with self.db.session() as s:
            s.add(
                PermissionLogRow(
                    run_id=run_id,
                    task_id=task_id,
                    tool=tool.name,
                    args_summary=summary,
                    rule=record.get("rule") or {},
                    decision=str(record["decision"]),
                    decided_by=str(record["by"]),
                )
            )
        self.audit.append(
            "user" if record["by"] == "user" else "policy",
            "permission.decision",
            {
                "tool": tool.name,
                "args": summary,
                **{k: v for k, v in record.items() if k != "rule"},
            },
        )
