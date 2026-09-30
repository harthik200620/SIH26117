"""Start BlackBox in a Linux network namespace; expose only a loopback UI bridge.

All packages, models and native runtimes must already be provisioned. No fallback.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from types import FrameType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from yantra_server.seal.env import SEAL_ENV  # noqa: E402
from yantra_server.seal.namespace import namespace_command, reject_special_files  # noqa: E402


def mounts(root: Path, python_prefix: Path, model_dir: Path, runtime: Path) -> list[Path]:
    paths = [
        root / name
        for name in ("server", "agents", "knowledge", "templates", "skills", "tools", "scripts")
    ]
    paths += [
        root / "models" / name
        for name in ("profiles", "registry.yaml", "routing.yaml", "catalog.json")
    ]
    paths += [root / name for name in ("pyproject.toml", "LICENSE", "NOTICE.md")]
    paths += [python_prefix, model_dir, runtime.parent]
    base = Path(sys.base_prefix).resolve()
    if not base.is_relative_to(Path("/usr")):
        paths.append(base)
    return list(dict.fromkeys(path for path in paths if path.exists()))


def access_key(data: Path) -> str:
    path = data / "admin.key"
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RuntimeError("Strict access keys require Linux O_NOFOLLOW support")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | nofollow)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != getattr(os, "getuid")() or info.st_mode & 0o077:  # noqa: B009 - Linux-only API, checked on Windows too
                raise ValueError("admin.key must belong to this user and have mode 0600") from None
            token = stream.read().strip()
        if len(token) < 32:
            raise ValueError("admin.key must contain at least 32 characters") from None
        return token
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as stream:
        stream.write(token + "\n")
    return token


async def serve_bridge(proc: subprocess.Popen[bytes], uds: Path, port: int) -> None:
    """Fixed Unix-socket destination. There is no URL proxy or outbound TCP connector."""
    active: set[asyncio.Task[None]] = set()

    async def relay(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
        while chunk := await asyncio.wait_for(source.read(65536), 120):
            destination.write(chunk)
            await asyncio.wait_for(destination.drain(), 30)

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(active) >= 64:
            writer.close()
            return
        task = asyncio.current_task()
        if task is None:
            writer.close()
            return
        active.add(task)
        upstream = None
        pumps = []
        try:
            upstream_reader, upstream = await asyncio.wait_for(
                getattr(asyncio, "open_unix_connection")(str(uds)), 5  # noqa: B009 - Linux-only API
            )
            pumps = [
                asyncio.create_task(relay(reader, upstream)),
                asyncio.create_task(relay(upstream_reader, writer)),
            ]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, TimeoutError):
            pass
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            writer.close()
            if upstream:
                upstream.close()
            active.discard(task)

    # Start only after uvicorn creates its socket, after namespace verification succeeds.
    for _ in range(1200):
        if proc.poll() is not None:
            raise RuntimeError(f"Isolated server exited with code {proc.returncode}")
        if await asyncio.to_thread(uds.exists):
            break
        await asyncio.sleep(0.1)
    else:
        raise RuntimeError("Isolated server did not become ready within 120 seconds")
    server = await asyncio.start_server(connection, "127.0.0.1", port)
    print(f"BlackBox strict runtime: http://127.0.0.1:{port}", flush=True)
    try:
        async with server:
            while proc.poll() is None:  # noqa: ASYNC110 - watching an external process, not an asyncio producer
                await asyncio.sleep(0.5)
    finally:
        for task in list(active):
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)


def main() -> None:
    def stop(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--llama-server", type=Path, required=True)
    parser.add_argument("--port", type=int, default=7331)
    parser.add_argument("--context", type=int, default=8192)
    parser.add_argument("--threads", type=int, default=min(8, os.cpu_count() or 4))
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Linux" or not shutil.which("bwrap"):
        parser.error(
            "Strict runtime requires Linux and bubblewrap. No unisolated fallback is allowed."
        )
    if not 1024 <= args.port <= 65535 or not 2048 <= args.context <= 131072 or args.threads < 1:
        parser.error("Invalid port, context or thread count")
    workspace, data, models, runtime = [
        p.expanduser().resolve()
        for p in (args.workspace, args.data_dir, args.models_dir, args.llama_server)
    ]
    if not workspace.is_dir() or not models.is_dir() or not runtime.is_file():
        parser.error("Workspace, model directory and preinstalled llama-server must exist")
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if data.stat().st_uid != getattr(os, "getuid")() or data.stat().st_mode & 0o077:  # noqa: B009 - Linux-only API
        parser.error(
            "Use a private data directory owned by this user with mode 0700 on a Linux filesystem"
        )
    readonly = mounts(ROOT, Path(sys.prefix).absolute(), models, runtime)
    writable = [workspace, data]
    for write in writable:
        for read in readonly:
            if write.is_relative_to(read) or read.is_relative_to(write):
                parser.error(f"Writable and read-only mounts overlap: {write} / {read}")
    if workspace.is_relative_to(data) or data.is_relative_to(workspace):
        parser.error("Workspace and private state must be separate directories")
    for path in [*readonly, *writable]:
        if path.is_dir():
            reject_special_files(path)
    access_key(data)
    env = dict(SEAL_ENV)
    with tempfile.TemporaryDirectory(prefix="blackbox-bridge-") as bridge:
        uds = Path(bridge) / "api.sock"
        writable.append(Path(bridge))
        probe = ROOT / "server/yantra_server/seal/namespace.py"
        check = [
            sys.executable,
            "-I",
            "-c",
            "import runpy,json,sys; m=runpy.run_path(sys.argv[1]); r=m['check_with_child'](); print(json.dumps(r)); sys.exit(0 if r['verified'] else 1)",
            str(probe),
        ]
        checked = subprocess.run(
            namespace_command(
                check, readonly=readonly, writable=writable, cwd=ROOT, environment=env
            ),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            close_fds=True,
        )
        if checked.returncode:
            parser.error(
                "Namespace verification failed; no server launched: "
                + checked.stderr[-800:]
                + checked.stdout[-800:]
            )
        evidence = json.loads(checked.stdout)
        (data / "namespace-preflight.json").write_text(
            json.dumps(evidence, indent=2), encoding="utf-8"
        )
        print(
            "Namespace and child native-socket checks passed. Evidence: "
            + str(data / "namespace-preflight.json"),
            flush=True,
        )
        if args.verify_only:
            return
        command = [
            sys.executable,
            str(ROOT / "scripts/run_workbench.py"),
            "--workspace",
            str(workspace),
            "--data-dir",
            str(data),
            "--models-dir",
            str(models),
            "--llama-server",
            str(runtime),
            "--context",
            str(args.context),
            "--threads",
            str(args.threads),
            "--port",
            str(args.port),
            "--sandbox",
            "bwrap",
            "--require-namespace",
            "--admin-token-file",
            str(data / "admin.key"),
            "--uds",
            str(uds),
        ]
        proc = subprocess.Popen(
            namespace_command(
                command, readonly=readonly, writable=writable, cwd=ROOT, environment=env
            ),
            close_fds=True,
            start_new_session=True,
        )
        print(
            "Access key is stored in " + str(data / "admin.key") + " (never sent to inference).",
            flush=True,
        )
        try:
            asyncio.run(serve_bridge(proc, uds, args.port))
        except KeyboardInterrupt:
            pass
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
