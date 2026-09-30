"""ORM models for every table in SPEC §19.3."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_id, utcnow


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RunEventRow(Base):
    __tablename__ = "run_events"
    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    frame: Mapped[dict[str, Any]] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------ sessions & runs


class SessionRow(TimestampMixin, Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str | None] = mapped_column(Text)
    workspace_path: Mapped[str] = mapped_column(Text)
    collections: Mapped[list[Any]] = mapped_column(default=list)
    mode: Mapped[str] = mapped_column(String(8), default="ask")
    status: Mapped[str] = mapped_column(String(16), default="active")
    parent_session_id: Mapped[str | None] = mapped_column(String(32))
    user: Mapped[str | None] = mapped_column(String(128))
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)


class RunRow(TimestampMixin, Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    goal_text: Mapped[str] = mapped_column(Text)
    goal_spec: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    plan: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="created", index=True)
    mode: Mapped[str] = mapped_column(String(8), default="ask")
    budgets: Mapped[dict[str, Any]] = mapped_column(default=dict)
    budget_used: Mapped[dict[str, Any]] = mapped_column(default=dict)
    workspace_path: Mapped[str] = mapped_column(Text)
    collections: Mapped[list[Any]] = mapped_column(default=list)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)  # summary/artifacts/gaps


class TaskRow(TimestampMixin, Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(48))
    inputs: Mapped[list[Any]] = mapped_column(default=list)
    outputs: Mapped[list[Any]] = mapped_column(default=list)
    acceptance: Mapped[list[Any]] = mapped_column(default=list)
    budget: Mapped[dict[str, Any]] = mapped_column(default=dict)
    model_hint: Mapped[str | None] = mapped_column(String(96))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    ladder_rung: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(Text)
    parent_task_id: Mapped[str | None] = mapped_column(String(48))
    depth: Mapped[int] = mapped_column(Integer, default=0)
    order_index: Mapped[int] = mapped_column(Integer, default=0)


class StepRow(Base):
    __tablename__ = "steps"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # plan-scoped task id (t1, t1.c2); TaskRow keys are run-scoped ("<run>:<tid>")
    task_id: Mapped[str] = mapped_column(String(48), index=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    n: Mapped[int] = mapped_column(Integer)
    thought: Mapped[str | None] = mapped_column(Text)
    action: Mapped[dict[str, Any]] = mapped_column(default=dict)
    observation: Mapped[str | None] = mapped_column(Text)
    observation_artifact_id: Mapped[str | None] = mapped_column(String(32))
    tokens: Mapped[dict[str, Any]] = mapped_column(default=dict)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    model_id: Mapped[str | None] = mapped_column(String(96))
    status: Mapped[str] = mapped_column(String(24), default="done")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolCallRow(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (UniqueConstraint("run_id", "idempotency_key", name="uq_toolcall_idem"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    step_id: Mapped[str | None] = mapped_column(String(32), index=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    task_id: Mapped[str | None] = mapped_column(String(48))
    tool: Mapped[str] = mapped_column(String(64))
    args: Mapped[dict[str, Any]] = mapped_column(default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(80))
    permission: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result_artifact_id: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    sandbox: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(String(32), index=True)
    task_id: Mapped[str | None] = mapped_column(String(48))
    kind: Mapped[str] = mapped_column(String(24))  # file|text|json|image|table|tool_output|render
    path: Mapped[str | None] = mapped_column(Text)
    blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    mime: Mapped[str | None] = mapped_column(String(96))
    sha256: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VerificationRow(Base):
    __tablename__ = "verifications"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(48), index=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    checks: Mapped[list[Any]] = mapped_column(default=list)
    reviewer: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    verdict: Mapped[str] = mapped_column(String(16))
    escalation_applied: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LedgerEntryRow(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(48), index=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    n: Mapped[int] = mapped_column(Integer)
    facts_established: Mapped[list[Any]] = mapped_column(default=list)
    done: Mapped[list[Any]] = mapped_column(default=list)
    remaining: Mapped[list[Any]] = mapped_column(default=list)
    blockers: Mapped[list[Any]] = mapped_column(default=list)
    stuck: Mapped[bool] = mapped_column(Boolean, default=False)
    next_action_hint: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------ observability


class SpanRow(Base):
    __tablename__ = "spans"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # OTel span id (hex)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(96), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # llm.call, tool.call, ...
    start_ns: Mapped[int] = mapped_column(Integer)
    end_ns: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    attrs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    run_id: Mapped[str | None] = mapped_column(String(32), index=True)
    task_id: Mapped[str | None] = mapped_column(String(48))
    step_id: Mapped[str | None] = mapped_column(String(32))


class AuditRow(Base):
    __tablename__ = "audit"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(40))  # ISO-8601 UTC; hashed, so stored as written
    actor: Mapped[str] = mapped_column(String(128))
    event: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class SealEventRow(Base):
    __tablename__ = "seal_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    process: Mapped[str] = mapped_column(String(96))
    dest: Mapped[str] = mapped_column(String(256))
    port: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24))  # blocked_connect|blocked_dns|counter
    stack: Mapped[list[Any]] = mapped_column(default=list)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)


# ------------------------------------------------------------------ memory & skills


class MemoryRow(TimestampMixin, Base):
    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user: Mapped[str] = mapped_column(String(128), default="local")
    scope: Mapped[str] = mapped_column(String(16), default="user")  # user|team
    kind: Mapped[str] = mapped_column(String(24))  # fact|preference|episode|skill_note
    text: Mapped[str] = mapped_column(Text)
    provenance: Mapped[list[Any]] = mapped_column(default=list)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    embedding_ref: Mapped[str | None] = mapped_column(String(64))
    approved: Mapped[bool] = mapped_column(Boolean, default=True)


class SkillRow(TimestampMixin, Base):
    __tablename__ = "skills"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(96), unique=True)
    description: Mapped[str] = mapped_column(Text)
    triggers: Mapped[list[Any]] = mapped_column(default=list)
    tools: Mapped[list[Any]] = mapped_column(default=list)
    body: Mapped[str] = mapped_column(Text)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|proposed


# ------------------------------------------------------------------ knowledge plane


class CollectionRow(TimestampMixin, Base):
    __tablename__ = "collections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(96), unique=True)
    source_roots: Mapped[list[Any]] = mapped_column(default=list)
    acl: Mapped[list[Any]] = mapped_column(default=list)
    embedding_model: Mapped[str | None] = mapped_column(String(96))
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="active")


class DocumentRow(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("collection_id", "path", name="uq_document_path"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    collection_id: Mapped[str] = mapped_column(ForeignKey("collections.id"), index=True)
    path: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    doc_type: Mapped[str | None] = mapped_column(String(48), index=True)
    language: Mapped[str | None] = mapped_column(String(16))
    sha256: Mapped[str | None] = mapped_column(String(64))
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer, default=0)
    mtime: Mapped[float | None] = mapped_column(Float)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[str | None] = mapped_column(String(32))
    doc_number: Mapped[str | None] = mapped_column(String(128), index=True)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="discovered", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)


class DocumentVersionRow(Base):
    __tablename__ = "document_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    group_key: Mapped[str] = mapped_column(String(160), index=True)  # normalised title/number
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    revision: Mapped[str | None] = mapped_column(String(32))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PageRow(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("document_id", "page_no", name="uq_page"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), default="text")  # text|table|drawing|photo|form
    has_text_layer: Mapped[bool] = mapped_column(Boolean, default=True)
    text_quality: Mapped[float] = mapped_column(Float, default=1.0)
    image_artifact_id: Mapped[str | None] = mapped_column(String(32))
    thumb_artifact_id: Mapped[str | None] = mapped_column(String(32))
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ChunkRow(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    collection_id: Mapped[str] = mapped_column(String(32), index=True)
    page_start: Mapped[int] = mapped_column(Integer, default=1)
    page_end: Mapped[int] = mapped_column(Integer, default=1)
    section_path: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    context_prefix: Mapped[str | None] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="prose")  # prose|table|code|figure
    parent_chunk_id: Mapped[str | None] = mapped_column(String(32), index=True)
    tombstoned: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IngestJobRow(TimestampMixin, Base):
    __tablename__ = "ingest_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    collection_id: Mapped[str] = mapped_column(String(32), index=True)
    document_id: Mapped[str | None] = mapped_column(String(32), index=True)
    stage: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class IngestErrorRow(Base):
    __tablename__ = "ingest_errors"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    collection_id: Mapped[str] = mapped_column(String(32), index=True)
    document_id: Mapped[str | None] = mapped_column(String(32), index=True)
    stage: Mapped[str] = mapped_column(String(24))
    error: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------ models & routing


class ModelRow(TimestampMixin, Base):
    __tablename__ = "models"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    family: Mapped[str | None] = mapped_column(String(48))
    path: Mapped[str] = mapped_column(Text)
    engine: Mapped[str] = mapped_column(String(24))
    params_b: Mapped[float] = mapped_column(Float, default=0.0)
    license: Mapped[str | None] = mapped_column(String(48))
    capabilities: Mapped[list[Any]] = mapped_column(default=list)
    context_len: Mapped[int] = mapped_column(Integer, default=0)
    serve_context_len: Mapped[int] = mapped_column(Integer, default=0)
    vram_gb: Mapped[float] = mapped_column(Float, default=0.0)
    quant: Mapped[str | None] = mapped_column(String(24))
    roles: Mapped[list[Any]] = mapped_column(default=list)
    serve_args: Mapped[list[Any]] = mapped_column(default=list)
    probes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="registered")


class ProbeResultRow(Base):
    __tablename__ = "probe_results"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    model_id: Mapped[str] = mapped_column(String(96), index=True)
    probe: Mapped[str] = mapped_column(String(48))
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RouterDecisionRow(Base):
    __tablename__ = "router_decisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(String(32), index=True)
    task_id: Mapped[str | None] = mapped_column(String(48))
    role: Mapped[str] = mapped_column(String(24))
    chosen: Mapped[str] = mapped_column(String(96))
    rejected: Mapped[list[Any]] = mapped_column(default=list)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PermissionLogRow(Base):
    __tablename__ = "permissions_log"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(String(32), index=True)
    task_id: Mapped[str | None] = mapped_column(String(48))
    tool: Mapped[str] = mapped_column(String(64))
    args_summary: Mapped[str | None] = mapped_column(Text)
    rule: Mapped[dict[str, Any]] = mapped_column(default=dict)
    decision: Mapped[str] = mapped_column(String(16))
    decided_by: Mapped[str] = mapped_column(String(16))  # user|policy
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GatewayCacheRow(Base):
    __tablename__ = "gateway_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(96))
    response: Mapped[dict[str, Any]] = mapped_column(default=dict)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmbeddingCacheRow(Base):
    __tablename__ = "embedding_cache"
    key: Mapped[str] = mapped_column(String(160), primary_key=True)  # model:sha256(text)
    model: Mapped[str] = mapped_column(String(96))
    dim: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)  # packed float32
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------ evals


class EvalRunRow(Base):
    __tablename__ = "eval_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    suite: Mapped[str] = mapped_column(String(64), index=True)
    profile: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)


class EvalResultRow(Base):
    __tablename__ = "eval_results"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    eval_run_id: Mapped[str] = mapped_column(ForeignKey("eval_runs.id"), index=True)
    case_id: Mapped[str] = mapped_column(String(96))
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[float | None] = mapped_column(Float)
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_spans_trace_kind", SpanRow.trace_id, SpanRow.kind)
Index("ix_chunks_doc_page", ChunkRow.document_id, ChunkRow.page_start)
Index("ix_ingest_jobs_claim", IngestJobRow.status, IngestJobRow.priority, IngestJobRow.created_at)
