import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.helpers import make_ctx, make_state
from yantra_server.config import PermissionRule
from yantra_server.db.models import PermissionLogRow, ToolCallRow
from yantra_server.state import AppState
from yantra_server.tools.base import Tool, ToolContext, ToolResult
from yantra_server.tools.compaction import compact_observation, compact_text
from yantra_server.tools.permissions import PermissionPolicy
from yantra_server.tools.runtime import idempotency_key


@pytest.fixture
def state() -> AppState:
    return make_state()


def get_tool(state: AppState, name: str) -> Tool:
    tool = state.tools.registry.get(name)
    assert tool is not None
    return tool


# ------------------------------------------------------------------ policy engine


def test_default_policy_matrix(state: AppState) -> None:
    policy = state.tools.policy
    read = policy.evaluate(get_tool(state, "read_file"), {"path": "a.txt"}, "ask")
    assert read.decision == "allow"

    delete = policy.evaluate(get_tool(state, "delete_file"), {"path": "a.txt"}, "auto")
    assert delete.decision == "deny"

    rmrf = policy.evaluate(get_tool(state, "bash"), {"cmd": "sudo rm -rf /data"}, "auto")
    assert rmrf.decision == "deny"

    bash_auto = policy.evaluate(get_tool(state, "bash"), {"cmd": "ls"}, "auto")
    assert bash_auto.decision == "allow"
    bash_ask = policy.evaluate(get_tool(state, "bash"), {"cmd": "ls"}, "ask")
    assert bash_ask.decision == "ask"

    write_plan = policy.evaluate(
        get_tool(state, "write_file"), {"path": "x", "content": ""}, "plan"
    )
    assert write_plan.decision == "deny"


def test_custom_rule_order_and_globs() -> None:
    rules = [
        PermissionRule(tool="bash", cmd_regex="pytest", decision="allow"),
        PermissionRule(tool="bash", decision="deny"),
        PermissionRule(tool="write_*", path_glob="reports/**", decision="allow"),
        PermissionRule(tool="*", decision="ask"),
    ]
    policy = PermissionPolicy(rules)

    class FakeBash(Tool):
        name = "bash"
        description = ""
        from pydantic import BaseModel

        class Args(BaseModel):
            cmd: str

        side_effects = "exec"

        async def run(self, args: object, ctx: ToolContext) -> ToolResult:
            return ToolResult()

    bash = FakeBash()
    assert policy.evaluate(bash, {"cmd": "python -m pytest -q"}, "auto").decision == "allow"
    assert policy.evaluate(bash, {"cmd": "curl example.com"}, "auto").decision == "deny"


# ------------------------------------------------------------------ broker / prompts


async def test_ask_flow_once_and_always(state: AppState, tmp_path: Path) -> None:
    ctx = make_ctx(state, tmp_path / "ws", mode="ask")
    published: list[dict[str, object]] = []

    original_publish = state.bus.publish

    def capture(note: object) -> None:
        published.append(note.model_dump())  # type: ignore[attr-defined]
        request_id = getattr(note, "request_id", None)
        if request_id:
            # simulate the user answering "always" shortly after
            asyncio.get_running_loop().call_later(
                0.05, state.tools.broker.resolve, request_id, "always", None
            )
        original_publish(note)  # type: ignore[arg-type]

    state.bus.publish = capture  # type: ignore[method-assign]
    result = await state.tools.runtime.execute(
        "write_file", {"path": "r.txt", "content": "hi"}, ctx
    )
    assert result.ok, result.error
    assert published and published[0]["tool"] == "write_file"

    # A reusable grant is scoped to the exact operation, not every file this tool can write.
    published.clear()
    result2 = await state.tools.runtime.execute(
        "write_file", {"path": "r2.txt", "content": "hi"}, ctx
    )
    assert result2.ok and published


async def test_deny_flow_logged(state: AppState, tmp_path: Path) -> None:
    ctx = make_ctx(state, tmp_path / "ws", mode="ask")

    def auto_deny(note: object) -> None:
        request_id = getattr(note, "request_id", None)
        if request_id:
            asyncio.get_running_loop().call_later(
                0.05, state.tools.broker.resolve, request_id, "deny", "not now"
            )

    state.bus.publish = auto_deny  # type: ignore[method-assign]
    result = await state.tools.runtime.execute("write_file", {"path": "n.txt", "content": "x"}, ctx)
    assert not result.ok and "denied" in (result.error or "")
    with state.db.session() as s:
        logs = list(s.execute(select(PermissionLogRow)).scalars())
    assert any(log.decision == "deny" and log.decided_by == "user" for log in logs)
    assert state.audit.verify().ok


async def test_pending_permission_is_bound_to_run_and_cleared(state: AppState) -> None:
    broker = state.tools.broker
    broker.policy = PermissionPolicy([])
    tool = get_tool(state, "write_file")
    task = asyncio.create_task(
        broker.check(
            tool,
            {"path": "report.md", "content": "draft"},
            mode="auto",
            run_id="run-a",
            task_id="t1",
        )
    )
    try:
        await asyncio.sleep(0)
        pending = broker.pending_for_run("run-a")
        assert len(pending) == 1
        assert broker.pending_for_run("run-b") == []
        request_id = pending[0]["request_id"]
        assert not broker.resolve(request_id, "once", run_id="run-b")
        assert not broker.resolve(request_id, "invalid", run_id="run-a")
        assert not task.done()
        assert broker.resolve(request_id, "once", run_id="run-a")
        allowed, _ = await task
        assert allowed
        assert broker.pending_for_run("run-a") == []
        assert not broker.resolve(request_id, "once", run_id="run-a")
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_cancelled_permission_cannot_be_reused(state: AppState) -> None:
    broker = state.tools.broker
    broker.policy = PermissionPolicy([])
    task = asyncio.create_task(
        broker.check(
            get_tool(state, "write_file"), {"path": "x"}, mode="auto", run_id="run-a", task_id="t1"
        )
    )
    await asyncio.sleep(0)
    request_id = broker.pending_for_run("run-a")[0]["request_id"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.pending_for_run("run-a") == []
    assert not broker.resolve(request_id, "once", run_id="run-a")


# ------------------------------------------------------------------ runtime behaviours


async def test_unknown_tool_and_invalid_args(state: AppState, tmp_path: Path) -> None:
    ctx = make_ctx(state, tmp_path / "ws")
    unknown = await state.tools.runtime.execute("teleport", {}, ctx)
    assert not unknown.ok and "unknown tool" in (unknown.error or "")
    invalid = await state.tools.runtime.execute("read_file", {"offset": -3}, ctx)
    assert not invalid.ok and "invalid arguments" in (invalid.error or "")


async def test_manifest_restriction(state: AppState, tmp_path: Path) -> None:
    ctx = make_ctx(state, tmp_path / "ws")
    ctx.allowed_tools = ["read_file"]
    blocked = await state.tools.runtime.execute("write_file", {"path": "a", "content": ""}, ctx)
    assert not blocked.ok and "not in this agent's manifest" in (blocked.error or "")


async def test_idempotent_replay(state: AppState, tmp_path: Path) -> None:
    ctx = make_ctx(state, tmp_path / "ws", idempotency_key=idempotency_key("t1", 1, "python", {}))
    first = await state.tools.runtime.execute(
        "python", {"code": "import random; print(random.random())"}, ctx
    )
    assert first.ok
    replay = await state.tools.runtime.execute(
        "python", {"code": "import random; print(random.random())"}, ctx
    )
    assert replay.content == first.content  # recorded result, not a re-execution
    with state.db.session() as s:
        rows = list(s.execute(select(ToolCallRow).where(ToolCallRow.status == "done")).scalars())
    assert len(rows) == 1


async def test_large_output_offloaded(state: AppState, tmp_path: Path) -> None:
    ctx = make_ctx(state, tmp_path / "ws")
    result = await state.tools.runtime.execute("python", {"code": "print('line\\n' * 5000)"}, ctx)
    assert result.ok and result.artifact_id is not None
    stored = state.artifacts.read_text(result.artifact_id)
    assert stored.count("line") >= 4900


# ------------------------------------------------------------------ registry surface


def test_manifest_and_specs(state: AppState) -> None:
    registry = state.tools.registry
    manifest = registry.manifest_text(["read_file", "bash"])
    assert "- read_file(path, offset?, limit?)" in manifest
    assert "- bash(cmd, cwd?, timeout_s?)" in manifest
    specs = registry.specs_for(["edit_file"])
    assert specs[0].parameters["required"] == ["path", "old", "new"]
    assert specs[0].parameters["additionalProperties"] is False


def test_fewshots_loaded(state: AppState) -> None:
    text = state.tools.registry.fewshots_text(["read_file", "python"])
    assert "read_file examples:" in text
    assert "pandas" in text


# ------------------------------------------------------------------ compaction


def test_compact_text_head_tail() -> None:
    text = "\n".join(f"line{i}" for i in range(500))
    compacted, truncated = compact_text(text, head=10, tail=5)
    assert truncated
    assert "line0" in compacted and "line499" in compacted
    assert "[485 lines omitted]" in compacted


def test_compact_observation_references_artifact() -> None:
    result = ToolResult(
        summary="big output",
        content="\n".join(f"row {i}" for i in range(400)),
        artifact_id="abc123",
    )
    obs = compact_observation(result, head=10, tail=5)
    assert "read_artifact" in obs and "abc123" in obs
