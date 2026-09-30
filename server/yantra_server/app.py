"""FastAPI application: HTTP API, WebSocket JSON-RPC, static dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from yantra_server import __version__, handlers
from yantra_server.artifacts import ArtifactStore
from yantra_server.config import LoadedConfig, load_config
from yantra_server.db.base import Database
from yantra_server.db.migrate import upgrade_to_head
from yantra_server.gateway.profile_spec import ProfileSpec
from yantra_server.gateway.registry import ModelManifest, ModelRegistry
from yantra_server.gateway.router import Router, RoutingPolicy
from yantra_server.gateway.service import Gateway
from yantra_server.gateway.supervisor import Supervisor
from yantra_server.observe.audit_chain import AuditChain
from yantra_server.observe.tracing import configure_tracing, shutdown_tracing
from yantra_server.protocol.jsonrpc import (
    INTERNAL_ERROR,
    RpcError,
    error_frame,
    parse_request,
    result_frame,
)
from yantra_server.rpc import Connection, EventBus, dispatch
from yantra_server.state import AppState

log = logging.getLogger(__name__)


def build_state(loaded: LoadedConfig) -> AppState:
    """Wire the object graph: DB, artifacts, audit, tracing, and the inference plane."""
    config = loaded.config
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    from yantra_server.workspaces import WorkspaceFolders

    workspace_folders = WorkspaceFolders(config.paths.data_dir, config.paths.workspace_roots)
    db = Database(config.db_url(), echo=config.db.echo)
    upgrade_to_head(config.db_url())
    artifacts = ArtifactStore(db, config.paths.data_dir / "artifacts")
    configure_tracing(db, artifacts)
    audit = AuditChain(db)
    bus = EventBus(db)

    registry_file = config.gateway.registry_file
    if not registry_file.is_absolute():
        registry_file = loaded.assets_dir / registry_file
    registry = ModelRegistry(
        registry_file,
        config.paths.models_dir,
        local_file=config.paths.data_dir / "registry.local.yaml",
    )
    registry.sync_to_db(db)
    profile_spec = ProfileSpec.load(loaded.assets_dir, config.profile)
    supervisor = Supervisor(config, registry, profile_spec)
    routing_file = config.gateway.routing_file
    if not routing_file.is_absolute():
        routing_file = loaded.assets_dir / routing_file
    allow_mock = config.profile == "mock"
    routing_policy = RoutingPolicy.load(routing_file)
    routing_policy.apply_local_overlay(config.paths.data_dir / "routing.generated.yaml")
    routing_policy.apply_local_overlay(config.paths.data_dir / "routing.local.yaml")
    router = Router(
        routing_policy,
        registry,
        supervisor.model_available,
        allow_mock=allow_mock,
    )
    gateway = Gateway(
        config,
        db,
        artifacts,
        router,
        supervisor,
        max_concurrent=1
        if config.gateway.max_resident_models > 0
        else int(profile_spec.concurrency.get("interactive_sessions", 4)) * 2,
    )

    from yantra_server.state import ToolsBundle
    from yantra_server.tools.builtin import default_registry
    from yantra_server.tools.mcp import MCPManager
    from yantra_server.tools.permissions import PermissionBroker, PermissionPolicy
    from yantra_server.tools.runtime import ToolRuntime

    tool_registry = default_registry(loaded.assets_dir)
    policy = PermissionPolicy(config.permissions)
    broker = PermissionBroker(policy, bus, db, audit)
    tools = ToolsBundle(
        registry=tool_registry,
        policy=policy,
        broker=broker,
        runtime=ToolRuntime(tool_registry, broker, db, audit),
        mcp=MCPManager(loaded.assets_dir / "tools" / "mcp"),
    )
    state = AppState(
        loaded=loaded,
        db=db,
        artifacts=artifacts,
        audit=audit,
        bus=bus,
        version=__version__,
        registry=registry,
        profile_spec=profile_spec,
        supervisor=supervisor,
        router=router,
        gateway=gateway,
        tools=tools,
    )
    state.extras["workspace_folders"] = workspace_folders

    from yantra_server.agents import AgentRoster
    from yantra_server.conductor.service import Conductor

    roster = AgentRoster(loaded.assets_dir / "agents")
    state.conductor = Conductor(state=state, roster=roster)
    state.extras["roster"] = roster
    gateway.run_models = state.extras.setdefault("run_models", {})

    from yantra_server.observe.seal_monitor import SealMonitor

    state.seal_monitor = SealMonitor(config, db, bus, loaded.assets_dir)

    try:
        from yantra_server.knowledge.service import KnowledgeService

        state.knowledge = KnowledgeService(config, db, gateway)
    except ImportError:
        state.knowledge = None  # knowledge extras not installed

    from yantra_server.memory.service import MemoryService

    state.memory = MemoryService(db, gateway, loaded.assets_dir / "skills")
    return state


def _auto_roles(manifest: Any) -> list[str]:
    """Assign router roles from a model's capabilities (one-click integration).

    Specialists get their one role; chat models get the general roles, plus vision
    duties when they can see. Probes run after serving starts and refine ranking."""
    caps = set(manifest.capabilities)
    if "embed" in caps:
        return ["embed"]
    if "rerank" in caps:
        return ["rerank"]
    if "ocr" in caps:
        return ["ocr"]
    roles = ["planner", "executor", "reviewer", "router", "utility"]
    if "vision" in caps:
        roles += ["vision"]
    return roles


def create_app(loaded: LoadedConfig | None = None) -> FastAPI:
    from yantra_server.instance import InstanceLock

    loaded = loaded or load_config()
    loaded.config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    ownership = InstanceLock(loaded.config.paths.data_dir)
    ownership.acquire()
    try:
        state = build_state(loaded)
    finally:
        ownership.release()

    @asynccontextmanager
    async def running(app: FastAPI) -> Any:
        if state.config.server.require_namespace:
            from yantra_server.seal.namespace import check_with_child

            evidence = check_with_child()
            if not evidence["verified"]:
                raise RuntimeError("Required OS network namespace isolation could not be verified")
            state.extras["network_isolation"] = evidence
            state.audit.append("system", "isolation.verified", evidence)
        state.bus.bind_loop(asyncio.get_running_loop())
        state.conductor.reconcile_interrupted()
        # Layer 3: install the socket guard in the server process, reporting to the monitor.
        from yantra_server.seal.socket_guard import install as install_guard

        install_guard(
            allowlist=state.config.seal.allowlist,
            reporter=state.seal_monitor.record_event if state.seal_monitor else None,
        )
        if state.seal_monitor is not None:
            await state.seal_monitor.start()
        state.audit.append(
            "system", "server.start", {"version": __version__, "profile": state.config.profile}
        )
        await state.supervisor.start_all()
        for mcp_tool in await state.tools.mcp.start_all():
            state.tools.registry.register(mcp_tool)
        wired = getattr(app.state, "wire_extra", None)
        if wired:
            await wired(state)
        yield
        drivers = [active.driver for active in state.conductor._active.values()]
        for driver in drivers:
            driver.cancel()
        await asyncio.gather(*drivers, return_exceptions=True)
        if state.seal_monitor is not None:
            await state.seal_monitor.stop()
        await state.tools.mcp.stop_all()
        await state.supervisor.stop_all()
        if state.knowledge is not None:
            state.knowledge.close()
        state.audit.append("system", "server.stop", {})
        shutdown_tracing()
        state.db.dispose()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        ownership.acquire()
        try:
            async with running(app):
                yield
        finally:
            ownership.release()

    app = FastAPI(title="BlackBox Workspace", version=__version__, lifespan=lifespan)
    app.state.yantra = state
    from yantra_server.security import LocalBoundaryMiddleware
    from yantra_server.workbench import workbench_router

    app.add_middleware(LocalBoundaryMiddleware, token=state.config.server.admin_token)
    app.include_router(workbench_router(state))

    @app.post("/auth/login")
    async def login(body: dict[str, str]) -> JSONResponse:
        import hmac

        from yantra_server.security import issue_session

        expected = state.config.server.admin_token
        supplied = body.get("token", "")
        if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
            return JSONResponse({"error": "Invalid access key"}, status_code=403)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "yantra_session",
            issue_session(expected),
            httponly=True,
            samesite="strict",
            max_age=8 * 3600,
            path="/",
        )
        return response

    @app.post("/auth/logout")
    async def logout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie("yantra_session", path="/")
        return response

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "profile": state.config.profile,
            "sealed": state.config.sealed(),
            "network_isolation": state.extras.get("network_isolation", {"verified": False}),
            "db": "postgres" if state.db.url.startswith("postgres") else "sqlite",
        }

    @app.get("/api/seal")
    async def seal() -> Any:
        if state.seal_monitor is not None:
            report = state.seal_monitor.status()
            report["network_isolation"] = state.extras.get("network_isolation", {"verified": False})
            return report
        from yantra_server.protocol import messages as msg

        return await handlers.seal_status(state, Connection(id="http"), msg.SealStatusParams())

    @app.get("/api/metrics")
    async def metrics() -> Any:
        from fastapi import Response

        from yantra_server.observe.metrics import render_metrics

        return Response(content=render_metrics(state), media_type="text/plain; version=0.0.4")

    @app.post("/api/seal/verify")
    async def api_seal_verify() -> Any:
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

    @app.post("/api/seal/demo")
    async def api_seal_demo() -> Any:
        from yantra_server.seal.demo import run_seal_demo

        return await run_seal_demo(state)

    @app.get("/api/runs")
    async def api_runs(limit: int = 50) -> Any:
        from sqlalchemy import select

        from yantra_server.db.models import RunRow

        with state.db.session() as s:
            rows = (
                s.execute(select(RunRow).order_by(RunRow.created_at.desc()).limit(limit))
                .scalars()
                .all()
            )
            return {
                "runs": [
                    {
                        "run_id": r.id,
                        "goal": r.goal_text[:160],
                        "status": r.status,
                        "mode": r.mode,
                        "created_at": r.created_at.isoformat(),
                        "budget_used": r.budget_used,
                    }
                    for r in rows
                ]
            }

    @app.get("/api/runs/{run_id}/trace")
    async def api_run_trace(run_id: str) -> Any:
        from yantra_server.protocol import messages as msg

        return await handlers.trace_get(
            state, Connection(id="http"), msg.TraceGetParams(run_id=run_id)
        )

    @app.get("/api/audit")
    async def api_audit() -> Any:
        result = state.audit.verify()
        head = state.audit.head()
        return {
            "ok": result.ok,
            "entries": result.entries,
            "head": head.hash if head else None,
            "detail": result.detail,
            "recent": list(state.audit.export(max(1, state.audit.count() - 40)))[-40:],
        }

    @app.get("/api/evals")
    async def api_evals(limit: int = 30) -> Any:
        from sqlalchemy import select

        from yantra_server.db.models import EvalResultRow, EvalRunRow

        with state.db.session() as s:
            runs = (
                s.execute(select(EvalRunRow).order_by(EvalRunRow.started_at.desc()).limit(limit))
                .scalars()
                .all()
            )
            payload = []
            for run in runs:
                results = (
                    s.execute(select(EvalResultRow).where(EvalResultRow.eval_run_id == run.id))
                    .scalars()
                    .all()
                )
                payload.append(
                    {
                        "id": run.id,
                        "suite": run.suite,
                        "profile": run.profile,
                        "status": run.status,
                        "started_at": run.started_at.isoformat(),
                        "pass_rate": (run.meta or {}).get("pass_rate"),
                        "cases": [
                            {
                                "case_id": r.case_id,
                                "passed": r.passed,
                                "score": r.score,
                                "metrics": r.metrics,
                                "detail": (r.detail or {}).get("detail", ""),
                            }
                            for r in results
                        ],
                    }
                )
        return {"eval_runs": payload}

    @app.get("/api/logs")
    async def api_logs() -> dict[str, Any]:
        logs_dir = state.config.paths.data_dir / "logs"
        files = []
        if logs_dir.is_dir():
            for p in sorted(logs_dir.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True):
                files.append({"name": p.name, "size_kb": round(p.stat().st_size / 1024, 1)})
        return {"files": files}

    @app.get("/api/logs/{name}")
    async def api_log_tail(name: str, tail: int = 200) -> Any:
        logs_dir = (state.config.paths.data_dir / "logs").resolve()
        target = (logs_dir / name).resolve()
        if target.parent != logs_dir or not target.is_file():
            return JSONResponse({"error": "no such log"}, status_code=404)
        text = await asyncio.to_thread(target.read_text, encoding="utf-8", errors="replace")
        lines = text.splitlines()[-max(1, min(tail, 2000)) :]
        return {"name": name, "lines": lines}

    @app.get("/api/config")
    async def api_config() -> dict[str, Any]:
        from yantra_server.config import effective_report

        return {
            "profile": state.config.profile,
            "sealed": state.config.sealed(),
            "config_file": str(loaded.config_file) if loaded.config_file else None,
            "assets_dir": str(loaded.assets_dir),
            "data_dir": str(state.config.paths.data_dir),
            "models_dir": str(state.config.paths.models_dir),
            "entries": [
                {"key": k, "value": "***" if "token" in k else v, "source": s}
                for k, v, s in effective_report(loaded)
            ],
        }

    @app.get("/api/knowledge")
    async def api_knowledge() -> Any:
        if state.knowledge is not None:
            return state.knowledge.dashboard_status()
        return {"collections": [], "note": "knowledge plane lands in M6"}

    @app.get("/api/models")
    async def api_models() -> dict[str, Any]:
        state.registry.load()  # hot-reload: operators may have edited registry.yaml

        def model_entry(m: ModelManifest) -> dict[str, Any]:
            passed, total = m.probe_counts()
            return {
                **m.model_dump(mode="json"),
                "available": state.supervisor.model_available(m.id),
                "local": state.registry.is_local(m.id),
                "path": m.path or None,
                "probes_passed": passed,
                "probes_total": total,
            }

        return {
            "models": [model_entry(m) for m in state.registry.all()],
            "engines": state.supervisor.status(),
        }

    @app.post("/api/models/probe")
    async def api_models_probe(body: dict[str, Any]) -> dict[str, Any]:
        from yantra_server.gateway.probe import run_probes

        model_id = str(body.get("model_id", ""))
        manifest = state.registry.get(model_id)
        if manifest is None:
            return {"error": f"unknown model {model_id}"}
        outcomes = await run_probes(state.gateway, model_id, manifest.capabilities)
        probes: dict[str, Any] = {o.probe: ("pass" if o.passed else "fail") for o in outcomes}
        for o in outcomes:
            if o.score is not None:
                probes[f"{o.probe}_score"] = o.score
        state.registry.set_probes(model_id, probes)
        state.registry.save()
        state.audit.append("system", "models.probe", {"model": model_id, "probes": probes})
        from yantra_server.db.models import ProbeResultRow

        with state.db.session() as s:
            for o in outcomes:
                s.add(
                    ProbeResultRow(
                        model_id=model_id,
                        probe=o.probe,
                        result=o.model_dump(mode="json"),
                        passed=o.passed,
                    )
                )
        return {"model": model_id, "outcomes": [o.model_dump(mode="json") for o in outcomes]}

    @app.get("/api/models/discover")
    async def api_models_discover() -> dict[str, Any]:
        """Downloaded-but-unregistered models: weight drops, app stores, HF/Ollama caches."""
        from yantra_server.gateway.discovery import discover_local_models

        def scan() -> list[dict[str, Any]]:
            registered_paths: set[str] = set()
            for m in state.registry.all():
                for p in (Path(m.path), m.resolved_path(state.config.paths.models_dir)):
                    try:
                        registered_paths.add(str(p.resolve()))
                    except OSError:
                        continue
            return discover_local_models(
                state.config.paths.models_dir, loaded.assets_dir, registered_paths
            )

        candidates = await asyncio.to_thread(scan)
        return {
            "candidates": candidates,
            "llamacpp_available": state.supervisor.llama_binary() is not None,
        }

    @app.get("/api/routing/assignments")
    async def api_routing_assignments() -> dict[str, Any]:
        """Which model each role resolves to right now (the logic live routing uses)."""
        state.registry.load()  # hot-reload, same as /api/models
        return {
            "assignments": [a.model_dump(mode="json") for a in state.router.current_assignments()]
        }

    @app.post("/api/models/integrate")
    async def api_models_integrate(body: dict[str, Any]) -> Any:
        """One click: inspect -> register -> route -> serve. Fails loud, audited."""
        from yantra_server.gateway.profile_spec import EngineSpec
        from yantra_server.gateway.registry import (
            LargeModelRefused,
            RegistryError,
            inspect_model_path,
        )

        if state.conductor._active:
            return JSONResponse(
                {"error": "Finish the current workflow before changing models"}, status_code=409
            )
        raw_path = str(body.get("path", "")).strip()
        if not raw_path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        try:
            if raw_path.startswith(("\\\\", "//")):
                raise RegistryError("Network model paths are not permitted")
            manifest = await asyncio.to_thread(inspect_model_path, Path(raw_path))
        except RegistryError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if name := str(body.get("name", "")).strip():
            manifest.id = name
        manifest.roles = [str(r) for r in body.get("roles") or _auto_roles(manifest)]
        try:
            state.registry.register(manifest, allow_large=False, local=True)
        except LargeModelRefused as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        state.registry.save()

        # Route it first for its roles: in memory now, and in the machine-local overlay for
        # restarts. The shipped routing.yaml stays pristine.
        for role in manifest.roles:
            candidates = state.router.policy.roles.setdefault(role, [])
            if manifest.id in candidates:
                candidates.remove(manifest.id)
            candidates.insert(0, manifest.id)
        state.router.policy.persist_local_front(
            state.config.paths.data_dir / "routing.local.yaml", manifest.id, manifest.roles
        )

        engine_status: dict[str, Any] | None = None
        if manifest.engine in ("vllm", "llamacpp") and bool(body.get("serve", True)):
            spec = EngineSpec(
                id=manifest.id,
                kind=manifest.engine,
                model=manifest.id,
                device="cpu" if manifest.engine == "llamacpp" else "auto",
                ctx=min(manifest.serve_context_len or 8192, 8192),
                threads=min(8, __import__("os").cpu_count() or 4),
                on_demand=state.config.profile in {"laptop", "portable"},
            )
            ep = await state.supervisor.add_engine(spec)
            state.supervisor.persist_integrated(spec)  # survives server restarts
            engine_status = {
                "engine_id": ep.spec.id,
                "status": ep.status,
                "port": ep.port,
                "error": ep.last_error,
            }

            async def probe_when_healthy(model_id: str = manifest.id) -> None:
                """Probe the freshly served model so routing can rank it by evidence."""
                from yantra_server.gateway.probe import run_probes

                deadline = asyncio.get_running_loop().time() + 300
                while asyncio.get_running_loop().time() < deadline:
                    await state.supervisor.check_health_once()
                    if state.supervisor.model_available(model_id):
                        break
                    await asyncio.sleep(5)
                else:
                    return
                m = state.registry.get(model_id)
                if m is None:
                    return
                outcomes = await run_probes(state.gateway, model_id, m.capabilities)
                probes: dict[str, Any] = {
                    o.probe: ("pass" if o.passed else "fail") for o in outcomes
                }
                for o in outcomes:
                    if o.score is not None:
                        probes[f"{o.probe}_score"] = o.score
                state.registry.set_probes(model_id, probes)
                state.registry.save()
                state.audit.append(
                    "system", "models.autoprobe", {"model": model_id, "probes": probes}
                )

            if state.config.profile not in {"laptop", "portable"}:
                asyncio.get_running_loop().create_task(probe_when_healthy())
        state.audit.append(
            "user",
            "models.integrate",
            {
                "model": manifest.id,
                "path": raw_path,
                "roles": manifest.roles,
                "engine": engine_status,
            },
        )
        return {
            "model": manifest.model_dump(mode="json"),
            "engine": engine_status,
            "routed_roles": manifest.roles,
        }

    @app.post("/api/runs")
    async def api_start_run(body: dict[str, Any]) -> Any:
        """Start a goal from the console (auto mode, same engine the terminal uses)."""
        from yantra_server.db.models import SessionRow

        goal = str(body.get("goal", "")).strip()
        if not goal:
            return JSONResponse({"error": "goal is required"}, status_code=400)
        from yantra_server.security import workspace_path

        workspace = str(body.get("workspace", "")).strip()
        if not workspace and state.config.paths.workspace_roots:
            workspace = str(state.config.paths.workspace_roots[0])
        try:
            ws = workspace_path(workspace, state.config.paths.workspace_roots)
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        with state.db.session() as s:
            session = SessionRow(
                workspace_path=str(ws), collections=[], mode="auto", title="console"
            )
            s.add(session)
            s.flush()
            session_id = session.id
        run_id = await state.conductor.start_run(session_id, goal, [], "auto")
        return {"run_id": run_id, "workspace": str(ws)}

    @app.post("/api/engines/{engine_id}/{action}")
    async def api_engine_action(engine_id: str, action: str) -> dict[str, Any]:
        matches = [ep for ep in state.supervisor.processes if ep.spec.id == engine_id]
        if not matches:
            return {"error": f"unknown engine {engine_id}"}
        for ep in matches:
            if action == "start":
                await state.supervisor.start(ep)
            elif action == "stop":
                await state.supervisor.stop(ep)
            else:
                return {"error": f"unknown action {action}"}
        state.audit.append("user", f"engine.{action}", {"engine": engine_id})
        return {"engines": state.supervisor.status()}

    @app.post("/api/bench/llm")
    async def api_bench_llm(body: dict[str, Any] | None = None) -> dict[str, Any]:
        from yantra_server.gateway.bench import bench_llm

        role = str((body or {}).get("role", "executor"))
        return await bench_llm(state.gateway, role=role)

    @app.get("/api/artifacts/{artifact_id}")
    async def api_artifact(artifact_id: str) -> Any:
        from fastapi import Response

        from yantra_server.artifacts.store import ArtifactNotFound

        try:
            row = state.artifacts.get(artifact_id)
            data = state.artifacts.read_bytes(artifact_id)
        except ArtifactNotFound:
            return JSONResponse({"error": "not found"}, status_code=404)
        return Response(content=data, media_type=row.mime or "application/octet-stream")

    @app.websocket("/rpc")
    async def rpc_socket(ws: WebSocket) -> None:
        token = state.config.server.admin_token
        if token and state.config.server.host not in ("127.0.0.1", "::1", "localhost"):
            provided = ws.query_params.get("token") or ws.headers.get("x-yantra-token")
            if provided != token:
                await ws.close(code=4401)
                return
        await ws.accept()
        conn = Connection(id=uuid.uuid4().hex[:12])
        state.bus.attach(conn)

        async def pump_outbound() -> None:
            while True:
                frame = await conn.outbound.get()
                await ws.send_text(json.dumps(frame, default=str))

        pump = asyncio.create_task(pump_outbound())
        try:
            while True:
                raw = await ws.receive_text()
                request_id: Any = None
                try:
                    request_id, method, params = parse_request(raw)
                    result = await dispatch(state, conn, method, params)
                    if request_id is not None:
                        conn.try_send(result_frame(request_id, result))
                except RpcError as exc:
                    conn.try_send(error_frame(request_id, exc.code, exc.message, exc.data))
                except Exception as exc:
                    log.exception("rpc failure")
                    conn.try_send(error_frame(request_id, INTERNAL_ERROR, str(exc)))
        except WebSocketDisconnect:
            pass
        finally:
            pump.cancel()
            state.bus.detach(conn.id)

    # The dashboard ships inside the package (vite outDir = server/yantra_server/static),
    # so it is present in the wheel/Docker image; web/dist is the dev-server fallback.
    packaged = Path(__file__).parent / "static"
    web_dist = (
        packaged if (packaged / "index.html").is_file() else loaded.assets_dir / "web" / "dist"
    )
    if (web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="dashboard")
    else:

        @app.get("/")
        async def root() -> JSONResponse:
            return JSONResponse(
                {
                    "name": "yantra-server",
                    "version": __version__,
                    "dashboard": "not built (run `pnpm --filter yantra-web build`)",
                }
            )

    return app
