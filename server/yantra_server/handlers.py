"""Foundation RPC handlers: ping/config/sessions/trace/seal status.

Conductor, models, RAG, memory and skills handlers are registered by their own
modules as milestones land.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from yantra_server import __version__
from yantra_server.db.models import RunRow, SealEventRow, SessionRow, SpanRow
from yantra_server.protocol import messages as msg
from yantra_server.protocol.jsonrpc import INVALID_PARAMS, RpcError
from yantra_server.rpc import Connection, rpc_method
from yantra_server.state import AppState


@rpc_method("ping", msg.PingParams)
async def ping(state: AppState, conn: Connection, params: msg.PingParams) -> msg.PingResult:
    return msg.PingResult(pong=True, version=state.version)


@rpc_method("config.get", msg.ConfigGetParams)
async def config_get(
    state: AppState, conn: Connection, params: msg.ConfigGetParams
) -> dict[str, Any]:
    dumped = state.config.model_dump(mode="json")
    dumped["server"]["admin_token"] = "***" if state.config.server.admin_token else None
    return {
        "config": dumped,
        "profile": state.config.profile,
        "sealed": state.config.sealed(),
        "version": __version__,
        "config_file": str(state.loaded.config_file) if state.loaded.config_file else None,
    }


@rpc_method("session.create", msg.SessionCreateParams)
async def session_create(
    state: AppState, conn: Connection, params: msg.SessionCreateParams
) -> msg.SessionCreateResult:
    from anyio import to_thread

    from yantra_server.security import workspace_path

    try:
        params.workspace = str(workspace_path(params.workspace, state.config.paths.workspace_roots))
    except ValueError as exc:
        raise RpcError(INVALID_PARAMS, str(exc)) from exc

    def _create() -> str:
        with state.db.session() as s:
            row = SessionRow(
                workspace_path=params.workspace,
                collections=list(params.collections),
                mode=params.mode,
                title=params.title,
            )
            s.add(row)
            s.flush()
            return row.id

    session_id = await to_thread.run_sync(_create)
    state.audit.append(
        "user", "session.create", {"session_id": session_id, "workspace": params.workspace}
    )
    conn.session_ids.add(session_id)
    return msg.SessionCreateResult(session_id=session_id)


@rpc_method("session.list", msg.SessionListParams)
async def session_list(
    state: AppState, conn: Connection, params: msg.SessionListParams
) -> msg.SessionListResult:
    from anyio import to_thread

    def _list() -> list[msg.SessionInfo]:
        with state.db.session() as s:
            rows = (
                s.execute(
                    select(SessionRow).order_by(SessionRow.updated_at.desc()).limit(params.limit)
                )
                .scalars()
                .all()
            )
            infos: list[msg.SessionInfo] = []
            for row in rows:
                last_goal = s.execute(
                    select(RunRow.goal_text)
                    .where(RunRow.session_id == row.id)
                    .order_by(RunRow.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                infos.append(
                    msg.SessionInfo(
                        session_id=row.id,
                        title=row.title,
                        workspace=row.workspace_path,
                        mode=row.mode,
                        status=row.status,
                        created_at=row.created_at.isoformat(),
                        updated_at=row.updated_at.isoformat(),
                        last_goal=last_goal,
                    )
                )
            return infos

    return msg.SessionListResult(sessions=await to_thread.run_sync(_list))


@rpc_method("session.resume", msg.SessionResumeParams)
async def session_resume(
    state: AppState, conn: Connection, params: msg.SessionResumeParams
) -> dict[str, Any]:
    from anyio import to_thread

    def _get() -> dict[str, Any] | None:
        with state.db.session() as s:
            row = s.get(SessionRow, params.session_id)
            if row is None:
                return None
            runs = (
                s.execute(
                    select(RunRow)
                    .where(RunRow.session_id == row.id)
                    .order_by(RunRow.created_at.asc())
                )
                .scalars()
                .all()
            )
            return {
                "session": {
                    "session_id": row.id,
                    "workspace": row.workspace_path,
                    "collections": row.collections,
                    "mode": row.mode,
                    "title": row.title,
                },
                "runs": [
                    {
                        "run_id": r.id,
                        "goal": r.goal_text,
                        "status": r.status,
                        "final": r.final,
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in runs
                ],
            }

    data = await to_thread.run_sync(_get)
    if data is None:
        raise RpcError(INVALID_PARAMS, f"unknown session {params.session_id}")
    conn.session_ids.add(params.session_id)
    replayed: list[dict[str, Any]] = []
    for run in data["runs"]:
        replayed.extend(state.bus.replay(run["run_id"], params.last_seq))
    for frame in replayed:
        conn.try_send(frame)
    data["replayed"] = len(replayed)
    return data


@rpc_method("trace.get", msg.TraceGetParams)
async def trace_get(
    state: AppState, conn: Connection, params: msg.TraceGetParams
) -> dict[str, Any]:
    from anyio import to_thread

    def _get() -> dict[str, Any]:
        with state.db.session() as s:
            spans = (
                s.execute(
                    select(SpanRow)
                    .where(SpanRow.run_id == params.run_id)
                    .order_by(SpanRow.start_ns.asc())
                )
                .scalars()
                .all()
            )
            return {
                "run_id": params.run_id,
                "spans": [
                    {
                        "span_id": sp.id,
                        "parent_id": sp.parent_id,
                        "trace_id": sp.trace_id,
                        "name": sp.name,
                        "kind": sp.kind,
                        "start_ns": sp.start_ns,
                        "end_ns": sp.end_ns,
                        "status": sp.status,
                        "attrs": sp.attrs,
                        "task_id": sp.task_id,
                        "step_id": sp.step_id,
                    }
                    for sp in spans
                ],
            }

    return await to_thread.run_sync(_get)


@rpc_method("run.approve", msg.RunApproveParams)
async def run_approve(
    state: AppState, conn: Connection, params: msg.RunApproveParams
) -> dict[str, Any]:
    resolved = state.tools.broker.resolve(
        params.request_id, params.decision, params.note, run_id=params.run_id
    )
    if not resolved and state.conductor is not None:
        if params.request_id.startswith("plan:"):
            if not params.request_id.startswith(f"plan:{params.run_id}:"):
                return {"resolved": False}
            resolved = state.conductor.resolve_plan_approval(
                params.run_id, params.decision, request_id=params.request_id
            )
        else:
            resolved = state.conductor.resolve_question(
                params.request_id, params.answers or [], run_id=params.run_id
            )
    return {"resolved": resolved}


@rpc_method("session.prompt", msg.SessionPromptParams)
async def session_prompt(
    state: AppState, conn: Connection, params: msg.SessionPromptParams
) -> msg.SessionPromptResult:
    if state.conductor is None:
        raise RpcError(INVALID_PARAMS, "conductor not available")
    if params.mode is not None:
        from anyio import to_thread

        new_mode: str = params.mode

        def _set_mode() -> None:
            with state.db.session() as s:
                row = s.get(SessionRow, params.session_id)
                if row is not None:
                    row.mode = new_mode

        await to_thread.run_sync(_set_mode)
    run_id = await state.conductor.start_run(
        params.session_id,
        params.text,
        params.attachments,
        params.mode,
        budget_overrides=params.budget_overrides,
    )
    return msg.SessionPromptResult(run_id=run_id)


@rpc_method("run.cancel", msg.RunCancelParams)
async def run_cancel(
    state: AppState, conn: Connection, params: msg.RunCancelParams
) -> dict[str, Any]:
    cancelled = state.conductor.cancel(params.run_id) if state.conductor else False
    return {"cancelled": cancelled}


@rpc_method("run.plan.update", msg.RunPlanUpdateParams)
async def run_plan_update(
    state: AppState, conn: Connection, params: msg.RunPlanUpdateParams
) -> dict[str, Any]:
    if state.conductor is None:
        raise RpcError(INVALID_PARAMS, "conductor not available")
    ok = await state.conductor.update_plan(params.run_id, params.plan)
    return {"updated": ok}


@rpc_method("session.fork", msg.SessionForkParams)
async def session_fork(
    state: AppState, conn: Connection, params: msg.SessionForkParams
) -> msg.SessionCreateResult:
    from anyio import to_thread

    def _fork() -> str | None:
        with state.db.session() as s:
            row = s.get(SessionRow, params.session_id)
            if row is None:
                return None
            fork = SessionRow(
                workspace_path=row.workspace_path,
                collections=list(row.collections),
                mode=row.mode,
                title=f"{row.title or 'session'} (fork)",
                parent_session_id=row.id,
            )
            s.add(fork)
            s.flush()
            return fork.id

    forked = await to_thread.run_sync(_fork)
    if forked is None:
        raise RpcError(INVALID_PARAMS, f"unknown session {params.session_id}")
    return msg.SessionCreateResult(session_id=forked)


@rpc_method("memory.list", msg.MemoryListParams)
async def memory_list(
    state: AppState, conn: Connection, params: msg.MemoryListParams
) -> dict[str, Any]:
    if state.memory is None:
        return {"memories": []}
    return {"memories": state.memory.list_memories(params.kind)}


@rpc_method("memory.review", msg.MemoryReviewParams)
async def memory_review(
    state: AppState, conn: Connection, params: msg.MemoryReviewParams
) -> dict[str, Any]:
    ok = state.memory.review_memory(params.memory_id, params.action) if state.memory else False
    if ok:
        state.audit.append(
            "user", "memory.review", {"id": params.memory_id, "action": params.action}
        )
    return {"ok": ok}


@rpc_method("skills.list", msg.SkillsListParams)
async def skills_list(
    state: AppState, conn: Connection, params: msg.SkillsListParams
) -> dict[str, Any]:
    if state.memory is None:
        return {"skills": []}
    return {"skills": state.memory.list_skills()}


@rpc_method("skills.approve", msg.SkillsApproveParams)
async def skills_approve(
    state: AppState, conn: Connection, params: msg.SkillsApproveParams
) -> dict[str, Any]:
    ok = state.memory.approve_skill(params.skill_id, params.action) if state.memory else False
    if ok:
        state.audit.append(
            "user", "skills.approve", {"id": params.skill_id, "action": params.action}
        )
    return {"ok": ok}


@rpc_method("rag.search", msg.RagSearchParams)
async def rag_search(
    state: AppState, conn: Connection, params: msg.RagSearchParams
) -> dict[str, Any]:
    if state.knowledge is None:
        return {"hits": []}
    hits = await state.knowledge.search(
        params.query, collections=params.collections or None, k=params.k, mode=params.mode
    )
    return {
        "hits": [
            {
                "chunk_id": h.chunk_id,
                "title": h.title,
                "page": h.page,
                "score": h.score,
                "snippet": h.snippet,
                "doc_type": h.doc_type,
            }
            for h in hits
        ]
    }


@rpc_method("models.list", msg.ModelsListParams)
async def models_list(
    state: AppState, conn: Connection, params: msg.ModelsListParams
) -> msg.ModelsListResult:
    state.registry.load()
    models: list[msg.ModelInfo] = []
    for m in state.registry.all():
        probes_passed, probes_total = m.probe_counts()
        models.append(
            msg.ModelInfo(
                id=m.id,
                family=m.family,
                engine=m.engine,
                params_b=m.params_b,
                capabilities=list(m.capabilities),
                roles=list(m.roles),
                status="registered",
                healthy=state.supervisor.model_available(m.id),
                vram_gb=m.vram_gb,
                quant=m.quant,
                local=state.registry.is_local(m.id),
                path=m.path or None,
                probes_passed=probes_passed,
                probes_total=probes_total,
            )
        )
    return msg.ModelsListResult(models=models)


@rpc_method("seal.status", msg.SealStatusParams)
async def seal_status(
    state: AppState, conn: Connection, params: msg.SealStatusParams
) -> dict[str, Any]:
    from anyio import to_thread

    if state.seal_monitor is not None:
        return await to_thread.run_sync(state.seal_monitor.status)

    def _count() -> tuple[int, list[dict[str, Any]]]:
        with state.db.session() as s:
            total = int(s.execute(select(func.count()).select_from(SealEventRow)).scalar_one())
            last = (
                s.execute(select(SealEventRow).order_by(SealEventRow.ts.desc()).limit(10))
                .scalars()
                .all()
            )
            return total, [
                {"ts": e.ts.isoformat(), "process": e.process, "dest": e.dest, "kind": e.kind}
                for e in last
            ]

    total, last = await to_thread.run_sync(_count)
    return {
        "sealed": state.config.sealed(),
        "allowlist": state.config.seal.allowlist,
        "blocked_attempts_total": total,
        "last_attempts": last,
        "layers": {},
    }


@rpc_method("seal.verify", msg.SealStatusParams)
async def seal_verify_rpc(
    state: AppState, conn: Connection, params: msg.SealStatusParams
) -> dict[str, Any]:
    from yantra_server.seal.verify import run_verification

    report = await run_verification(state)
    return {
        "ok": report.ok(),
        "outcomes": [
            {"name": o.name, "passed": o.passed, "skipped": o.skipped, "detail": o.detail}
            for o in report.outcomes
        ],
        "certificate": report.certificate,
        "signature": report.signature,
    }


@rpc_method("seal.demo", msg.SealStatusParams)
async def seal_demo_rpc(
    state: AppState, conn: Connection, params: msg.SealStatusParams
) -> dict[str, Any]:
    from yantra_server.seal.demo import run_seal_demo

    return await run_seal_demo(state)
