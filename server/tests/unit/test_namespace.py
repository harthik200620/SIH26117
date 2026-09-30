from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yantra_server.app import create_app
from yantra_server.config import load_config
from yantra_server.seal.namespace import isolation_evidence, namespace_command, reject_special_files


def test_unsupported_host_does_not_attempt_native_connections(monkeypatch) -> None:
    monkeypatch.setattr("yantra_server.seal.namespace.platform.system", lambda: "Windows")

    def fail(*args, **kwargs):
        raise AssertionError("No native probe should run outside the Linux boundary")

    monkeypatch.setattr("yantra_server.seal.namespace.ctypes.CDLL", fail)
    assert not isolation_evidence()["verified"]


def test_strict_startup_refuses_missing_namespace(monkeypatch) -> None:
    monkeypatch.setattr(
        "yantra_server.seal.namespace.check_with_child", lambda: {"verified": False}
    )
    app = create_app(load_config(cli_overrides={"server": {"require_namespace": True}}))
    with pytest.raises(RuntimeError, match="isolation could not be verified"), TestClient(app):
        pass


def test_namespace_mounts_are_explicit(tmp_path: Path) -> None:
    code, work, data = [tmp_path / name for name in ("code", "work", "data")]
    command = namespace_command(
        ["python", "app.py"], readonly=[code], writable=[work, data], cwd=code
    )
    assert "--unshare-all" in command and "--new-session" in command
    assert "--clearenv" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--") + 1 :] == ["python", "app.py"]
    # No whole-host bind, host runtime socket tree or inherited home mount.
    for index, arg in enumerate(command):
        if arg in ("--bind", "--ro-bind"):
            assert command[index + 1] not in ("/", "/home", "/run", "/tmp")
    assert command[command.index("--tmpfs") + 1] == "/tmp"


def test_mount_scan_accepts_regular_files(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("synthetic", encoding="utf-8")
    reject_special_files(tmp_path)
