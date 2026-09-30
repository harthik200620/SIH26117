"""Private workbench API: readiness, evidence retrieval, runs and replayable SSE."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from yantra_server.db.models import (
    ArtifactRow,
    RouterDecisionRow,
    RunRow,
    SessionRow,
    ToolCallRow,
    VerificationRow,
)
from yantra_server.hardware import hardware_info
from yantra_server.rpc import Connection
from yantra_server.security import workspace_path
from yantra_server.state import AppState


def collection_for(workspace: Path) -> str:
    return (
        "ws_" + hashlib.sha256(os.path.normcase(str(workspace.resolve())).encode()).hexdigest()[:16]
    )


def _within(path: str, root: Path) -> bool:
    return bool(path and Path(path).resolve().is_relative_to(root))


class WorkspaceBody(BaseModel):
    workspace: str = Field(max_length=1024)


class SearchBody(WorkspaceBody):
    query: str = Field(min_length=1, max_length=4000)


class StartBody(WorkspaceBody):
    goal: str = Field(min_length=1, max_length=12000)
    model: str | None = None
    mode: Literal["auto", "ask", "plan"] = "auto"


def workbench_router(state: AppState) -> APIRouter:
    router = APIRouter(prefix="/api/workbench")
    cached_hardware: dict[str, Any] = {}
    hardware_at = 0.0
    index_lock = asyncio.Lock()

    def workspace(raw: str) -> Path:
        try:
            return workspace_path(raw, state.config.paths.workspace_roots)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/workspace/select")
    def select_workspace(body: WorkspaceBody) -> dict[str, Any]:
        # This is a user-facing administrative action, never an agent tool.
        if not state.config.server.admin_token:
            raise HTTPException(403, "Configure an access key before adding workspace folders")
        try:
            root = state.extras["workspace_folders"].select(body.workspace)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, f"Cannot use this folder: {exc}") from exc
        state.audit.append("user", "workspace.selected", {"workspace": str(root)})
        return {"workspace": str(root), "roots": [str(p) for p in state.config.paths.workspace_roots]}

    def get_run(run_id: str) -> dict[str, Any]:
        with state.db.session() as db:
            row = db.get(RunRow, run_id)
            if row is None:
                raise HTTPException(404, "Run not found")
            return {
                "run_id": row.id,
                "goal": row.goal_text,
                "status": row.status,
                "workspace": row.workspace_path,
                "final": row.final,
                "budget_used": row.budget_used,
                "created_at": row.created_at.isoformat(),
                "mode": row.mode,
                "model": row.budgets.get("model"),
                "plan": row.plan,
            }

    @router.get("/status")
    async def status() -> dict[str, Any]:
        from yantra_server.seal.env import assert_environment_locked
        from yantra_server.seal.socket_guard import is_installed

        nonlocal hardware_at
        if not cached_hardware or time.monotonic() - hardware_at > 30:
            cached_hardware.update(await asyncio.to_thread(hardware_info))
            hardware_at = time.monotonic()
        catalog = json.loads(
            (state.loaded.assets_dir / "models/catalog.json").read_text(encoding="utf-8")
        )
        total = cached_hardware.get("ram_gb") or 0
        for item in catalog:
            item["url"] = f"https://huggingface.co/{item['repo']}"
            item["fit"] = (
                "comfortable"
                if item["runtime_gb"] <= total * 0.40
                else "tight"
                if item["runtime_gb"] <= total * 0.60
                else "not_recommended"
            )
            item["estimate"] = True
            available = cached_hardware.get("available_ram_gb")
            if available is not None and item["runtime_gb"] > available:
                item["fit"] = "free_memory_low"
            item["command"] = (
                f"python scripts/setup_workbench.py --download {item['id']}"
                if item["file"]
                else None
            )
        real = [
            m
            for m in state.registry.all()
            if m.engine != "mock" and state.supervisor.model_available(m.id)
        ]
        roots = [str(p) for p in state.config.paths.workspace_roots]
        return {
            "hardware": cached_hardware,
            "catalog": catalog,
            "profile": state.config.profile,
            "workspace": roots[0] if roots else str(state.loaded.assets_dir / "workspace"),
            "roots": roots,
            "models": [
                {
                    "id": m.id,
                    "params_b": m.params_b,
                    "quant": m.quant,
                    "roles": m.roles,
                    "capabilities": m.capabilities,
                    "context": m.serve_context_len,
                }
                for m in real
            ],
            "ready": any("chat" in m.capabilities for m in real),
            "runtime_available": state.supervisor.llama_binary() is not None,
            "security": {
                "application_guard": is_installed(),
                "offline_environment": not assert_environment_locked(),
                "network_policy": state.config.seal.allowlist,
                "os_isolation_verified": bool(
                    state.extras.get("network_isolation", {}).get("verified")
                ),
                "isolation_evidence": state.extras.get("network_isolation", {"verified": False}),
                "code_execution": state.config.sandbox.backend,
                "blocked_attempts": state.seal_monitor.blocked_total() if state.seal_monitor else 0,
                "note": "Network namespace and native child probes passed; scope is this workload, under a trusted host/kernel/browser."
                if state.extras.get("network_isolation", {}).get("verified")
                else "Application controls are active only within their scope. OS-wide zero egress is not attested. Use strict Linux deployment or a disconnected machine.",
            },
            "retrieval": "Local BM25 (no embedding model needed)"
            if state.config.knowledge.lexical_only
            else "Hybrid local retrieval",
            "parameter_cap_b": 120,
        }

    @router.get("/files")
    async def files(workspace_path: str) -> dict[str, Any]:
        root = workspace(workspace_path)
        entries = []
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".") and d not in {"node_modules", "__pycache__"}
            )
            if len(Path(directory).relative_to(root).parts) >= 3:
                dirs[:] = []
            for name in sorted(names):
                path = Path(directory) / name
                if name.startswith(".") or not path.resolve().is_relative_to(root):
                    continue
                entries.append(
                    {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size}
                )
                if len(entries) >= 300:
                    return {"files": entries, "truncated": True}
        return {"files": entries, "truncated": False}

    @router.get("/runs/{run_id}/permissions")
    async def pending_permissions(run_id: str) -> dict[str, Any]:
        get_run(run_id)
        requests = state.tools.broker.pending_for_run(run_id)
        return {
            "requests": requests,
            "pending_ids": [r["request_id"] for r in requests]
            + (state.conductor.pending_request_ids(run_id) if state.conductor else []),
        }

    @router.get("/file-preview")
    async def file_preview(workspace_path: str, path: str) -> dict[str, Any]:
        root = workspace(workspace_path)
        if path.startswith(("\\\\", "//")):
            raise HTTPException(400, "Network paths are disabled")
        target = (root / path).resolve()
        if not target.is_relative_to(root) or any(
            part.startswith(".") for part in target.relative_to(root).parts
        ):
            raise HTTPException(404, "Workspace file not found")

        def read() -> dict[str, Any]:
            try:
                descriptor = os.open(
                    target,
                    os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise HTTPException(400, "Preview requires a regular file")
                    content = stream.read(100_001)
            except OSError as exc:
                raise HTTPException(404, "Workspace file unavailable") from exc
            if b"\0" in content:
                raise HTTPException(415, "Binary file preview is unavailable")
            try:
                text = codecs.getincrementaldecoder("utf-8")().decode(
                    content[:100_000], final=len(content) <= 100_000
                )
            except UnicodeDecodeError as exc:
                raise HTTPException(415, "Preview supports UTF-8 text files") from exc
            return {
                "path": target.relative_to(root).as_posix(),
                "text": text,
                "bytes": info.st_size,
                "truncated": len(content) > 100_000,
            }

        return await asyncio.to_thread(read)

    @router.post("/index")
    async def index(body: WorkspaceBody) -> dict[str, Any]:
        root = workspace(body.workspace)
        if state.knowledge is None:
            raise HTTPException(503, "Knowledge dependencies are missing")
        async with index_lock:
            started = time.monotonic()
            stats = await state.knowledge.ingest_path(root, collection_for(root))
            return {
                **asdict(stats),
                "seconds": round(time.monotonic() - started, 3),
                "collection": collection_for(root),
            }

    @router.post("/search")
    async def search(body: SearchBody) -> dict[str, Any]:
        root = workspace(body.workspace)
        if state.knowledge is None:
            raise HTTPException(503, "Knowledge dependencies are missing")
        hits = await state.knowledge.search(body.query, collections=[collection_for(root)], k=8)
        # A copied/old index cannot grant access outside the selected workspace.
        return {"hits": [asdict(h) for h in hits if _within(h.path, root)]}

    @router.post("/runs")
    async def start(body: StartBody) -> dict[str, Any]:
        root = workspace(body.workspace)
        if state.conductor._active:
            raise HTTPException(
                409, "A workflow is already running. Stop or finish it before starting another."
            )
        if body.model:
            model = state.registry.get(body.model)
            if (
                not model
                or model.engine == "mock"
                or "chat" not in model.capabilities
                or model.params_b >= 120
                or not state.supervisor.model_available(model.id)
            ):
                raise HTTPException(400, "Selected local model is unavailable")
        else:
            from yantra_server.gateway.router import NoRouteAvailable, RouteNeed

            try:
                state.router.route(RouteNeed(role="planner", needs_json=True))
            except NoRouteAvailable as exc:
                raise HTTPException(
                    503, "No local model is ready. Open Models and finish local setup."
                ) from exc
        with state.db.session() as db:
            session = SessionRow(
                workspace_path=str(root),
                mode=body.mode,
                title=body.goal[:70],
                collections=[collection_for(root)],
            )
            db.add(session)
            db.flush()
            session_id = session.id
        run_id = await state.conductor.start_run(session_id, body.goal, [], body.mode)
        if body.model:
            state.extras.setdefault("run_models", {})[run_id] = body.model
            with state.db.session() as db:
                row = db.get(RunRow, run_id)
                if row is not None:
                    row.budgets = {**row.budgets, "model": body.model}
        return {"run_id": run_id, "session_id": session_id}

    @router.get("/runs/{run_id}")
    async def run(run_id: str) -> dict[str, Any]:
        return get_run(run_id)

    @router.get("/runs/{run_id}/execution-evidence")
    async def execution_evidence(run_id: str) -> dict[str, Any]:
        from yantra_server.tools.base import ToolResult

        get_run(run_id)
        with state.db.session() as db:
            rows = (
                db.execute(
                    select(ToolCallRow)
                    .where(
                        ToolCallRow.run_id == run_id,
                        ToolCallRow.tool.in_(["python", "bash", "run_tests"]),
                    )
                    .order_by(ToolCallRow.started_at.desc(), ToolCallRow.id)
                    .limit(101)
                )
                .scalars()
                .all()
            )
        calls = []
        for row in rows[:100]:
            evidence: dict[str, Any] = {
                "call_id": row.id,
                "task_id": row.task_id,
                "tool": row.tool,
                "status": row.status,
                "result_available": False,
            }
            if row.result_artifact_id:
                try:
                    result = ToolResult.model_validate_json(
                        state.artifacts.read_text(row.result_artifact_id)
                    )
                    sandbox = result.data.get("sandbox")
                    if isinstance(sandbox, dict):
                        evidence.update(
                            result_available=True,
                            ok=result.ok,
                            exit_code=sandbox.get("exit_code"),
                            timed_out=sandbox.get("timed_out"),
                            backend=sandbox.get("backend"),
                        )
                except (ValueError, OSError, KeyError):
                    pass
            calls.append(evidence)
        return {"calls": calls, "truncated": len(rows) > 100}

    @router.get("/runs/{run_id}/validation-record")
    async def validation_record(run_id: str) -> Response:
        """Download persisted, run-scoped observations without exporting tool arguments."""
        from datetime import UTC, datetime

        row = get_run(run_id)
        with state.db.session() as db:
            checks = db.scalars(
                select(VerificationRow)
                .where(VerificationRow.run_id == run_id)
                .order_by(VerificationRow.created_at.desc(), VerificationRow.id)
                .limit(501)
            ).all()
            routes = db.scalars(
                select(RouterDecisionRow)
                .where(RouterDecisionRow.run_id == run_id)
                .order_by(RouterDecisionRow.created_at.desc(), RouterDecisionRow.id)
                .limit(501)
            ).all()
            revisions = db.scalars(
                select(ArtifactRow)
                .where(ArtifactRow.run_id == run_id, ArtifactRow.kind == "render_revision")
                .order_by(ArtifactRow.created_at.desc(), ArtifactRow.id)
                .limit(501)
            ).all()
        record = {
            "schema_version": 1,
            "exported_at": datetime.now(UTC).isoformat(),
            "run": {
                key: row[key]
                for key in ("run_id", "goal", "status", "created_at", "model", "budget_used")
            },
            "verifications": [
                {
                    "task_id": c.task_id,
                    "attempt": c.attempt,
                    "verdict": c.verdict,
                    "checks": c.checks,
                    "reviewer": c.reviewer,
                    "created_at": c.created_at.isoformat(),
                }
                for c in reversed(checks[:500])
            ],
            "model_routes": [
                {"task_id": r.task_id, "role": r.role, "model": r.chosen, "reason": r.reason}
                for r in reversed(routes[:500])
            ],
            "execution": await execution_evidence(run_id),
            "render_revisions": [
                {
                    "artifact_id": a.id,
                    "task_id": a.task_id,
                    "sha256": a.sha256,
                    "bytes": a.size,
                    "created_at": a.created_at.isoformat(),
                    "download": f"/api/workbench/runs/{run_id}/revisions/{a.id}",
                }
                for a in reversed(revisions[:500])
            ],
            "truncated": {
                "verifications": len(checks) > 500,
                "model_routes": len(routes) > 500,
                "render_revisions": len(revisions) > 500,
            },
            "limitations": [
                "Contains task and review content; handle as confidential.",
                "This is an application record, not independent certification or human approval.",
                "Model review can be wrong; static file checks do not recalculate spreadsheet formulas.",
                "Files, source hashes and network packet evidence are not included in this record.",
                "A running task's record is a partial snapshot; later activity is not included.",
            ],
        }
        filename = f"blackbox-validation-{hashlib.sha256(run_id.encode()).hexdigest()[:12]}.json"
        return Response(
            json.dumps(record, indent=2, ensure_ascii=False),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    @router.get("/runs/{run_id}/revisions/{artifact_id}")
    async def render_revision(run_id: str, artifact_id: str) -> Response:
        """Download the saved revision, never the replaceable workspace file."""
        from yantra_server.artifacts.store import ArtifactNotFound

        get_run(run_id)
        try:
            artifact = state.artifacts.get(artifact_id)
        except ArtifactNotFound as exc:
            raise HTTPException(404, "Revision not found") from exc
        if artifact.run_id != run_id or artifact.kind != "render_revision":
            raise HTTPException(404, "Revision not found")
        if artifact.size > 64 * 1024 * 1024:
            raise HTTPException(413, "Revision exceeds the 64 MiB download limit")
        try:
            content = state.artifacts.read_bytes(artifact_id)
        except OSError as exc:
            raise HTTPException(409, "Saved revision is unavailable") from exc
        if len(content) != artifact.size or hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise HTTPException(409, "Saved revision failed its integrity check")
        suffix = Path(str(artifact.meta.get("filename", ""))).suffix.lower()
        if suffix not in {".xlsx", ".docx", ".pptx", ".pdf", ".md", ".json"}:
            suffix = ".bin"
        return Response(
            content,
            media_type=artifact.mime or "application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="revision-{artifact.id}{suffix}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str) -> dict[str, Any]:
        get_run(run_id)
        return {"cancelled": state.conductor.cancel(run_id)}

    @router.get("/runs/{run_id}/recovery")
    async def recovery(run_id: str) -> dict[str, Any]:
        row = get_run(run_id)
        uncertain = state.conductor.uncertain_operations(run_id)
        return {
            "status": row["status"],
            "can_resume": row["status"] == "interrupted" and not uncertain,
            "uncertain_operations": uncertain,
            "budget_used": row["budget_used"],
        }

    @router.post("/runs/{run_id}/resume")
    async def resume(run_id: str) -> dict[str, Any]:
        row = get_run(run_id)
        if row["status"] != "interrupted":
            raise HTTPException(409, "Only an interrupted workflow can be resumed")
        try:
            await state.conductor.resume_run(run_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id, "resumed": True}

    @router.get("/runs/{run_id}/download")
    async def download(run_id: str, path: str) -> FileResponse:
        if path.startswith(("\\\\", "//")):
            raise HTTPException(400, "Network paths are disabled")
        row = get_run(run_id)
        root = workspace(row["workspace"])
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "Workspace file not found")
        return FileResponse(target, filename=target.name, media_type="application/octet-stream")

    @router.get("/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
        get_run(run_id)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "Invalid event cursor") from None
        conn = Connection(id="sse-" + uuid.uuid4().hex)
        state.bus.attach(conn)

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            try:
                latest = state.bus.latest_seq(run_id)
                if latest == 0 or cursor > latest:
                    cursor = 0
                    row = get_run(run_id)
                    yield "id: 0\n: stream reset\n\n"
                    if row["plan"]:
                        yield (
                            "data: "
                            + json.dumps(
                                {"method": "plan.updated", "params": {"plan": row["plan"]}}
                            )
                            + "\n\n"
                        )
                while batch := state.bus.replay(run_id, cursor):
                    for frame in batch:
                        seq = int(frame["params"].get("seq", 0))
                        cursor = seq
                        yield f"id: {seq}\ndata: {json.dumps(frame)}\n\n"
                        if frame["method"] == "run.finished":
                            return
                row = get_run(run_id)
                if row["status"] in {
                    "done",
                    "done_with_gaps",
                    "cancelled",
                    "failed",
                    "planned",
                    "interrupted",
                }:
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "method": "run.finished",
                                "params": {
                                    "run_id": run_id,
                                    "status": row["status"],
                                    "summary": (
                                        "Work was interrupted. Review recovery before continuing."
                                        if row["status"] == "interrupted"
                                        else "No saved summary is available."
                                    ),
                                    "budget_used": row["budget_used"],
                                    **(row["final"] or {}),
                                },
                            }
                        )
                        + "\n\n"
                    )
                    return
                while not await request.is_disconnected():
                    try:
                        frame = await asyncio.wait_for(conn.outbound.get(), timeout=10)
                    except TimeoutError:
                        # Slow consumers can overflow the live queue. Refill from durable history.
                        missed = state.bus.replay(run_id, cursor)
                        if not missed:
                            yield ": heartbeat\n\n"
                            continue
                        frame = missed[0]
                    params = frame.get("params", {})
                    if params.get("run_id") != run_id or int(params.get("seq", 0)) <= cursor:
                        continue
                    for saved in state.bus.replay(run_id, cursor):
                        cursor = int(saved["params"]["seq"])
                        yield f"id: {cursor}\ndata: {json.dumps(saved)}\n\n"
                        if saved["method"] == "run.finished":
                            return
            finally:
                state.bus.detach(conn.id)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
