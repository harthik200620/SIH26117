import pytest

from yantra_server.instance import InstanceLock


def test_single_coordinator_and_release(tmp_path) -> None:
    first, second = InstanceLock(tmp_path), InstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="Another BlackBox coordinator"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
    second.release()


def test_active_server_prevents_second_initialization(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from yantra_server.app import create_app
    from yantra_server.config import load_config

    loaded = load_config()
    app = create_app(loaded)
    with TestClient(app):

        def unexpected_build(*args, **kwargs):
            raise AssertionError("A second server must not initialize or migrate active state")

        monkeypatch.setattr("yantra_server.app.build_state", unexpected_build)
        with pytest.raises(RuntimeError, match="Another BlackBox coordinator"):
            create_app(loaded)
