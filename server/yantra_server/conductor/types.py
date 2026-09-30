"""Conductor core types (SPEC §8.1). Every model-facing shape is schema-constrained."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# ------------------------------------------------------------------ goal intake


class DeliverableSpec(BaseModel):
    type: Literal["docx", "xlsx", "pptx", "pdf", "md", "png", "code", "text"] = "md"
    name: str
    schema_id: str | None = None  # binds to templates/schemas/<id>.json for render tasks
    path_hint: str | None = None


class ContextRef(BaseModel):
    kind: Literal["file", "folder", "collection", "url_local"] = "file"
    ref: str


class GoalSpec(BaseModel):
    objective: str
    deliverables: list[DeliverableSpec] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    context_refs: list[ContextRef] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ checks


class FileExistsCheck(BaseModel):
    kind: Literal["file_exists"] = "file_exists"
    path: str


class ToolSucceededCheck(BaseModel):
    kind: Literal["tool_succeeded"] = "tool_succeeded"
    tool: Literal["python", "run_tests", "bash"]


class SchemaValidCheck(BaseModel):
    kind: Literal["schema_valid"] = "schema_valid"
    path: str
    schema_id: str


class TestsPassCheck(BaseModel):
    kind: Literal["tests_pass"] = "tests_pass"
    cmd: str = "auto"
    cwd: str = "."


class CommandSucceedsCheck(BaseModel):
    kind: Literal["command_succeeds"] = "command_succeeds"
    cmd: str


class CitationCoverageCheck(BaseModel):
    kind: Literal["citation_coverage"] = "citation_coverage"
    min_ratio: float = 0.9


class ClaimsEntailedCheck(BaseModel):
    kind: Literal["claims_entailed"] = "claims_entailed"
    min_ratio: float = 0.85


class ImageContainsCheck(BaseModel):
    kind: Literal["image_contains"] = "image_contains"
    path: str
    expectations: list[str] = Field(default_factory=list)


class RubricCheck(BaseModel):
    kind: Literal["rubric"] = "rubric"
    rubric_id: str = "default"
    min_score: int = 80


class DiffAppliesCheck(BaseModel):
    kind: Literal["diff_applies"] = "diff_applies"
    patch_ref: str


class TableTotalsCheck(BaseModel):
    kind: Literal["table_totals"] = "table_totals"
    path: str
    rules: list[str] = Field(default_factory=list)  # e.g. "sum(col:count) == cell(B12)"


class GraphValidCheck(BaseModel):
    kind: Literal["graph_valid"] = "graph_valid"
    pid_graph_ref: str


class CustomCheck(BaseModel):
    kind: Literal["custom"] = "custom"
    plugin: str
    args: dict[str, Any] = Field(default_factory=dict)


Check = Annotated[
    FileExistsCheck
    | ToolSucceededCheck
    | SchemaValidCheck
    | TestsPassCheck
    | CommandSucceedsCheck
    | CitationCoverageCheck
    | ClaimsEntailedCheck
    | ImageContainsCheck
    | RubricCheck
    | DiffAppliesCheck
    | TableTotalsCheck
    | GraphValidCheck
    | CustomCheck,
    Field(discriminator="kind"),
]


# ------------------------------------------------------------------ plan


class TaskBudget(BaseModel):
    max_steps: int = 20
    max_tokens: int = 60_000
    max_seconds: int = 600
    max_retries: int = 3


class ArtifactSpec(BaseModel):
    name: str
    type: str = "text"  # file kind or deliverable type
    path_hint: str | None = None
    schema_id: str | None = None


class PlanTask(BaseModel):
    id: str = Field(pattern=r"^t\d{1,2}$", description="t1, t2, … in execution order")
    title: str = Field(max_length=120)
    intent: str = Field(
        max_length=13000, description="What done looks like, including original user constraints"
    )
    role: str = Field(description="Agent name from the roster")
    inputs: list[str] = Field(
        default_factory=list, description="Task ids or context refs this needs"
    )
    outputs: list[ArtifactSpec] = Field(default_factory=list)
    acceptance: list[Check] = Field(min_length=1)
    budget: TaskBudget = Field(default_factory=TaskBudget)
    model_hint: str | None = None


class Plan(BaseModel):
    tasks: list[PlanTask] = Field(min_length=1, max_length=25)
    edges: list[list[str]] = Field(
        default_factory=list, description="[from_id, to_id] dependency pairs"
    )
    rationale: str = Field(default="", max_length=1500)
    version: int = 1


class PlanCritique(BaseModel):
    ok: bool
    findings: list[str] = Field(default_factory=list, max_length=10)


# ------------------------------------------------------------------ step decisions


class Claim(BaseModel):
    text: str
    citations: list[str] = Field(
        default_factory=list, description="chunk ids or artifact_id:locator strings"
    )
    kind: Literal["fact", "inference", "computed"] = "fact"


class SelfCheck(BaseModel):
    score: int = Field(ge=0, le=100)
    notes: str = Field(default="", max_length=500)


class FinishArgs(BaseModel):
    summary: str = Field(max_length=2000)
    artifacts: list[str] = Field(default_factory=list, description="paths or artifact ids produced")
    claims: list[Claim] = Field(default_factory=list)
    self_check: SelfCheck = Field(default_factory=lambda: SelfCheck(score=70))


class StepAction(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class StepDecision(BaseModel):
    thought: str = Field(max_length=400)
    action: StepAction


# ------------------------------------------------------------------ verification


class CheckResult(BaseModel):
    check: dict[str, Any]
    passed: bool
    detail: str = ""


class ReviewerFailure(BaseModel):
    what: str
    where: str = ""
    why: str = ""


class ReviewedCriterion(BaseModel):
    requirement: str = Field(min_length=1, max_length=350)
    evidence: str = Field(min_length=1, max_length=650)
    status: Literal["met", "missing", "incorrect", "unverifiable"]


class EvidenceReview(BaseModel):
    criteria: list[ReviewedCriterion] = Field(min_length=1, max_length=12)
    all_requirements_covered: bool


class ReviewerReport(BaseModel):
    score: int = Field(ge=0, le=100)
    failures: list[ReviewerFailure] = Field(default_factory=list)
    fix_instructions: list[str] = Field(default_factory=list)
    verdict: Literal["pass", "fail"]
    criteria: list[ReviewedCriterion] = Field(default_factory=list)
    method: Literal["model", "deterministic"] = "model"


class VerificationOutcome(BaseModel):
    task_id: str
    attempt: int
    checks: list[CheckResult] = Field(default_factory=list)
    reviewer: ReviewerReport | None = None
    verdict: Literal["pass", "fail"]
    escalation_applied: str | None = None

    def failure_summary(self) -> str:
        lines = [f"- {c.check.get('kind')}: {c.detail}" for c in self.checks if not c.passed]
        if self.reviewer and self.reviewer.verdict == "fail":
            lines += [f"- reviewer: {f.what} ({f.why})" for f in self.reviewer.failures[:5]]
            lines += [f"  fix: {i}" for i in self.reviewer.fix_instructions[:5]]
        return "\n".join(lines) or "unspecified failure"


# ------------------------------------------------------------------ progress ledger


class LedgerState(BaseModel):
    facts_established: list[str] = Field(default_factory=list, max_length=20)
    done: list[str] = Field(default_factory=list, max_length=20)
    remaining: list[str] = Field(default_factory=list, max_length=20)
    blockers: list[str] = Field(default_factory=list, max_length=10)
    stuck: bool = False
    next_action_hint: str = Field(default="", max_length=300)


class DifficultyLabel(BaseModel):
    label: Literal["trivial", "easy", "normal", "hard", "extreme"]
