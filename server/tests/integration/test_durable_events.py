import json

import pytest
from fastapi.testclient import TestClient

from tests.helpers import make_state
from yantra_server.app import create_app
from yantra_server.config import load_config
from yantra_server.db.models import RunRow, SessionRow
from yantra_server.protocol.messages import AssistantDelta
from yantra_server.rpc import Connection, EventBus


def test_events_survive_new_bus_and_preserve_cursor() -> None:
    state = make_state()
    first = EventBus(state.db)
    first.publish(AssistantDelta(run_id="alpha", text="first"))
    first.publish(AssistantDelta(run_id="beta", text="other run"))
    second = EventBus(state.db)
    assert second.latest_seq("alpha") == 1
    second.publish(AssistantDelta(run_id="alpha", text="second"))
    assert [event["params"]["text"] for event in second.replay("alpha", 0)] == ["first", "second"]
    assert [event["params"]["seq"] for event in second.replay("alpha", 1)] == [2]


def test_persistence_failure_does_not_broadcast_success(monkeypatch) -> None:
    state = make_state()
    connection = Connection(id="observer")
    state.bus.attach(connection)

    def broken():
        raise OSError("storage unavailable")

    monkeypatch.setattr(state.db, "session", broken)
    with pytest.raises(OSError):
        state.bus.publish(AssistantDelta(run_id="alpha", text="must persist first"))
    assert connection.outbound.empty()


def test_sse_replays_all_pages_and_reconnects_after_restart(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("yantra_server.rpc.RING_SIZE", 2)
    app = create_app(load_config())
    state = app.state.yantra
    with state.db.session() as db:
        session = SessionRow(workspace_path=str(tmp_path), mode="ask")
        db.add(session)
        db.flush()
        run = RunRow(
            session_id=session.id,
            workspace_path=str(tmp_path),
            goal_text="synthetic",
            status="running",
            mode="ask",
        )
        db.add(run)
        db.flush()
        run_id = run.id
    for n in range(7):
        state.bus.publish(AssistantDelta(run_id=run_id, text=str(n)))
    # Empty memory rings with the same durable database, like a process restart.
    state.bus = EventBus(state.db)
    with TestClient(app) as client:
        response = client.get(
            f"/api/workbench/runs/{run_id}/events", headers={"last-event-id": "2"}
        )
        frames = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        assert [
            frame["params"]["text"] for frame in frames if frame["method"] == "assistant.delta"
        ] == ["2", "3", "4", "5", "6"]
        assert frames[-1]["params"]["status"] == "interrupted"
