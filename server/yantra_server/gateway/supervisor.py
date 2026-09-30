"""Engine supervisor (SPEC §7.1): launch, health, restart, on-demand swap, sealed env."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal

from yantra_server.config import YantraConfig
from yantra_server.gateway.engines.base import Engine
from yantra_server.gateway.engines.llamacpp import LlamaCppEngine
from yantra_server.gateway.engines.mock import MockEngine
from yantra_server.gateway.engines.pooling import PoolingWorker
from yantra_server.gateway.engines.vllm import VLLMEngine
from yantra_server.gateway.profile_spec import EngineSpec, ProfileSpec
from yantra_server.gateway.registry import ModelRegistry
from yantra_server.observe.tracing import span
from yantra_server.seal.env import sealed_environment

log = logging.getLogger(__name__)

BASE_PORT = 8100
MAX_RESTARTS = 3
HEALTH_INTERVAL_S = 10.0
IDLE_STOP_S = 300.0  # on_demand engines stop after this idle time

Status = Literal["stopped", "starting", "healthy", "unhealthy", "failed", "unavailable"]


@dataclass
class EngineProcess:
    spec: EngineSpec
    replica: int
    port: int | None = None
    status: Status = "stopped"
    proc: subprocess.Popen[bytes] | None = None
    engine: Engine | None = None
    restarts: int = 0
    last_error: str | None = None
    last_used: float = field(default_factory=time.monotonic)
    log_file: Path | None = None
    api_key: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)


class Supervisor:
    """Owns every engine process; the gateway asks it for a client by model id."""

    def __init__(
        self,
        config: YantraConfig,
        registry: ModelRegistry,
        profile: ProfileSpec,
        *,
        spawn: Callable[..., subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.profile = profile
        self._spawn = spawn or subprocess.Popen
        self.processes: list[EngineProcess] = []
        self._rr = itertools.count()
        self._health_task: asyncio.Task[None] | None = None
        port = itertools.count(BASE_PORT)
        for spec in [*profile.engines, *self._integrated_specs({e.id for e in profile.engines})]:
            for replica in range(max(1, spec.replicas)):
                needs_port = spec.kind in ("vllm", "llamacpp")
                self.processes.append(
                    EngineProcess(
                        spec=spec, replica=replica, port=next(port) if needs_port else None
                    )
                )

    # -------------------------------------------------- integrated engines (machine state)

    def llama_binary(self) -> str | None:
        configured = os.environ.get("YANTRA_LLAMA_SERVER")
        if configured:
            return configured if Path(configured).is_file() else None
        local = self.config.paths.assets_dir / ".yantra" / "runtime" / "llama"
        for name in ("llama-server.exe", "llama-server"):
            for candidate in (local / name, local / "build" / "bin" / name):
                if candidate.is_file():
                    return str(candidate)
        return shutil.which("llama-server")

    def _integrated_file(self) -> Path:
        return self.config.paths.data_dir / "integrated_engines.yaml"

    def _integrated_specs(self, profile_ids: set[str]) -> list[EngineSpec]:
        """Engines added via one-click integration persist per machine, across restarts."""
        import yaml

        path = self._integrated_file()
        if not path.is_file():
            return []
        try:
            entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        except (OSError, yaml.YAMLError):
            return []
        specs = []
        for entry in entries:
            try:
                spec = EngineSpec.model_validate(entry)
            except Exception:
                continue
            if spec.id not in profile_ids:
                specs.append(spec)
        return specs

    def persist_integrated(self, spec: EngineSpec) -> None:
        import yaml

        path = self._integrated_file()
        entries: list[dict[str, Any]] = []
        if path.is_file():
            with contextlib.suppress(OSError, yaml.YAMLError):
                entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        entries = [e for e in entries if e.get("id") != spec.id]
        entries.append(
            spec.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            | {"id": spec.id, "kind": spec.kind}
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")

    # ------------------------------------------------------------- commands

    def command_for(self, ep: EngineProcess) -> list[str] | None:
        """Launch command for a process (None for in-process engines). Pure and unit-tested."""
        spec = ep.spec
        if spec.kind in ("pooling", "mock"):
            return None
        manifest = self.registry.get(spec.model or "")
        if manifest is None:
            return None
        model_path = str(manifest.resolved_path(self.config.paths.models_dir))
        if spec.kind == "vllm":
            cmd = [
                "vllm",
                "serve",
                model_path,
                "--host",
                "127.0.0.1",
                "--port",
                str(ep.port),
                "--served-model-name",
                manifest.id,
                "--structured-outputs-config",
                json.dumps({"backend": "xgrammar"}),
            ]
            if spec.gpu_memory_utilization is not None:
                cmd += ["--gpu-memory-utilization", str(spec.gpu_memory_utilization)]
            if spec.max_model_len or manifest.serve_context_len:
                cmd += ["--max-model-len", str(spec.max_model_len or manifest.serve_context_len)]
            if spec.enable_prefix_caching:
                cmd += ["--enable-prefix-caching"]
            if spec.kv_cache_dtype:
                cmd += ["--kv-cache-dtype", spec.kv_cache_dtype]
            if spec.tensor_parallel > 1:
                cmd += ["--tensor-parallel-size", str(spec.tensor_parallel)]
            cmd += [str(a) for a in manifest.serve_args]
            return cmd
        if spec.kind == "llamacpp":
            cmd = [
                self.llama_binary() or "llama-server",
                "-m",
                model_path,
                "--host",
                "127.0.0.1",
                "--port",
                str(ep.port),
                "--alias",
                manifest.id,
                "-c",
                str(spec.ctx or manifest.serve_context_len or 8192),
            ]
            if spec.threads:
                cmd += ["-t", str(spec.threads)]
            cmd += ["-ngl", str(spec.gpu_layers), "--parallel", "1", "--jinja"]
            if spec.mmproj:
                cmd += ["--mmproj", str(Path(spec.mmproj).resolve())]
            if spec.mode == "embedding" or "--embedding" in manifest.serve_args:
                cmd += ["--embedding"]
            if spec.mode == "reranking" or "--reranking" in manifest.serve_args:
                cmd += ["--reranking"]
            return cmd
        return None

    def environment_for(self, ep: EngineProcess) -> dict[str, str]:
        env = sealed_environment(allowlist=self.config.seal.allowlist)
        if ep.spec.kind == "llamacpp" and not ep.spec.url:
            # Keep the per-process key out of commands, manifests and audit logs.
            env["LLAMA_API_KEY"] = ep.api_key
            env["LLAMA_ARG_CORS_ORIGINS"] = "http://127.0.0.1"
            env["LLAMA_ARG_UI"] = "0"
        device = ep.spec.device
        if device.startswith("cuda"):
            ids = device.removeprefix("cuda:") or "0"
            env["CUDA_VISIBLE_DEVICES"] = ids
        elif device == "cpu":
            env["CUDA_VISIBLE_DEVICES"] = ""
        return env

    def binary_available(self, ep: EngineProcess) -> bool:
        if ep.spec.kind == "vllm":
            return shutil.which("vllm") is not None
        if ep.spec.kind == "llamacpp":
            return self.llama_binary() is not None
        return True

    # ------------------------------------------------------------- lifecycle

    async def start_all(self) -> None:
        for ep in self.processes:
            if not ep.spec.on_demand:
                await self.start(ep)
        if self._health_task is None:
            self._health_task = asyncio.create_task(self._health_loop(), name="engine-health")

    async def add_engine(self, spec: EngineSpec) -> EngineProcess:
        """Integrate an engine at runtime (one-click model add): allocate a port, start it.

        Replaces a same-id engine that is not running (re-integration after a failure)."""
        for existing in list(self.processes):
            if existing.spec.id == spec.id and existing.status in (
                "stopped",
                "failed",
                "unavailable",
            ):
                self.processes.remove(existing)
        used = [ep.port for ep in self.processes if ep.port is not None]
        needs_port = spec.kind in ("vllm", "llamacpp") and not spec.url
        ep = EngineProcess(
            spec=spec,
            replica=0,
            port=(max(used, default=BASE_PORT - 1) + 1) if needs_port else None,
        )
        self.processes.append(ep)
        await self.start(ep)
        return ep

    async def start(self, ep: EngineProcess) -> None:
        if ep.status in ("starting", "healthy"):
            return
        spec = ep.spec
        limit = self.config.gateway.max_resident_models
        if limit > 0 and spec.kind != "mock":
            resident = sorted(
                (other for other in self.processes if other is not ep and other.proc is not None),
                key=lambda other: other.last_used,
            )
            incoming = self.registry.get(spec.model or "")

            def parameters(process: EngineProcess) -> float:
                model = self.registry.get(process.spec.model or "")
                return model.params_b if model else 0

            while resident and (
                len(resident) >= limit
                or sum(parameters(other) for other in resident)
                + (incoming.params_b if incoming else 0)
                >= 120
            ):
                await self.stop(resident.pop(0))
        with span(
            "engine.start", kind="engine.log", engine_id=spec.id, engine_kind=spec.kind
        ) as sp:
            if spec.kind == "mock":
                ep.engine = MockEngine()
                ep.status = "healthy"
                sp.set("status", "healthy (in-process mock)")
                return
            if spec.kind == "pooling":
                model_paths = {
                    m: (manifest.path if (manifest := self.registry.get(m)) else m)
                    for m in spec.served_models()
                }
                device = "cuda" if spec.device.startswith("cuda") else "cpu"
                ep.engine = PoolingWorker(self.config.paths.models_dir, model_paths, device=device)
                health = await ep.engine.health()
                ep.status = "healthy" if health.ok else "unavailable"
                ep.last_error = health.detail
                sp.set("status", ep.status)
                sp.set("detail", health.detail or "")
                return
            if spec.url:
                if self.config.profile in {"laptop", "portable"}:
                    from yantra_server.security import local_endpoint

                    local_endpoint(spec.url)
                # Attach mode (docker-compose): the engine runs in a sibling container;
                # we only create the client and let the health loop promote it.
                ep.engine = self._client_for(ep)
                ep.status = "starting"
                sp.set("status", "starting (attached)")
                sp.set("url", spec.url)
                return
            if not self.binary_available(ep):
                ep.status = "unavailable"
                ep.last_error = f"{spec.kind} binary not on PATH"
                sp.set("status", "unavailable")
                sp.set("detail", ep.last_error)
                return
            cmd = self.command_for(ep)
            if cmd is None:
                ep.status = "unavailable"
                ep.last_error = f"model {spec.model!r} not in registry"
                sp.set("status", "unavailable")
                sp.set("detail", ep.last_error)
                return
            manifest = self.registry.get(spec.model or "")
            if manifest and not manifest.resolved_path(self.config.paths.models_dir).exists():
                ep.status = "unavailable"
                ep.last_error = (
                    f"weights missing: {manifest.resolved_path(self.config.paths.models_dir)} "
                    "(run scripts/fetch_models.py on a connected machine)"
                )
                sp.set("status", "unavailable")
                sp.set("detail", ep.last_error)
                return
            logs_dir = self.config.paths.data_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            ep.log_file = logs_dir / f"engine-{spec.id}-{ep.replica}.log"
            log_fh: IO[bytes] = ep.log_file.open("ab")
            try:
                ep.proc = self._spawn(
                    cmd,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    env=self.environment_for(ep),
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except OSError as exc:
                ep.status = "failed"
                ep.last_error = str(exc)
                sp.set("status", "failed")
                sp.set("detail", str(exc))
                return
            finally:
                log_fh.close()
            ep.status = "starting"
            ep.engine = self._client_for(ep)
            sp.set("status", "starting")
            sp.set("cmd", " ".join(cmd))
            sp.set("port", ep.port)

    def _client_for(self, ep: EngineProcess) -> Engine:
        base_url = ep.spec.url or f"http://127.0.0.1:{ep.port}"
        timeout = self.config.gateway.request_timeout_s
        if ep.spec.kind == "vllm":
            return VLLMEngine(base_url, timeout_s=timeout)
        return LlamaCppEngine(
            base_url, timeout_s=timeout, api_key=ep.api_key if not ep.spec.url else None
        )

    async def stop(self, ep: EngineProcess) -> None:
        if ep.proc is not None:
            with contextlib.suppress(OSError):
                ep.proc.terminate()
            try:
                await asyncio.to_thread(ep.proc.wait, 15)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    ep.proc.kill()
            ep.proc = None
        ep.status = "stopped"
        if ep.engine is not None and hasattr(ep.engine, "aclose"):
            await ep.engine.aclose()
            ep.engine = None

    async def stop_all(self) -> None:
        if self._health_task is not None:
            self._health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._health_task
            self._health_task = None
        for ep in self.processes:
            await self.stop(ep)

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(HEALTH_INTERVAL_S)
            with contextlib.suppress(Exception):
                await self.check_health_once()

    async def check_health_once(self) -> None:
        for ep in self.processes:
            if ep.status in ("unavailable",):
                continue
            if ep.proc is not None and ep.proc.poll() is not None:
                ep.last_error = f"process exited with {ep.proc.returncode}"
                await self._maybe_restart(ep)
                continue
            if ep.engine is None:
                continue
            if (
                ep.spec.on_demand
                and ep.status == "healthy"
                and ep.proc is not None
                and time.monotonic() - ep.last_used > IDLE_STOP_S
            ):
                log.info("stopping idle on-demand engine %s", ep.spec.id)
                await self.stop(ep)
                continue
            if ep.status in ("starting", "healthy", "unhealthy"):
                health = await ep.engine.health()
                if health.ok:
                    if ep.status != "healthy":
                        with span("engine.healthy", kind="engine.log", engine_id=ep.spec.id):
                            pass
                    ep.status = "healthy"
                    ep.restarts = 0
                elif ep.status == "healthy":
                    ep.status = "unhealthy"
                    ep.last_error = health.detail

    async def _maybe_restart(self, ep: EngineProcess) -> None:
        with span(
            "engine.exit",
            kind="engine.log",
            engine_id=ep.spec.id,
            detail=ep.last_error or "",
            restarts=ep.restarts,
        ):
            pass
        ep.proc = None
        if ep.restarts >= MAX_RESTARTS:
            ep.status = "failed"
            return
        ep.restarts += 1
        ep.status = "stopped"
        await asyncio.sleep(min(2**ep.restarts, 15))
        await self.start(ep)

    # ------------------------------------------------------------- lookup

    def _serves(self, ep: EngineProcess, model_id: str) -> bool:
        if ep.spec.kind == "mock":
            return True
        return model_id in ep.spec.served_models()

    def model_available(self, model_id: str) -> bool:
        return any(
            self._serves(ep, model_id)
            and (
                ep.status == "healthy"
                or (
                    ep.spec.on_demand
                    and ep.status not in {"failed", "unavailable"}
                    and self.binary_available(ep)
                    and (manifest := self.registry.get(model_id)) is not None
                    and manifest.resolved_path(self.config.paths.models_dir).exists()
                )
            )
            for ep in self.processes
        )

    async def engine_for_model(self, model_id: str) -> Engine:
        candidates = [ep for ep in self.processes if self._serves(ep, model_id)]
        # The mock claims every model id so GPU-less dev keeps working, but it must never
        # shadow a real engine: prefer engines that actually serve this model.
        real = [ep for ep in candidates if ep.spec.kind != "mock"]
        if any(ep.status == "healthy" and ep.engine is not None for ep in real):
            candidates = real
        healthy = [ep for ep in candidates if ep.status == "healthy" and ep.engine is not None]
        if not healthy:
            on_demand = [ep for ep in candidates if ep.spec.on_demand]
            for ep in on_demand:
                await self.start(ep)
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline and ep.engine is not None:
                    if ep.proc is not None and ep.proc.poll() is not None:
                        ep.status = "failed"
                        ep.last_error = (
                            f"Model process exited with {ep.proc.returncode}; see {ep.log_file}"
                        )
                        break
                    if (await ep.engine.health()).ok:
                        ep.status = "healthy"
                        break
                    await asyncio.sleep(2)
            healthy = [ep for ep in candidates if ep.status == "healthy" and ep.engine is not None]
        if not healthy:
            detail = (
                "; ".join(f"{ep.spec.id}:{ep.status} {ep.last_error or ''}" for ep in candidates)
                or "no engine"
            )
            raise EngineUnavailable(model_id, detail)
        chosen = healthy[next(self._rr) % len(healthy)]
        chosen.last_used = time.monotonic()
        assert chosen.engine is not None
        return chosen.engine

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "engine_id": ep.spec.id,
                "kind": ep.spec.kind,
                "replica": ep.replica,
                "models": ep.spec.served_models() or (["*"] if ep.spec.kind == "mock" else []),
                "port": ep.port,
                "status": ep.status,
                "restarts": ep.restarts,
                "error": ep.last_error,
                "pid": ep.proc.pid if ep.proc else None,
                "log_file": str(ep.log_file) if ep.log_file else None,
            }
            for ep in self.processes
        ]


class EngineUnavailable(Exception):
    def __init__(self, model_id: str, detail: str) -> None:
        super().__init__(f"no healthy engine for model {model_id!r} ({detail})")
        self.model_id = model_id


def detect_gpus() -> list[dict[str, Any]]:
    """GPU inventory via nvidia-smi (NVML without the extra dependency)."""
    smi = shutil.which("nvidia-smi")
    if smi is None:
        return []
    try:
        out = subprocess.run(
            [smi, "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "NO_PROXY": "*"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    gpus: list[dict[str, Any]] = []
    if out.returncode == 0:
        for line in out.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3 and parts[0].isdigit():
                gpus.append({"index": int(parts[0]), "name": parts[1], "vram_mb": int(parts[2])})
    return gpus
