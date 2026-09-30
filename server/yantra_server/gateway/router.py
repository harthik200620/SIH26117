"""Deterministic router (SPEC §7.5): capability/health filters + escalation-aware choice."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from .registry import ModelManifest, ModelRegistry

Effort = Literal["low", "medium", "high", "xhigh"]


class EscalationRung(BaseModel):
    action: Literal["raise_effort", "best_of_n", "switch_role", "replan"]
    to: str | None = None
    n: int = 3
    select_with: str | None = None


class RoutingPolicy(BaseModel):
    roles: dict[str, list[str]]
    effort: dict[str, Effort] = Field(default_factory=dict)
    escalation: list[EscalationRung] = Field(default_factory=list)
    difficulty_signals: list[str] = Field(default_factory=list)
    difficulty: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> RoutingPolicy:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def apply_local_overlay(self, path: Path) -> None:
        """Machine-local candidates (one-click integrations) go to the FRONT of their roles.

        The shipped routing.yaml stays pristine; this overlay lives in the data dir."""
        if not path.is_file():
            return
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return
        for role, ids in (data.get("roles") or {}).items():
            candidates = self.roles.setdefault(str(role), [])
            for model_id in reversed([str(i) for i in ids]):
                if model_id in candidates:
                    candidates.remove(model_id)
                candidates.insert(0, model_id)

    def persist_local_front(self, path: Path, model_id: str, roles: list[str]) -> None:
        """Record a machine-local front-of-role candidate in the overlay file."""
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                data = {}
        role_map: dict[str, list[str]] = {
            str(k): [str(i) for i in v] for k, v in (data.get("roles") or {}).items()
        }
        for role in roles:
            ids = role_map.setdefault(role, [])
            if model_id in ids:
                ids.remove(model_id)
            ids.insert(0, model_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Machine-local routing overlay (one-click integrations go first for their roles).\n"
            + yaml.safe_dump({"roles": role_map}, sort_keys=False),
            encoding="utf-8",
        )

    def start_rung_for(self, difficulty: str | None) -> int:
        table = self.difficulty.get("start_rung", {})
        return int(table.get(difficulty, 0)) if difficulty else 0


class RouteNeed(BaseModel):
    role: str
    needs_vision: bool = False
    needs_json: bool = False
    context_tokens: int = 0
    difficulty: str | None = None
    ladder_rung: int = 0
    prior_failures: int = 0


class Rejection(BaseModel):
    model: str
    reason: str


class RouteDecision(BaseModel):
    model: str
    engine: str
    effort: Effort
    reason: str
    rejected: list[Rejection] = Field(default_factory=list)
    ladder_rung: int = 0
    best_of_n: int = 1


class NoRouteAvailable(Exception):
    def __init__(self, need: RouteNeed, rejected: list[Rejection]) -> None:
        detail = "; ".join(f"{r.model}: {r.reason}" for r in rejected) or "no candidates"
        super().__init__(f"no model available for role {need.role!r} ({detail})")
        self.rejected = rejected


# Roles shown first in /api/routing/assignments; anything else follows alphabetically.
ASSIGNMENT_ROLE_ORDER = ("planner", "executor", "reviewer", "router", "utility")


class RoleAssignment(BaseModel):
    """What live routing would pick for one role right now (GET /api/routing/assignments)."""

    role: str
    model_id: str | None = None
    source: Literal["policy", "fallback", "none"]
    reason: str
    probes_passed: int | None = None
    probes_total: int | None = None


class Router:
    def __init__(
        self,
        policy: RoutingPolicy,
        registry: ModelRegistry,
        is_available: Callable[[str], bool],
        *,
        allow_mock: bool = False,
    ) -> None:
        self.policy = policy
        self.registry = registry
        self.is_available = is_available
        self.allow_mock = allow_mock

    def effective_rung(self, need: RouteNeed) -> int:
        return max(need.ladder_rung, self.policy.start_rung_for(need.difficulty))

    def route(self, need: RouteNeed) -> RouteDecision:
        rung = self.effective_rung(need)
        role = need.role
        best_of_n = 1
        # The ladder can redirect the role (switch_role) or fan out (best_of_n).
        for rung_index in range(min(rung, len(self.policy.escalation))):
            action = self.policy.escalation[rung_index]
            if action.action == "switch_role" and action.to:
                role = action.to
            if action.action == "best_of_n" and rung_index == rung - 1:
                best_of_n = action.n

        manifest, rejected = self._first_policy_candidate(need, role)
        if manifest is not None:
            effort = self._effort(need, rung)
            reason_text = self._reason_text(need, role, rung, rejected)
            return RouteDecision(
                model=manifest.id,
                engine=manifest.engine,
                effort=effort,
                reason=reason_text,
                rejected=rejected,
                ladder_rung=rung,
                best_of_n=best_of_n,
            )
        # Best-available fallback: a small installation may have one real model and none of
        # the role's listed candidates. Rather than fail, rank every available model that
        # meets the need and take the strongest (probe scores, then size). Mock still only
        # wins when allowed and nothing real exists.
        fallback = self._best_available(need, exclude={r.model for r in rejected})
        if fallback is not None:
            effort = self._effort(need, rung)
            return RouteDecision(
                model=fallback.id,
                engine=fallback.engine,
                effort=effort,
                reason=f"role={need.role} (best-available fallback: no listed candidate up)",
                rejected=rejected,
                ladder_rung=rung,
                best_of_n=best_of_n,
            )
        raise NoRouteAvailable(need, rejected)

    def _first_policy_candidate(
        self, need: RouteNeed, role: str
    ) -> tuple[ModelManifest | None, list[Rejection]]:
        """First candidate in the role's configured list that can serve right now."""
        candidates = self.policy.roles.get(role) or self.policy.roles.get(need.role) or []
        rejected: list[Rejection] = []
        for model_id in candidates:
            manifest = self.registry.get(model_id)
            if manifest is None:
                rejected.append(Rejection(model=model_id, reason="not in registry"))
                continue
            reason = self._capability_gap(manifest, need)
            if reason:
                rejected.append(Rejection(model=model_id, reason=reason))
                continue
            if manifest.engine == "mock" and not self.allow_mock:
                rejected.append(Rejection(model=model_id, reason="mock engine disallowed (sealed)"))
                continue
            if not self.is_available(model_id):
                rejected.append(Rejection(model=model_id, reason="engine not healthy"))
                continue
            return manifest, rejected
        return None, rejected

    def current_assignments(self) -> list[RoleAssignment]:
        """Resolve, for every known role, the model live routing would pick right now.

        Covers each role the policy knows plus each role any registered model claims,
        through the same candidate order, availability filters and best-available
        fallback that route() uses (no escalation ladder: that is per-request)."""
        roles = set(self.policy.roles)
        for manifest in self.registry.all():
            roles.update(manifest.roles)
        ordered = [r for r in ASSIGNMENT_ROLE_ORDER if r in roles]
        ordered += sorted(roles.difference(ASSIGNMENT_ROLE_ORDER))
        assignments: list[RoleAssignment] = []
        for role in ordered:
            need = RouteNeed(role=role)
            picked, rejected = self._first_policy_candidate(need, role)
            if picked is not None:
                assignments.append(
                    self._assignment(role, picked, "policy", "first available policy candidate")
                )
                continue
            fallback = self._best_available(need, exclude={r.model for r in rejected})
            if fallback is not None:
                assignments.append(
                    self._assignment(
                        role, fallback, "fallback", "best available model by probe evidence"
                    )
                )
                continue
            assignments.append(
                RoleAssignment(
                    role=role,
                    model_id=None,
                    source="none",
                    reason="no registered model can serve this role right now",
                )
            )
        return assignments

    @staticmethod
    def _assignment(
        role: str, manifest: ModelManifest, source: Literal["policy", "fallback"], reason: str
    ) -> RoleAssignment:
        passed, total = manifest.probe_counts()
        return RoleAssignment(
            role=role,
            model_id=manifest.id,
            source=source,
            reason=reason,
            probes_passed=passed,
            probes_total=total,
        )

    def _best_available(self, need: RouteNeed, exclude: set[str]) -> ModelManifest | None:
        scored: list[tuple[float, str, ModelManifest]] = []
        for manifest in self.registry.all():
            if manifest.id in exclude or self._capability_gap(manifest, need):
                continue
            if manifest.engine == "mock":
                continue  # handled below, only as the very last resort
            if "chat" not in manifest.capabilities and need.role not in (
                "embed",
                "embed_visual",
                "rerank",
                "rerank_visual",
                "ocr",
            ):
                continue
            if not self.is_available(manifest.id):
                continue
            probe_score = sum(1.0 for v in manifest.probes.values() if v == "pass") + sum(
                float(v) / 100
                for k, v in manifest.probes.items()
                if k.endswith("_score") and isinstance(v, int | float)
            )
            scored.append((probe_score + manifest.params_b / 1000, manifest.id, manifest))
        if scored:
            scored.sort(reverse=True)
            return scored[0][2]
        if self.allow_mock:
            mock = self.registry.get("mock")
            if mock is not None and self.is_available("mock"):
                return mock
        return None

    def _capability_gap(self, manifest: ModelManifest, need: RouteNeed) -> str | None:
        caps = set(manifest.capabilities)
        required = {"embed": "embed", "embed_visual": "embed", "rerank": "rerank", "rerank_visual": "rerank", "vision": "vision", "ocr": "ocr"}.get(need.role)
        if required and required not in caps:
            return f"no {required} capability"
        if manifest.params_b >= 120 and manifest.engine != "mock":
            return "exceeds the conservative 120B parameter cap"
        if need.needs_vision and "vision" not in caps:
            return "no vision capability"
        if need.needs_json and not ({"json", "embed", "rerank", "ocr"} & caps):
            return "no structured-output capability"
        if need.context_tokens and manifest.serve_context_len < need.context_tokens:
            return f"context {manifest.serve_context_len} < needed {need.context_tokens}"
        return None

    def _effort(self, need: RouteNeed, rung: int) -> Effort:
        base: Effort = self.policy.effort.get(need.role, "low")
        if (
            rung >= 1
            and self.policy.escalation
            and self.policy.escalation[0].action == "raise_effort"
        ):
            raised = self.policy.escalation[0].to
            if raised in ("low", "medium", "high", "xhigh"):
                return raised  # type: ignore[return-value]
            return "high"
        return base

    def _reason_text(self, need: RouteNeed, role: str, rung: int, rejected: list[Rejection]) -> str:
        parts = [f"role={need.role}"]
        if role != need.role:
            parts.append(f"ladder→{role}")
        if rung:
            parts.append(f"rung={rung}")
        if need.difficulty:
            parts.append(f"difficulty={need.difficulty}")
        if need.needs_vision:
            parts.append("vision")
        if need.prior_failures:
            parts.append(f"failures={need.prior_failures}")
        if rejected:
            parts.append(f"skipped={len(rejected)}")
        return " ".join(parts)
