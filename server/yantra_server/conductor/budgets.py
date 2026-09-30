"""Token/time/tool budgets shared by the gateway and the executor (SPEC §16.4)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    def __init__(self, budget: str, used: float, limit: float) -> None:
        super().__init__(f"budget exceeded: {budget} used {used:.0f} of {limit:.0f}")
        self.budget = budget
        self.used = used
        self.limit = limit


@dataclass
class BudgetTracker:
    """Thread-safe usage counters against limits; warn once at warn_ratio."""

    max_tokens: int = 400_000
    max_seconds: float = 3600.0
    max_tool_calls: int = 400
    max_sandbox_cpu_s: float = 1200.0
    warn_ratio: float = 0.8

    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls_used: int = 0
    sandbox_cpu_used: float = 0.0
    prior_seconds: float = 0.0
    on_change: Callable[[dict[str, float]], None] | None = field(default=None, repr=False)
    started_at: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _warned: set[str] = field(default_factory=set, repr=False)

    def elapsed_s(self) -> float:
        return self.prior_seconds + time.monotonic() - self.started_at

    def persist(self) -> None:
        if self.on_change is not None:
            self.on_change(self.snapshot())

    def restore(self, usage: dict[str, float]) -> None:
        """Restore cumulative usage; time spent offline is not execution time."""
        for name in ("tokens_used", "prompt_tokens", "completion_tokens", "tool_calls_used"):
            setattr(self, name, max(0, int(usage.get(name, 0))))
        self.sandbox_cpu_used = max(0.0, float(usage.get("sandbox_cpu_used", 0)))
        self.prior_seconds = max(0.0, float(usage.get("seconds_used", 0)))
        self.started_at = time.monotonic()

    def add_tokens(self, n: int) -> None:
        with self._lock:
            self.tokens_used += n
        self.persist()

    def add_usage(self, prompt: int, completion: int) -> None:
        with self._lock:
            self.tokens_used += prompt + completion
            self.prompt_tokens += prompt
            self.completion_tokens += completion
        self.persist()

    def add_tool_call(self) -> None:
        with self._lock:
            self.tool_calls_used += 1
        self.persist()

    def add_sandbox_cpu(self, seconds: float) -> None:
        with self._lock:
            self.sandbox_cpu_used += seconds
        self.persist()

    def check(self, *, raise_on_exhausted: bool = True) -> list[str]:
        """Return budget names newly past warn_ratio; raise when any limit is exhausted."""
        warnings: list[str] = []
        usages = {
            "tokens": (float(self.tokens_used), float(self.max_tokens)),
            "seconds": (self.elapsed_s(), self.max_seconds),
            "tool_calls": (float(self.tool_calls_used), float(self.max_tool_calls)),
            "sandbox_cpu_s": (self.sandbox_cpu_used, self.max_sandbox_cpu_s),
        }
        for name, (used, limit) in usages.items():
            if limit <= 0:
                continue
            if used >= limit and raise_on_exhausted:
                raise BudgetExceeded(name, used, limit)
            if used >= limit * self.warn_ratio and name not in self._warned:
                with self._lock:
                    self._warned.add(name)
                warnings.append(name)
        return warnings

    def exhausted(self) -> bool:
        try:
            self.check(raise_on_exhausted=True)
        except BudgetExceeded:
            return True
        return False

    def snapshot(self) -> dict[str, float]:
        return {
            "tokens_used": self.tokens_used,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tokens_max": self.max_tokens,
            "seconds_used": round(self.elapsed_s(), 1),
            "seconds_max": self.max_seconds,
            "tool_calls_used": self.tool_calls_used,
            "tool_calls_max": self.max_tool_calls,
            "sandbox_cpu_used": round(self.sandbox_cpu_used, 1),
            "sandbox_cpu_max": self.max_sandbox_cpu_s,
        }
