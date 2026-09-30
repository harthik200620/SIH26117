"""Sandbox contract + backend selection.

Backends: bwrap (Linux default), docker (--network none fallback), local (dev-only,
UNSEALED, no isolation — ADR 0004). Every run records exit/timing/output, the files it
changed under the workspace. Isolation policy is distinct from measured network traffic;
an absent blocked-attempt count does not establish zero packets or zero attempts.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from yantra_server.config import SandboxConfig


class SandboxError(Exception):
    pass


class SandboxLimits(BaseModel):
    timeout_s: float = 120.0
    cpu_s: float = 120.0
    max_rss_mb: int = 4096
    max_pids: int = 256
    max_output_bytes: int = 10 * 1024 * 1024


class SandboxResult(BaseModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    wall_s: float = 0.0
    cpu_s: float | None = None
    peak_rss_mb: float | None = None
    timed_out: bool = False
    files_changed: list[str] = Field(default_factory=list)
    blocked_net_attempts: int = 0
    backend: str = ""
    truncated: bool = False

    def combined_output(self) -> str:
        if self.stderr and self.stdout:
            return f"{self.stdout}\n[stderr]\n{self.stderr}"
        return self.stdout or self.stderr


OutputCallback = Callable[[str], None]


class Sandbox(ABC):
    name = "sandbox"
    provides_isolation = True

    def __init__(self, workspace: Path, limits: SandboxLimits) -> None:
        self.workspace = workspace
        self.limits = limits

    @classmethod
    @abstractmethod
    def available(cls) -> bool: ...

    @abstractmethod
    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
        on_output: OutputCallback | None = None,
    ) -> SandboxResult: ...

    # ------------------------------------------------------------- shared helpers

    def snapshot_mtimes(self) -> dict[str, float]:
        snapshot: dict[str, float] = {}
        for root, dirs, files in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules"}]
            for name in files:
                path = Path(root) / name
                try:
                    snapshot[str(path.relative_to(self.workspace))] = path.stat().st_mtime
                except OSError:
                    continue
        return snapshot

    def diff_files(self, before: dict[str, float]) -> list[str]:
        after = self.snapshot_mtimes()
        changed = [p for p, m in after.items() if before.get(p) != m]
        changed += [p for p in before if p not in after]  # deletions
        return sorted(changed)[:200]


async def drain_process(
    proc: asyncio.subprocess.Process,
    *,
    limits: SandboxLimits,
    timeout_s: float,
    on_output: OutputCallback | None,
) -> tuple[str, str, bool, bool]:
    """Read stdout/stderr concurrently with a wall-clock timeout and an output cap."""
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    truncated = False
    total = 0

    async def reader(stream: asyncio.StreamReader | None, parts: list[bytes], is_out: bool) -> None:
        nonlocal total, truncated
        if stream is None:
            return
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            total += len(chunk)
            if total <= limits.max_output_bytes:
                parts.append(chunk)
                if on_output is not None and is_out:
                    on_output(chunk.decode("utf-8", errors="replace"))
            else:
                truncated = True

    timed_out = False
    readers = asyncio.gather(
        reader(proc.stdout, stdout_parts, True), reader(proc.stderr, stderr_parts, False)
    )

    async def completed() -> None:
        await asyncio.shield(readers)
        await proc.wait()

    try:
        await asyncio.wait_for(completed(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        try:
            await asyncio.wait_for(readers, timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            readers.cancel()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        readers.cancel()
        await asyncio.gather(readers, return_exceptions=True)
        raise
    return (
        b"".join(stdout_parts).decode("utf-8", errors="replace"),
        b"".join(stderr_parts).decode("utf-8", errors="replace"),
        timed_out,
        truncated,
    )


def select_sandbox(config: SandboxConfig, workspace: Path, *, sealed: bool) -> Sandbox:
    """Pick the backend per config/availability; refuse no-isolation backends when sealed."""
    from .bwrap import BwrapSandbox
    from .docker import DockerSandbox
    from .local import LocalSandbox

    limits = SandboxLimits(
        timeout_s=config.max_seconds,
        cpu_s=config.max_seconds,
        max_rss_mb=config.max_rss_mb,
        max_pids=config.max_pids,
    )
    backend = config.backend
    if backend == "disabled":
        from .disabled import DisabledSandbox

        return DisabledSandbox(workspace, limits)
    if backend == "auto":
        if BwrapSandbox.available():
            backend = "bwrap"
        elif DockerSandbox.available():
            backend = "docker"
        else:
            backend = "local"
    if backend == "bwrap":
        if not BwrapSandbox.available():
            raise SandboxError("bwrap backend requested but bwrap is not installed")
        return BwrapSandbox(workspace, limits)
    if backend == "docker":
        if not DockerSandbox.available():
            raise SandboxError("docker backend requested but docker is not available")
        return DockerSandbox(workspace, limits)
    if sealed:
        raise SandboxError(
            "no isolating sandbox available (bwrap/docker missing) and YANTRA_SEALED=1: "
            "refusing the 'local' backend in sealed mode. Install bubblewrap or Docker, "
            "or set YANTRA_SEALED=0 for development."
        )
    return LocalSandbox(workspace, limits)


def now() -> float:
    return time.monotonic()
