"""Every JSON-RPC request/response and server notification, typed.

The TUI reconnects with the last seen `seq` per run; the server replays missed
notifications from its ring buffer (see app.py).
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

Mode = Literal["ask", "auto", "plan"]

# ------------------------------------------------------------------ requests


class PingParams(BaseModel):
    pass


class PingResult(BaseModel):
    pong: bool = True
    version: str = ""


class SessionCreateParams(BaseModel):
    workspace: str
    collections: list[str] = Field(default_factory=list)
    mode: Mode = "ask"
    title: str | None = None


class SessionCreateResult(BaseModel):
    session_id: str


class SessionPromptParams(BaseModel):
    session_id: str
    text: str
    attachments: list[str] = Field(default_factory=list)  # file paths (images etc.)
    mode: Mode | None = None  # override the session mode for this run
    budget_overrides: dict[str, int] | None = None  # max_tokens / max_seconds / max_tool_calls


class SessionPromptResult(BaseModel):
    run_id: str


class RunCancelParams(BaseModel):
    run_id: str


class RunApproveParams(BaseModel):
    run_id: str
    request_id: str
    decision: Literal["once", "always", "deny"]
    note: str | None = None
    answers: list[str] | None = None  # for question notifications


class RunPlanUpdateParams(BaseModel):
    run_id: str
    plan: dict[str, Any]


class SessionListParams(BaseModel):
    limit: int = 50


class SessionInfo(BaseModel):
    session_id: str
    title: str | None = None
    workspace: str
    mode: Mode
    status: str
    created_at: str
    updated_at: str
    last_goal: str | None = None


class SessionListResult(BaseModel):
    sessions: list[SessionInfo] = Field(default_factory=list)


class SessionResumeParams(BaseModel):
    session_id: str
    last_seq: int = 0


class SessionForkParams(BaseModel):
    session_id: str


class ModelsListParams(BaseModel):
    pass


class ModelInfo(BaseModel):
    id: str
    family: str | None = None
    engine: str
    params_b: float = 0
    capabilities: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    status: str = "registered"
    healthy: bool | None = None
    vram_gb: float = 0
    quant: str | None = None
    local: bool = False  # came from the machine-local overlay registry
    path: str | None = None  # weights path, when known
    probes_passed: int | None = None  # None = never probed
    probes_total: int | None = None


class ModelsListResult(BaseModel):
    models: list[ModelInfo] = Field(default_factory=list)


class ModelsProbeParams(BaseModel):
    model_id: str


class ModelsAddParams(BaseModel):
    path: str
    allow_large: bool = False


class ModelsRemoveParams(BaseModel):
    model_id: str


class RagSearchParams(BaseModel):
    query: str
    collections: list[str] = Field(default_factory=list)
    k: int = 10
    mode: Literal["hybrid", "lexical", "dense", "visual"] = "hybrid"


class SealStatusParams(BaseModel):
    pass


class TraceGetParams(BaseModel):
    run_id: str


class ConfigGetParams(BaseModel):
    pass


class MemoryListParams(BaseModel):
    kind: str | None = None


class MemoryReviewParams(BaseModel):
    memory_id: str
    action: Literal["approve", "purge"]


class SkillsListParams(BaseModel):
    pass


class SkillsApproveParams(BaseModel):
    skill_id: str
    action: Literal["approve", "reject"]


REQUESTS: dict[str, type[BaseModel]] = {
    "ping": PingParams,
    "session.create": SessionCreateParams,
    "session.prompt": SessionPromptParams,
    "session.list": SessionListParams,
    "session.resume": SessionResumeParams,
    "session.fork": SessionForkParams,
    "run.cancel": RunCancelParams,
    "run.approve": RunApproveParams,
    "run.plan.update": RunPlanUpdateParams,
    "models.list": ModelsListParams,
    "models.probe": ModelsProbeParams,
    "models.add": ModelsAddParams,
    "models.remove": ModelsRemoveParams,
    "rag.search": RagSearchParams,
    "seal.status": SealStatusParams,
    "seal.verify": SealStatusParams,
    "seal.demo": SealStatusParams,
    "trace.get": TraceGetParams,
    "config.get": ConfigGetParams,
    "memory.list": MemoryListParams,
    "memory.review": MemoryReviewParams,
    "skills.list": SkillsListParams,
    "skills.approve": SkillsApproveParams,
}


RESULTS: dict[str, type[BaseModel]] = {
    "ping": PingResult,
    "session.create": SessionCreateResult,
    "session.prompt": SessionPromptResult,
    "session.list": SessionListResult,
    "models.list": ModelsListResult,
}


# ------------------------------------------------------------------ notifications


class Notification(BaseModel):
    """Base for server→client events. All carry run_id and a monotonic per-run seq."""

    method: ClassVar[str] = ""
    run_id: str | None = None
    seq: int = 0


class AssistantDelta(Notification):
    method: ClassVar[str] = "assistant.delta"
    text: str


class ThinkingDelta(Notification):
    method: ClassVar[str] = "thinking.delta"
    text: str


class PlanUpdated(Notification):
    method: ClassVar[str] = "plan.updated"
    plan: dict[str, Any]


class TaskUpdated(Notification):
    method: ClassVar[str] = "task.updated"
    task: dict[str, Any]


class ToolStarted(Notification):
    method: ClassVar[str] = "tool.started"
    step_id: str
    task_id: str | None = None
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolOutputDelta(Notification):
    method: ClassVar[str] = "tool.output.delta"
    step_id: str
    text: str


class ToolFinished(Notification):
    method: ClassVar[str] = "tool.finished"
    step_id: str
    tool: str = ""
    summary: str = ""
    artifact_id: str | None = None
    ok: bool = True


class VerifyResultNote(Notification):
    method: ClassVar[str] = "verify.result"
    task_id: str
    report: dict[str, Any]


class EscalationNote(Notification):
    method: ClassVar[str] = "escalation"
    task_id: str
    rung: str
    attempt: int = 0


class PermissionRequest(Notification):
    method: ClassVar[str] = "permission.request"
    request_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    rule: dict[str, Any] = Field(default_factory=dict)
    explanation: str | None = None


class QuestionNote(Notification):
    method: ClassVar[str] = "question"
    request_id: str
    questions: list[str]


class ImageNote(Notification):
    method: ClassVar[str] = "image"
    artifact_id: str
    dims: list[int] = Field(default_factory=list)  # [w, h]
    region: list[int] | None = None  # [x, y, w, h]
    caption: str | None = None


class SealEventNote(Notification):
    method: ClassVar[str] = "seal.event"
    kind: str
    process: str = ""
    dest: str = ""
    port: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class BudgetWarning(Notification):
    method: ClassVar[str] = "budget.warning"
    budget: str
    used: float
    limit: float


class RunStats(Notification):
    """Live status-line data, emitted after every executor step."""

    method: ClassVar[str] = "run.stats"
    tokens_in: int = 0
    tokens_out: int = 0
    context_pct: float = 0.0
    elapsed_s: float = 0.0
    cost_saved_inr: float = 0.0
    active_model: str = ""


class RunFinished(Notification):
    method: ClassVar[str] = "run.finished"
    response_kind: Literal["workflow", "conversation"] = "workflow"
    status: str = "done"
    summary: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)
    budget_used: dict[str, Any] = Field(default_factory=dict)


class ErrorNote(Notification):
    method: ClassVar[str] = "error"
    code: str
    message: str
    hint: str | None = None


NOTIFICATIONS: dict[str, type[Notification]] = {
    cls.method: cls
    for cls in (
        AssistantDelta,
        ThinkingDelta,
        PlanUpdated,
        TaskUpdated,
        ToolStarted,
        ToolOutputDelta,
        ToolFinished,
        VerifyResultNote,
        EscalationNote,
        PermissionRequest,
        QuestionNote,
        ImageNote,
        SealEventNote,
        BudgetWarning,
        RunStats,
        RunFinished,
        ErrorNote,
    )
}
