import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from yantra_server.app import create_app
from yantra_server.config import load_config


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("YANTRA_PATHS__DATA_DIR", str(tmp_path / "data"))
    app = create_app(load_config())
    with TestClient(app) as test_client:
        yield test_client


def rpc(ws: Any, method: str, params: dict[str, Any], rid: int = 1) -> dict[str, Any]:
    ws.send_text(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
    while True:
        frame = json.loads(ws.receive_text())
        if frame.get("id") == rid:
            return frame


@pytest.mark.integration
def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["db"] == "sqlite"


def test_text_preview_is_bounded_and_workspace_scoped(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "preview"
    root.mkdir()
    (root / "report.md").write_text("<script>do not execute</script>\nEvidence", encoding="utf-8")
    response = client.get(
        "/api/workbench/file-preview", params={"workspace_path": str(root), "path": "report.md"}
    )
    assert response.status_code == 200
    assert "<script>" in response.json()["text"]  # JSON text; the UI renders inside <pre>.
    (tmp_path / "outside.txt").write_text("private", encoding="utf-8")
    assert (
        client.get(
            "/api/workbench/file-preview",
            params={"workspace_path": str(root), "path": "../outside.txt"},
        ).status_code
        == 404
    )
    (root / ".env").write_text("private", encoding="utf-8")
    assert (
        client.get(
            "/api/workbench/file-preview", params={"workspace_path": str(root), "path": ".env"}
        ).status_code
        == 404
    )
    (root / "binary.dat").write_bytes(b"a\0b")
    assert (
        client.get(
            "/api/workbench/file-preview",
            params={"workspace_path": str(root), "path": "binary.dat"},
        ).status_code
        == 415
    )
    (root / "large.txt").write_text("€" * 40_000, encoding="utf-8")
    response = client.get(
        "/api/workbench/file-preview", params={"workspace_path": str(root), "path": "large.txt"}
    )
    assert response.status_code == 200 and response.json()["truncated"]
    assert len(response.json()["text"].encode("utf-8")) <= 100_000


def test_optional_login_cookie_and_logout(tmp_path: Path) -> None:
    app = create_app(load_config(cli_overrides={"server": {"admin_token": "test-key"}}))
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 403
        assert client.post("/auth/login", json={"token": "wrong"}).status_code == 403
        response = client.post("/auth/login", json={"token": "test-key"})
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert client.get("/api/health").status_code == 200
        client.post("/auth/logout")
        assert client.get("/api/health").status_code == 403


@pytest.mark.integration
def test_ws_ping_and_session_flow(client: TestClient, tmp_path: Path) -> None:
    with client.websocket_connect("/rpc") as ws:
        pong = rpc(ws, "ping", {})
        assert pong["result"]["pong"] is True

        created = rpc(ws, "session.create", {"workspace": str(tmp_path), "mode": "ask"}, rid=2)
        session_id = created["result"]["session_id"]
        assert session_id

        listed = rpc(ws, "session.list", {}, rid=3)
        assert any(s["session_id"] == session_id for s in listed["result"]["sessions"])

        resumed = rpc(ws, "session.resume", {"session_id": session_id}, rid=4)
        assert resumed["result"]["session"]["workspace"] == str(tmp_path.resolve())


@pytest.mark.integration
def test_ws_unknown_method_and_bad_params(client: TestClient) -> None:
    with client.websocket_connect("/rpc") as ws:
        err = rpc(ws, "does.not.exist", {})
        assert err["error"]["code"] == -32601
        bad = rpc(ws, "session.create", {"workspace": 42}, rid=2)
        assert bad["error"]["code"] == -32602


@pytest.mark.integration
def test_api_evals_lists_recorded_runs(client: TestClient) -> None:
    assert client.get("/api/evals").json() == {"eval_runs": []}

    state = client.app.state.yantra  # type: ignore[attr-defined]
    from yantra_server.db.models import EvalResultRow, EvalRunRow

    with state.db.session() as s:
        run = EvalRunRow(suite="pid", profile="mock", status="done", meta={"pass_rate": 1.0})
        s.add(run)
        s.flush()
        s.add(
            EvalResultRow(
                eval_run_id=run.id,
                case_id="pid_3-1201_revC",
                passed=True,
                score=1.0,
                metrics={"planted": 3},
                detail={"detail": "3/3 found"},
            )
        )
    body = client.get("/api/evals").json()
    assert len(body["eval_runs"]) == 1
    row = body["eval_runs"][0]
    assert row["suite"] == "pid" and row["pass_rate"] == 1.0
    assert row["cases"][0]["case_id"] == "pid_3-1201_revC" and row["cases"][0]["passed"]


@pytest.mark.integration
def test_seal_status_and_audit_started(client: TestClient) -> None:
    body = client.get("/api/seal").json()
    assert body["blocked_attempts_total"] == 0
    assert body["allowlist"]

    state = client.app.state.yantra  # type: ignore[attr-defined]
    head = state.audit.head()
    assert head is not None and head.event == "server.start"
    assert state.audit.verify().ok


def test_terminal_sse_and_download_boundary(client: TestClient, tmp_path: Path) -> None:
    from yantra_server.db.models import RunRow, SessionRow

    state = client.app.state.yantra
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "answer.txt").write_text("verified", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("private", encoding="utf-8")
    state.config.paths.workspace_roots = [root]
    with state.db.session() as db:
        session = SessionRow(workspace_path=str(root), mode="auto")
        db.add(session)
        db.flush()
        run = RunRow(
            session_id=session.id,
            workspace_path=str(root),
            goal_text="test",
            mode="auto",
            status="done",
            final={"summary": "complete"},
        )
        db.add(run)
        db.flush()
        run_id = run.id
    response = client.get(f"/api/workbench/runs/{run_id}/events", headers={"last-event-id": "123"})
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert '"method": "run.finished"' in response.text
    assert (
        client.get(
            f"/api/workbench/runs/{run_id}/events", headers={"last-event-id": "bad"}
        ).status_code
        == 400
    )
    assert (
        client.get(f"/api/workbench/runs/{run_id}/download", params={"path": "answer.txt"}).text
        == "verified"
    )
    assert (
        client.get(
            f"/api/workbench/runs/{run_id}/download", params={"path": "../outside.txt"}
        ).status_code
        == 404
    )


def test_execution_evidence_is_run_scoped_and_requires_result(
    client: TestClient, tmp_path: Path
) -> None:
    from yantra_server.db.models import RunRow, SessionRow, ToolCallRow
    from yantra_server.tools.base import ToolResult

    state = client.app.state.yantra
    with state.db.session() as db:
        session = SessionRow(workspace_path=str(tmp_path))
        db.add(session)
        db.flush()
        run = RunRow(session_id=session.id, workspace_path=str(tmp_path), goal_text="synthetic")
        db.add(run)
        db.flush()
        run_id = run.id
    result = ToolResult(
        ok=True,
        summary="executed",
        data={"sandbox": {"exit_code": 0, "timed_out": False, "backend": "bwrap"}},
    )
    ref = state.artifacts.put_text(result.model_dump_json(), kind="tool_output")
    with state.db.session() as db:
        for identifier, owner, artifact in [
            ("good", run_id, ref),
            ("missing", run_id, None),
            ("other", "other-run", ref),
        ]:
            db.add(
                ToolCallRow(
                    id=identifier,
                    run_id=owner,
                    task_id="t1",
                    tool="python",
                    args={"code": "private code"},
                    idempotency_key=identifier,
                    status="done",
                    result_artifact_id=artifact,
                )
            )
    response = client.get(f"/api/workbench/runs/{run_id}/execution-evidence")
    assert response.status_code == 200
    calls = {c["call_id"]: c for c in response.json()["calls"]}
    assert set(calls) == {"good", "missing"}
    assert calls["good"]["result_available"] and calls["good"]["exit_code"] == 0
    assert not calls["missing"]["result_available"]
    assert "private code" not in response.text
    assert client.get("/api/workbench/runs/unknown/execution-evidence").status_code == 404


def test_validation_record_preserves_failed_attempts_and_actual_routes(
    client: TestClient, tmp_path: Path
) -> None:
    from yantra_server.db.models import RouterDecisionRow, RunRow, SessionRow, VerificationRow

    state = client.app.state.yantra
    with state.db.session() as db:
        session = SessionRow(workspace_path=str(tmp_path))
        db.add(session)
        db.flush()
        run = RunRow(
            session_id=session.id,
            workspace_path=str(tmp_path),
            goal_text="synthetic",
            status="failed",
        )
        db.add(run)
        db.flush()
        run_id = run.id
        db.add(
            VerificationRow(
                run_id=run_id,
                task_id="t1",
                attempt=0,
                verdict="fail",
                checks=[{"passed": False, "detail": "broken formula"}],
            )
        )
        db.add(
            VerificationRow(
                run_id="another-run",
                task_id="t1",
                verdict="pass",
                checks=[{"detail": "not this task"}],
            )
        )
        db.add(
            RouterDecisionRow(
                run_id=run_id,
                role="executor",
                chosen="actual-local-model",
                reason="capability match",
            )
        )
    response = client.get(f"/api/workbench/runs/{run_id}/validation-record")
    assert response.status_code == 200
    record = response.json()
    assert record["run"]["status"] == "failed"
    assert len(record["verifications"]) == 1
    assert record["verifications"][0]["checks"][0]["detail"] == "broken formula"
    assert record["model_routes"][0]["model"] == "actual-local-model"
    assert not any(record["truncated"].values())
    assert record["limitations"]
    assert "attachment;" in response.headers["content-disposition"]
    assert "no-store" in response.headers["cache-control"].split(", ")
    assert str(tmp_path) not in response.text
    assert client.get("/api/workbench/runs/absent/validation-record").status_code == 404


def test_saved_render_download_checks_scope_and_hash(client: TestClient, tmp_path: Path) -> None:
    from yantra_server.db.models import ArtifactRow, RunRow, SessionRow

    state = client.app.state.yantra
    with state.db.session() as db:
        session = SessionRow(workspace_path=str(tmp_path))
        db.add(session)
        db.flush()
        run = RunRow(session_id=session.id, workspace_path=str(tmp_path), goal_text="revision test")
        db.add(run)
        db.flush()
        run_id = run.id
    original = b"original revision bytes"
    revision = state.artifacts.put_bytes(
        original, kind="render_revision", run_id=run_id, meta={"filename": "example.xlsx"}
    )
    unrelated = state.artifacts.put_bytes(
        b"private other task", kind="render_revision", run_id="other"
    )
    record = client.get(f"/api/workbench/runs/{run_id}/validation-record").json()
    assert len(record["render_revisions"]) == 1
    url = record["render_revisions"][0]["download"]
    response = client.get(url)
    assert response.content == original
    assert response.headers["content-disposition"].endswith('.xlsx"')
    assert client.get(f"/api/workbench/runs/{run_id}/revisions/{unrelated}").status_code == 404
    with state.db.session() as db:
        db.get(ArtifactRow, revision).blob = b"corrupted"
    assert client.get(url).status_code == 409
