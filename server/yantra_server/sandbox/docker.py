"""Docker backend: `--network none --read-only --cap-drop ALL` (hosts without bwrap)."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid
from pathlib import Path

from .base import OutputCallback, Sandbox, SandboxLimits, SandboxResult, drain_process, now

DEFAULT_IMAGE = os.environ.get("YANTRA_SANDBOX_IMAGE", "yantra-sandbox:latest")


def container_argv(argv: list[str], workspace: Path) -> list[str]:
    """Translate host executable/script paths to the container's /work mount."""
    result = []
    for index, arg in enumerate(argv):
        if index == 0 and arg == sys.executable:
            result.append("python")
        elif index == 0 and Path(arg).name.lower() in {
            "powershell",
            "powershell.exe",
            "pwsh",
            "cmd.exe",
        }:
            # Shell commands are interpreted by Linux in this backend.
            return ["sh", "-lc", argv[-1]]
        elif (
            not arg.startswith("-")
            and Path(arg).is_absolute()
            and Path(arg).resolve().is_relative_to(workspace.resolve())
        ):
            result.append(
                "/work/" + Path(arg).resolve().relative_to(workspace.resolve()).as_posix()
            )
        else:
            result.append(arg)
    return result


def docker_command(
    argv: list[str],
    *,
    workspace: Path,
    cwd: Path,
    limits: SandboxLimits,
    image: str = DEFAULT_IMAGE,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Pure construction of the docker run argument list."""
    rel_cwd = "/work"
    try:
        rel = cwd.resolve().relative_to(workspace.resolve())
        if str(rel) != ".":
            rel_cwd = f"/work/{rel.as_posix()}"
    except ValueError:
        pass
    uid = getattr(os, "getuid", lambda: 10001)()
    gid = getattr(os, "getgid", lambda: 10001)()
    cmd = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{uid}:{gid}" if uid != 0 else "10001:10001",
        "--pids-limit",
        str(limits.max_pids),
        "--memory",
        f"{limits.max_rss_mb}m",
        "--cpus",
        "2",
        "--tmpfs",
        "/tmp:size=512m",
        "-v",
        f"{workspace.resolve()}:/work",
        "-w",
        rel_cwd,
        "-e",
        "YANTRA_SEALED=1",
        "-e",
        "HOME=/work",
    ]
    for key, value in (env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += [image, *container_argv(argv, workspace)]
    return cmd


class DockerSandbox(Sandbox):
    name = "docker"
    provides_isolation = True

    @classmethod
    def available(cls) -> bool:
        return shutil.which("docker") is not None

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
        on_output: OutputCallback | None = None,
    ) -> SandboxResult:
        before = self.snapshot_mtimes()
        cmd = docker_command(
            argv, workspace=self.workspace, cwd=cwd or self.workspace, limits=self.limits, env=env
        )
        name = "yantra-" + uuid.uuid4().hex
        cmd[2:2] = ["--name", name]
        started = now()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        )
        if stdin is not None and proc.stdin is not None:
            proc.stdin.write(stdin.encode())
            proc.stdin.close()
        try:
            stdout, stderr, timed_out, truncated = await drain_process(
                proc,
                limits=self.limits,
                timeout_s=timeout_s or self.limits.timeout_s,
                on_output=on_output,
            )
        finally:
            # Killing a Docker CLI does not terminate its container.
            cleanup = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(cleanup.wait(), timeout=10)
            except TimeoutError:
                cleanup.kill()
                await cleanup.wait()
        return SandboxResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            wall_s=round(now() - started, 3),
            timed_out=timed_out,
            files_changed=self.diff_files(before),
            blocked_net_attempts=0,  # --network none: no interface exists
            backend=self.name,
            truncated=truncated,
        )
