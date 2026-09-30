"""The tool contract (SPEC §9.1). Descriptions are written for a small model."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from yantra_server.conductor.budgets import BudgetTracker
    from yantra_server.sandbox import Sandbox
    from yantra_server.state import AppState

SideEffects = Literal["none", "read", "write", "exec", "render", "delete"]
Risk = Literal["low", "medium", "high"]


class ToolError(Exception):
    """Tool-level failure surfaced to the model as a failed observation, never a crash."""


class ToolResult(BaseModel):
    ok: bool = True
    summary: str = ""  # one line for the TUI tool card
    content: str = ""  # full text result; the runtime offloads large ones to an artifact
    data: dict[str, Any] = Field(default_factory=dict)  # structured payload for the harness
    artifact_id: str | None = None
    error: str | None = None
    images: list[str] = Field(default_factory=list)  # artifact ids to attach as image parts

    @classmethod
    def fail(cls, error: str, summary: str | None = None) -> ToolResult:
        return cls(ok=False, error=error, summary=summary or error[:120])


@dataclass
class ToolContext:
    workspace: Path
    state: AppState
    sandbox: Sandbox
    mode: str = "ask"
    run_id: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    idempotency_key: str = ""
    budget: BudgetTracker | None = None
    allowed_tools: list[str] = field(default_factory=list)
    emit_output: Callable[[str], None] | None = None
    ask_user: Callable[[list[str]], Awaitable[list[str] | None]] | None = None
    gpu: bool = False

    def resolve_path(self, user_path: str) -> Path:
        """Workspace-scoped path resolution; symlink-escape-proof (SPEC §9.2)."""
        if user_path.startswith(("\\\\", "//")):
            raise ToolError("Network shares are disabled")
        candidate = Path(user_path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        workspace = self.workspace.resolve()
        if resolved != workspace and workspace not in resolved.parents:
            raise ToolError(f"path escapes the workspace: {user_path}")
        return resolved

    def emit(self, text: str) -> None:
        if self.emit_output is not None:
            self.emit_output(text)


class Tool(ABC):
    """One capability. Args/Result are Pydantic; the registry builds schemas from Args.

    name/description are plain attributes (not ClassVar) so adapters like MCPTool can
    set them per instance; built-ins still define them at class level."""

    name: str
    description: str
    Args: ClassVar[type[BaseModel]]
    side_effects: ClassVar[SideEffects] = "none"
    risk: ClassVar[Risk] = "low"
    needs_sandbox: ClassVar[bool] = False
    idempotent: ClassVar[bool] = True
    fewshots_path: ClassVar[str | None] = None

    @abstractmethod
    async def run(self, args: Any, ctx: ToolContext) -> ToolResult: ...
