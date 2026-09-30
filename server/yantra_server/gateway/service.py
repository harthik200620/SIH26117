"""The Model Gateway (SPEC §7.2): the single door through which every model call passes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from yantra_server.artifacts import ArtifactStore
from yantra_server.conductor.budgets import BudgetTracker
from yantra_server.config import YantraConfig
from yantra_server.db.base import Database
from yantra_server.db.models import RouterDecisionRow
from yantra_server.observe.tracing import current_run_context, span

from .cache import EmbeddingCache, ResponseCache, response_cache_key
from .engines.base import (
    ChatEvent,
    ChatMessage,
    ChatResult,
    Constraint,
    ContentDelta,
    Decoding,
    EngineError,
    EngineTimeout,
    FinalEvent,
    ImagePart,
    MalformedOutput,
    ReasoningDelta,
    ToolSpec,
)
from .router import RouteDecision, RouteNeed, Router
from .structured import constraint_for, tighten, validate_output
from .supervisor import Supervisor

DeltaCallback = Callable[[ChatEvent], None]


@dataclass
class ModelRequest:
    role: str
    messages: list[ChatMessage]
    schema_model: type[BaseModel] | None = None
    constraint: Constraint | None = None
    tools: list[ToolSpec] = field(default_factory=list)
    decoding: Decoding | None = None
    budget: BudgetTracker | None = None
    priority: int = 0  # 0 interactive, 1 background
    difficulty: str | None = None
    ladder_rung: int = 0
    prior_failures: int = 0
    force_model: str | None = None  # probes/bench pin a model, bypassing the router
    meta: dict[str, Any] = field(default_factory=dict)
    on_delta: DeltaCallback | None = None


@dataclass
class GatewayResult:
    result: ChatResult
    parsed: Any
    decision: RouteDecision
    cached: bool = False
    attempts: int = 1


class PriorityGate:
    """Bounded concurrency where interactive callers pre-empt background ones."""

    def __init__(self, max_concurrent: int) -> None:
        self._sem = asyncio.Semaphore(max_concurrent)
        self._interactive_waiting = 0
        self._cond = asyncio.Condition()

    async def acquire(self, priority: int) -> None:
        if priority <= 0:
            async with self._cond:
                self._interactive_waiting += 1
            try:
                await self._sem.acquire()
            finally:
                async with self._cond:
                    self._interactive_waiting -= 1
                    self._cond.notify_all()
            return
        async with self._cond:
            while self._interactive_waiting > 0:
                await self._cond.wait()
        await self._sem.acquire()

    def release(self) -> None:
        self._sem.release()


class Gateway:
    def __init__(
        self,
        config: YantraConfig,
        db: Database,
        artifacts: ArtifactStore,
        router: Router,
        supervisor: Supervisor,
        *,
        max_concurrent: int = 8,
    ) -> None:
        self.config = config
        self.db = db
        self.artifacts = artifacts
        self.router = router
        self.supervisor = supervisor
        self.response_cache = ResponseCache(db, ttl_s=config.gateway.cache_ttl_s)
        self.embedding_cache = EmbeddingCache(db)
        self.gate = PriorityGate(max_concurrent)
        self.run_models: dict[str, str] = {}
        self.run_budgets: dict[str, BudgetTracker] = {}

    # ----------------------------------------------------------------- chat

    async def chat(self, request: ModelRequest) -> GatewayResult:
        if request.budget is None:
            request.budget = self.run_budgets.get(current_run_context().run_id or "")
        if request.budget is not None:
            request.budget.check()
        if request.force_model is None and request.role not in {"embed", "rerank", "vision", "ocr"}:
            request.force_model = self.run_models.get(current_run_context().run_id or "")
        constraint = request.constraint
        if constraint is None and request.schema_model is not None:
            constraint = constraint_for(request.schema_model)
        need = RouteNeed(
            role=request.role,
            needs_vision=any(
                isinstance(part, ImagePart)
                for message in request.messages
                if isinstance(message.content, list)
                for part in message.content
            ),
            needs_json=constraint is not None,
            context_tokens=sum(len(m.text()) for m in request.messages) // 4,
            difficulty=request.difficulty,
            ladder_rung=request.ladder_rung,
            prior_failures=request.prior_failures,
        )
        with span("llm.call", kind="llm.call", role=request.role) as sp:
            if request.force_model is not None:
                manifest = self.router.registry.get(request.force_model)
                if manifest is None or (manifest.engine != "mock" and manifest.params_b >= 120):
                    raise EngineError("Selected model is missing or exceeds the parameter cap")
                if need.needs_vision and "vision" not in manifest.capabilities:
                    raise EngineError("Selected model does not support images")
                forced_effort = (
                    request.decoding.reasoning_effort if request.decoding else None
                ) or "low"
                decision = RouteDecision(
                    model=request.force_model,
                    engine=manifest.engine if manifest else "unknown",
                    effort=forced_effort,
                    reason="forced (probe/bench)",
                )
            else:
                decision = self.router.route(need)
            self._record_decision(decision, request)
            sp.set("router", decision.model_dump(mode="json"))
            sp.set("model", decision.model)
            sp.set("engine", decision.engine)

            decoding = (request.decoding or Decoding()).model_copy()
            if decoding.reasoning_effort is None:
                decoding.reasoning_effort = decision.effort
            from .engines.base import EngineChatRequest

            engine_request = EngineChatRequest(
                model=decision.model,
                messages=request.messages,
                tools=request.tools,
                decoding=decoding,
                meta={"role": request.role, **request.meta},
                constraint=constraint,
            )

            prompt_blob = json.dumps(
                [m.model_dump(mode="json") for m in request.messages], ensure_ascii=False
            )
            ctx = current_run_context()
            prompt_artifact = self.artifacts.put_text(
                prompt_blob, kind="prompt", run_id=ctx.run_id, task_id=ctx.task_id
            )
            sp.set("prompt_artifact", prompt_artifact)
            sp.set("prompt_sha256", hashlib.sha256(prompt_blob.encode()).hexdigest())
            sp.set("params", decoding.model_dump(mode="json"))
            sp.set("messages_count", len(request.messages))

            # The response cache is an exact-match, temperature-0 optimisation. A profile can
            # disable it with gateway.cache_ttl_s <= 0 — the mock profile does, because the
            # MockEngine replays stateful scripted sequences and a cache hit would skip the
            # engine and desync the script.
            cache_key = (
                response_cache_key(engine_request) if self.config.gateway.cache_ttl_s > 0 else None
            )
            if cache_key is not None:
                cached = self.response_cache.get(cache_key)
                if cached is not None:
                    sp.set("cache_hit", True)
                    parsed = self._parse(cached, constraint, request.schema_model)
                    return GatewayResult(
                        result=cached, parsed=parsed, decision=decision, cached=True
                    )
            sp.set("cache_hit", False)

            await self.gate.acquire(request.priority)
            try:
                result, attempts = await self._call_with_retries(
                    engine_request, constraint, request, sp
                )
            finally:
                self.gate.release()

            if cache_key is not None:
                self.response_cache.put(cache_key, decision.model, result)
            output_artifact = self.artifacts.put_text(
                result.content, kind="llm_output", run_id=ctx.run_id, task_id=ctx.task_id
            )
            sp.set("output_artifact", output_artifact)
            sp.set("tokens", result.usage.model_dump(mode="json"))
            sp.set("ttft_ms", result.ttft_ms)
            sp.set("latency_ms", result.latency_ms)
            if result.latency_ms:
                sp.set(
                    "tokens_per_s",
                    round(result.usage.completion_tokens / (result.latency_ms / 1000 + 1e-9), 1),
                )
            sp.set("attempts", attempts)
            parsed = self._parse(result, constraint, request.schema_model)
            return GatewayResult(result=result, parsed=parsed, decision=decision, attempts=attempts)

    async def _call_with_retries(
        self,
        engine_request: Any,
        constraint: Constraint | None,
        request: ModelRequest,
        sp: Any,
    ) -> tuple[ChatResult, int]:
        max_retries = self.config.gateway.max_retries
        attempts = 0
        tightened = False
        last_error: Exception | None = None
        while attempts < max_retries + 1:
            attempts += 1
            try:
                engine = await self.supervisor.engine_for_model(engine_request.model)
                result = await self._run_stream(engine, engine_request, request.on_delta)
                if request.budget is not None:
                    request.budget.add_usage(result.usage.prompt_tokens, result.usage.completion_tokens)
                if constraint is not None:
                    try:
                        validate_output(result.content, constraint, None)
                    except MalformedOutput as exc:
                        sp.set(f"malformed_attempt_{attempts}", str(exc)[:400])
                        if tightened:
                            sp.set("error_class", "MalformedOutput")
                            raise
                        # Retry once with a tightened schema and a shorter budget (SPEC §7.3).
                        if (
                            engine_request.constraint is not None
                            and engine_request.constraint.kind == "json_schema"
                            and engine_request.constraint.json_schema is not None
                        ):
                            engine_request.constraint = Constraint(
                                kind="json_schema",
                                json_schema=tighten(engine_request.constraint.json_schema),
                            )
                        engine_request.decoding.max_tokens = max(
                            256, int(engine_request.decoding.max_tokens * 0.75)
                        )
                        tightened = True
                        last_error = exc
                        continue
                return result, attempts
            except (EngineTimeout, EngineError) as exc:
                if isinstance(exc, MalformedOutput):
                    raise
                last_error = exc
                sp.set(f"engine_error_attempt_{attempts}", str(exc)[:400])
                if attempts >= max_retries + 1:
                    break
                await asyncio.sleep(min(0.5 * (2**attempts), 8.0) * (0.5 + random.random()))
        sp.set("error_class", type(last_error).__name__ if last_error else "unknown")
        raise last_error if last_error else EngineError("gateway: exhausted retries")

    async def _run_stream(
        self, engine: Any, engine_request: Any, on_delta: DeltaCallback | None
    ) -> ChatResult:
        result: ChatResult | None = None
        async for event in engine.chat_stream(engine_request):
            if on_delta is not None and isinstance(event, ContentDelta | ReasoningDelta):
                on_delta(event)
            if isinstance(event, FinalEvent):
                result = event.result
        if result is None:
            raise EngineError("engine stream ended without a final result")
        return result

    def _parse(
        self,
        result: ChatResult,
        constraint: Constraint | None,
        schema_model: type[BaseModel] | None,
    ) -> Any:
        if constraint is None:
            return result.content
        return validate_output(result.content, constraint, schema_model)

    def _record_decision(self, decision: RouteDecision, request: ModelRequest) -> None:
        ctx = current_run_context()
        with self.db.session() as s:
            s.add(
                RouterDecisionRow(
                    run_id=ctx.run_id,
                    task_id=ctx.task_id,
                    role=request.role,
                    chosen=decision.model,
                    rejected=[r.model_dump(mode="json") for r in decision.rejected],
                    reason=decision.reason,
                )
            )

    # ----------------------------------------------------------------- pooling

    async def embed(
        self, texts: list[str], *, role: str = "embed", instruction: str | None = None
    ) -> list[list[float]]:
        if not texts:
            return []
        decision = self.router.route(RouteNeed(role=role, needs_json=False))
        with span("llm.embed", kind="llm.call", role=role, model=decision.model) as sp:
            keyed = [f"{instruction or ''}\x1f{t}" for t in texts]
            hits = self.embedding_cache.get_many(decision.model, keyed)
            missing = [i for i in range(len(texts)) if i not in hits]
            sp.set("count", len(texts))
            sp.set("cache_hits", len(hits))
            if missing:
                await self.gate.acquire(1)
                try:
                    engine = await self.supervisor.engine_for_model(decision.model)
                    fresh = await engine.embed(decision.model, [texts[i] for i in missing], instruction)
                finally:
                    self.gate.release()
                normalized = self.embedding_cache.put_many(
                    decision.model, [(keyed[i], v) for i, v in zip(missing, fresh, strict=True)]
                )
                for i, v in zip(missing, normalized, strict=True):
                    hits[i] = v
            return [hits[i] for i in range(len(texts))]

    async def rerank(
        self, query: str, documents: list[str], *, role: str = "rerank"
    ) -> list[float]:
        if not documents:
            return []
        decision = self.router.route(RouteNeed(role=role))
        with span("llm.rerank", kind="llm.call", role=role, model=decision.model) as sp:
            sp.set("count", len(documents))
            await self.gate.acquire(1)
            try:
                engine = await self.supervisor.engine_for_model(decision.model)
                return await engine.rerank(decision.model, query, documents)
            finally:
                self.gate.release()

    # ----------------------------------------------------------------- helpers

    async def classify(
        self, text: str, choices: list[str], *, instruction: str, priority: int = 0
    ) -> str:
        """Cheap utility-role classification under a choice constraint (cached)."""
        result = await self.chat(
            ModelRequest(
                role="utility",
                messages=[
                    ChatMessage(role="system", content=instruction),
                    ChatMessage(role="user", content=text),
                ],
                constraint=Constraint(kind="choice", choices=choices),
                decoding=Decoding(temperature=0.0, max_tokens=16),
                priority=priority,
            )
        )
        return str(result.parsed)
