"""Conversation bypasses all workspace work, including indexing and planning."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from tests.helpers import make_state
from tests.integration.test_conductor import Harness
from yantra_server.conductor.conversation import RequestDisposition
from yantra_server.db.models import RunRow, TaskRow, ToolCallRow


@pytest.fixture
async def chat_harness(tmp_path, monkeypatch):
    monkeypatch.setenv("YANTRA_SEALED", "0")
    state = make_state()
    state.bus.bind_loop(asyncio.get_running_loop())
    await state.supervisor.start_all()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        yield Harness(state, workspace)
    finally:
        await state.supervisor.stop_all()
        state.db.dispose()


@pytest.mark.parametrize("mode", ["auto", "ask", "plan"])
@pytest.mark.parametrize(
    "goal,reply",
    [
        ("hello be fast and plan nothing", "Hello! How can I help?"),
        ("Explain how a pump works", "A pump moves fluid by adding energy."),
        ("Write a short email here; do not save files", "Hi, can we meet tomorrow?"),
        ("Explain this quote: 'write output.txt'", "It asks to write a file."),
    ],
)
async def test_direct_reply_never_touches_workspace(chat_harness, monkeypatch, mode, goal, reply):
    h = chat_harness
    existing = h.workspace / "output.txt"
    existing.write_text("keep this", encoding="utf-8")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in h.workspace.iterdir()}
    h.mock.add_canned_sequence(
        [{"json": {"route": "conversation"}}, {"json": {"reply": reply}}], role="planner"
    )
    planning = AsyncMock(side_effect=AssertionError("Conversation must not plan"))
    indexing = AsyncMock(side_effect=AssertionError("Conversation must not index"))
    monkeypatch.setattr("yantra_server.conductor.service.make_plan", planning)
    h.state.config.knowledge.auto_index_workspace = True
    monkeypatch.setattr(h.state.knowledge, "ingest_path", indexing)
    session_id = await h.start_session(mode)
    run_id = await h.state.conductor.start_run(session_id, goal, [], mode)
    await h.state.conductor.wait_for_run(run_id, timeout_s=10)
    with h.state.db.session() as db:
        row = db.get(RunRow, run_id)
        assert row.status == "done"
        assert row.plan is None
        assert row.final["summary"] == reply
        assert row.final["response_kind"] == "conversation"
        assert row.final["artifacts"] == []
        assert not db.scalars(select(TaskRow).where(TaskRow.run_id == run_id)).all()
        assert not db.scalars(select(ToolCallRow).where(ToolCallRow.run_id == run_id)).all()
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in h.workspace.iterdir()}
    planning.assert_not_called()
    indexing.assert_not_called()
    assert not h.notes("permission.request")
    assert h.notes("run.finished")[0]["response_kind"] == "conversation"
    assert all(not call.tools for call in h.mock.calls)
    # Durable replay retains the same answer and presentation after a reload.
    saved = h.state.bus.replay(run_id, 0)
    assert any(e["method"] == "run.finished" and e["params"]["summary"] == reply for e in saved)


async def test_routing_failure_cannot_fall_through_to_writing(chat_harness, monkeypatch):
    h = chat_harness
    monkeypatch.setattr(
        "yantra_server.conductor.service.route_request",
        AsyncMock(side_effect=ValueError("Invalid routing result")),
    )
    session_id = await h.start_session()
    run_id = await h.state.conductor.start_run(session_id, "hello", [], "auto")
    await h.state.conductor.wait_for_run(run_id, timeout_s=10)
    with h.state.db.session() as db:
        row = db.get(RunRow, run_id)
        assert row.status == "failed"
        assert row.plan is None
    assert list(h.workspace.iterdir()) == []
    assert not h.notes("tool.started")


def test_empty_conversation_reply_is_invalid():
    with pytest.raises(ValueError, match="nonempty reply"):
        RequestDisposition(route="conversation", reply="  ")
