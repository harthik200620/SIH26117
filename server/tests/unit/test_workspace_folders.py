from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yantra_server.security import LocalBoundaryMiddleware, workspace_path
from yantra_server.workbench import workbench_router
from yantra_server.workspaces import WorkspaceFolders, local_directory


def test_selected_folder_persists_without_granting_parent(tmp_path: Path) -> None:
    root = tmp_path / "original"
    chosen = tmp_path / "project with spaces"
    root.mkdir()
    chosen.mkdir()
    store = WorkspaceFolders(tmp_path / "data", [root])
    assert store.select(str(chosen)) == chosen.resolve()
    store.select(str(chosen))
    restored = WorkspaceFolders(tmp_path / "data", [root])
    assert restored.roots == [root, chosen.resolve()]
    assert workspace_path(str(chosen), restored.roots) == chosen.resolve()
    with pytest.raises(ValueError):
        workspace_path(str(tmp_path), restored.roots)
    assert list(chosen.iterdir()) == []


@pytest.mark.parametrize("raw", ["", "relative/folder", "//server/share", "\\\\server\\share"])
def test_requires_explicit_local_absolute_directory(raw: str) -> None:
    with pytest.raises(ValueError):
        local_directory(raw)


def test_rejects_file_and_missing_folder(tmp_path: Path) -> None:
    file = tmp_path / "file.txt"
    file.write_text("test")
    store = WorkspaceFolders(tmp_path / "data", [])
    for path in [file, tmp_path / "missing"]:
        with pytest.raises((ValueError, OSError)):
            store.select(str(path))
    assert store.roots == []
    assert not store.file.exists()


def test_removed_saved_folder_does_not_grant_access(tmp_path: Path) -> None:
    chosen = tmp_path / "removed"
    chosen.mkdir()
    store = WorkspaceFolders(tmp_path / "data", [])
    store.select(str(chosen))
    chosen.rmdir()
    assert WorkspaceFolders(tmp_path / "data", []).roots == []


def test_api_requires_authentication_and_audits_selection(tmp_path: Path) -> None:
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    roots: list[Path] = []
    audit = Mock()
    state = SimpleNamespace(
        config=SimpleNamespace(
            paths=SimpleNamespace(workspace_roots=roots),
            server=SimpleNamespace(admin_token="test-key"),
        ),
        extras={"workspace_folders": WorkspaceFolders(tmp_path / "data", roots)},
        audit=audit,
    )
    app = FastAPI()
    app.add_middleware(LocalBoundaryMiddleware, token="test-key")
    app.include_router(workbench_router(state))
    with TestClient(app) as client:
        body = {"workspace": str(chosen)}
        assert client.post("/api/workbench/workspace/select", json=body).status_code == 403
        headers = {"x-yantra-token": "test-key"}
        assert (
            client.post(
                "/api/workbench/workspace/select",
                json=body,
                headers=headers | {"origin": "https://example.com"},
            ).status_code
            == 403
        )
        assert roots == []
        response = client.post("/api/workbench/workspace/select", json=body, headers=headers)
        assert response.status_code == 200
        assert response.json() == {
            "workspace": str(chosen.resolve()),
            "roots": [str(chosen.resolve())],
        }
        audit.append.assert_called_once_with(
            "user", "workspace.selected", {"workspace": str(chosen.resolve())}
        )
        assert (
            client.post(
                "/api/workbench/workspace/select",
                json={"workspace": str(chosen / "missing")},
                headers=headers,
            ).status_code
            == 400
        )
        state.config.server.admin_token = None
        assert (
            client.post("/api/workbench/workspace/select", json=body, headers=headers).status_code
            == 403
        )
