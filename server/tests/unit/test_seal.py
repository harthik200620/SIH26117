import socket
from collections.abc import Iterator

import pytest

from yantra_server.seal import env as seal_env
from yantra_server.seal import socket_guard
from yantra_server.seal.socket_guard import (
    SealViolation,
    install,
    parse_allowlist,
    uninstall,
)


@pytest.fixture
def clean_guard() -> Iterator[None]:
    """A fresh, uninstalled guard before and after each test (no double-wrap recursion)."""
    uninstall()
    yield
    uninstall()


def test_allowlist_parsing() -> None:
    nets = parse_allowlist("127.0.0.0/8, 10.0.0.0/8")
    assert len(nets) == 2
    assert parse_allowlist(None)  # defaults


def test_application_access_key_never_reaches_model_children(monkeypatch) -> None:
    monkeypatch.setenv("YANTRA_SERVER__ADMIN_TOKEN", "synthetic-key")
    seal_env.apply_seal_env_to_current_process()
    import os

    assert os.environ["YANTRA_SERVER__ADMIN_TOKEN"] == "synthetic-key"
    assert "YANTRA_SERVER__ADMIN_TOKEN" not in seal_env.sealed_environment()


def test_guard_blocks_public_connect(clean_guard: None) -> None:
    install(allowlist=["127.0.0.0/8", "::1/128"])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, pytest.raises(SealViolation):
        s.connect(("1.1.1.1", 443))


def test_guard_allows_loopback(clean_guard: None, monkeypatch: pytest.MonkeyPatch) -> None:
    install(allowlist=["127.0.0.0/8"])
    # connect to a definitely-closed loopback port: allowed by the guard, refused by the OS.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", 9))
        except SealViolation:
            pytest.fail("loopback must be allowed by the guard")
        except OSError:
            pass  # connection refused / timeout is fine — the guard let it through


def test_guard_blocks_dns(clean_guard: None) -> None:
    install(allowlist=["127.0.0.0/8"])
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("example.com", 443)
    # numeric loopback still resolves
    assert socket.getaddrinfo("127.0.0.1", 80)


def test_guard_reporter_called(clean_guard: None) -> None:
    events: list[dict] = []
    install(allowlist=["127.0.0.0/8"], reporter=events.append)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, pytest.raises(SealViolation):
        s.connect(("8.8.8.8", 53))
    assert events and events[0]["kind"] == "blocked_connect"
    assert events[0]["dest"] == "8.8.8.8"
    assert events[0]["stack"]


def test_self_test(clean_guard: None) -> None:
    install(allowlist=["127.0.0.0/8"])
    result = socket_guard.self_test()
    assert result["connect_blocked"] and result["dns_blocked"]


def test_sealed_environment_locks_and_scrubs(monkeypatch: pytest.MonkeyPatch) -> None:
    base = {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "secret",
        "HTTPS_PROXY": "http://proxy:8080",
        "HF_TOKEN": "hf_xxx",
    }
    env = seal_env.sealed_environment(base, allowlist=["127.0.0.0/8"])
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["VLLM_NO_USAGE_STATS"] == "1"
    assert "OPENAI_API_KEY" not in env
    assert "HTTPS_PROXY" not in env
    assert "HF_TOKEN" not in env
    assert env["YANTRA_SEAL_ALLOWLIST"] == "127.0.0.0/8"
    assert "sitecustomize" not in env["PYTHONPATH"]  # dir on path, not the file
    assert env["PYTHONPATH"].endswith("server") or "server" in env["PYTHONPATH"]


def test_assert_environment_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in seal_env.SEAL_ENV.items():
        if name != "YANTRA_SEALED":
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("YANTRA_SEALED", "1")
    for scrub in [*seal_env.UNSET_EXACT]:
        monkeypatch.delenv(scrub, raising=False)
    for name in list(__import__("os").environ):
        if name.startswith(seal_env.UNSET_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    assert seal_env.assert_environment_locked() == []
    monkeypatch.setenv("OPENAI_API_KEY", "leak")
    assert any("OPENAI_API_KEY" in p for p in seal_env.assert_environment_locked())


def test_unsealed_skips_assertion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YANTRA_SEALED", "0")
    assert seal_env.assert_environment_locked() == []
