import sys
from pathlib import Path

import pytest

from tests.helpers import make_ctx, make_state
from yantra_server.sandbox.base import SandboxLimits
from yantra_server.sandbox.bwrap import bwrap_command
from yantra_server.sandbox.docker import docker_command
from yantra_server.sandbox.local import LocalSandbox, shell_argv
from yantra_server.state import AppState


@pytest.fixture
def state() -> AppState:
    return make_state()


def test_bwrap_command_shape(tmp_path: Path) -> None:
    cmd = bwrap_command(
        ["python", "run.py"],
        workspace=tmp_path,
        cwd=tmp_path / "sub",
        limits=SandboxLimits(),
    )
    text = " ".join(cmd)
    assert cmd[0] == "bwrap"
    assert "--unshare-all" in cmd and "--unshare-net" in cmd
    assert "--die-with-parent" in cmd
    assert f"--bind {tmp_path} /work" in text
    assert "--tmpfs /tmp" in text
    assert "--chdir /work/sub" in text
    assert cmd[-2:] == ["python", "run.py"]
    # workspace is the ONLY writable bind
    binds = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--bind"]
    assert binds == [str(tmp_path)]


def test_docker_command_shape(tmp_path: Path) -> None:
    cmd = docker_command(
        ["python", "-c", "print(1)"],
        workspace=tmp_path,
        cwd=tmp_path,
        limits=SandboxLimits(max_rss_mb=2048, max_pids=100),
    )
    text = " ".join(cmd)
    assert "--network none" in text
    assert "--read-only" in text
    assert "--cap-drop ALL" in text
    assert "--pull=never" in cmd
    assert "--user" in cmd
    assert cmd[cmd.index("--user") + 1] != "0:0"
    assert "--pids-limit 100" in text
    assert "--memory 2048m" in text
    assert f"{tmp_path.resolve()}:/work" in text


async def test_local_sandbox_runs_python(tmp_path: Path) -> None:
    sandbox = LocalSandbox(tmp_path, SandboxLimits(timeout_s=30))
    result = await sandbox.run([sys.executable, "-c", "print('hello from sandbox')"])
    assert result.exit_code == 0
    assert "hello from sandbox" in result.stdout
    assert result.backend == "local"


async def test_local_sandbox_timeout_kills(tmp_path: Path) -> None:
    sandbox = LocalSandbox(tmp_path, SandboxLimits(timeout_s=2))
    result = await sandbox.run([sys.executable, "-c", "import time; time.sleep(60)"], timeout_s=1.5)
    assert result.timed_out


async def test_timeout_still_applies_after_child_closes_output(tmp_path: Path) -> None:
    sandbox = LocalSandbox(tmp_path, SandboxLimits(timeout_s=1))
    result = await sandbox.run(
        [sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(60)"],
        timeout_s=0.5,
    )
    assert result.timed_out
    assert result.wall_s < 10


async def test_files_changed_detected(tmp_path: Path) -> None:
    sandbox = LocalSandbox(tmp_path, SandboxLimits())
    result = await sandbox.run(
        [sys.executable, "-c", "open('made.txt', 'w').write('x')"], cwd=tmp_path
    )
    assert "made.txt" in result.files_changed


async def test_output_cap(tmp_path: Path) -> None:
    limits = SandboxLimits(max_output_bytes=10_000)
    sandbox = LocalSandbox(tmp_path, limits)
    result = await sandbox.run([sys.executable, "-c", "print('x' * 100000)"])
    assert result.truncated
    assert len(result.stdout) <= 10_100


def test_select_sandbox_refuses_local_when_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yantra_server.config import SandboxConfig
    from yantra_server.sandbox import SandboxError, select_sandbox
    from yantra_server.sandbox.bwrap import BwrapSandbox
    from yantra_server.sandbox.docker import DockerSandbox

    monkeypatch.setattr(BwrapSandbox, "available", classmethod(lambda cls: False))
    monkeypatch.setattr(DockerSandbox, "available", classmethod(lambda cls: False))
    with pytest.raises(SandboxError, match="refusing the 'local' backend"):
        select_sandbox(SandboxConfig(backend="auto"), tmp_path, sealed=True)
    sandbox = select_sandbox(SandboxConfig(backend="auto"), tmp_path, sealed=False)
    assert sandbox.name == "local"


# ------------------------------------------------------------------ exec tools


async def test_python_tool(state: AppState, tmp_path: Path) -> None:
    ctx = make_ctx(state, tmp_path / "ws")
    result = await state.tools.runtime.execute(
        "python", {"code": "print(21 * 2)\nopen('out.txt', 'w').write('done')"}, ctx
    )
    assert result.ok, result.error
    assert "42" in result.content
    assert "out.txt" in result.data["files_changed"]
    assert result.data["sandbox"]["backend"] == "local"


async def test_python_tool_failure_reported(state: AppState, tmp_path: Path) -> None:
    ctx = make_ctx(state, tmp_path / "ws")
    result = await state.tools.runtime.execute("python", {"code": "raise ValueError('bad')"}, ctx)
    assert not result.ok
    assert "ValueError: bad" in result.content


async def test_bash_tool(state: AppState, tmp_path: Path) -> None:
    ctx = make_ctx(state, tmp_path / "ws")
    result = await state.tools.runtime.execute("bash", {"cmd": "echo sandboxed"}, ctx)
    assert result.ok and "sandboxed" in result.content


async def test_dangerous_bash_denied_by_policy(state: AppState, tmp_path: Path) -> None:
    ctx = make_ctx(state, tmp_path / "ws", mode="auto")
    result = await state.tools.runtime.execute("bash", {"cmd": "rm -rf /"}, ctx)
    assert not result.ok and "denied" in (result.error or "")


async def test_run_tests_auto_detect(state: AppState, tmp_path: Path) -> None:
    ctx = make_ctx(state, tmp_path / "ws")
    (ctx.workspace / "test_ok.py").write_text("def test_pass():\n    assert 1 + 1 == 2\n")
    result = await state.tools.runtime.execute("run_tests", {"cmd": "auto"}, ctx)
    assert result.ok, result.content[-500:]
    assert "passed" in result.summary


async def test_sql_query_readonly(state: AppState, tmp_path: Path) -> None:
    import sqlite3

    ctx = make_ctx(state, tmp_path / "ws")
    db_file = ctx.workspace / "tags.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE tags (tag TEXT, sev TEXT)")
    conn.execute("INSERT INTO tags VALUES ('FIC-3201', 'high')")
    conn.commit()
    conn.close()
    result = await state.tools.runtime.execute(
        "sql_query", {"db_path": "tags.db", "query": "SELECT tag FROM tags"}, ctx
    )
    assert result.ok and "FIC-3201" in result.content
    blocked = await state.tools.runtime.execute(
        "sql_query", {"db_path": "tags.db", "query": "DROP TABLE tags"}, ctx
    )
    assert not blocked.ok and "read-only" in (blocked.error or "")


def test_shell_argv_prefers_bash() -> None:
    argv = shell_argv("echo hi")
    assert argv[-1] == "echo hi"
    assert argv[0].lower().endswith(("bash", "bash.exe", "cmd", "sh"))
