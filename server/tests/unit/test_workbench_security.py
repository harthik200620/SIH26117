"""Adversarial boundary checks for the portable workbench."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yantra_server.security import (
    LocalBoundaryMiddleware,
    issue_session,
    local_endpoint,
    valid_session,
    workspace_path,
)
from yantra_server.tools.builtin.calculator import calculate


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com",
        "http://10.0.0.1:8000",
        "http://127.0.0.1.evil.test",
        "http://user:password@127.0.0.1",
        "http://[::ffff:8.8.8.8]",
    ],
)
def test_inference_rejects_nonlocal_urls(url: str) -> None:
    with pytest.raises(ValueError):
        local_endpoint(url)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8100", "http://localhost:8100", "http://[::1]:8100"]
)
def test_inference_allows_loopback(url: str) -> None:
    assert local_endpoint(url) == url


def test_workspace_boundaries(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    assert workspace_path(str(root), [root]) == root.resolve()
    for path in [str(tmp_path), str(root / "missing"), "//server/share", "\\\\server\\share"]:
        with pytest.raises(ValueError):
            workspace_path(path, [root])


def test_cookie_integrity_and_rotation() -> None:
    signed = issue_session("correct")
    assert valid_session("yantra_session=" + signed, "correct")
    assert not valid_session("yantra_session=" + signed, "rotated")
    assert not valid_session("yantra_session=" + signed + "x", "correct")
    assert not valid_session("yantra_session=0.fake", "correct")
    assert not valid_session("unrelated=true", "correct")


def test_origin_host_and_authentication_boundary() -> None:
    app = FastAPI()
    app.add_middleware(LocalBoundaryMiddleware, token="secret")

    @app.get("/api/test")
    def endpoint() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/api/test").status_code == 403
        trusted = {"x-yantra-token": "secret"}
        response = client.get("/api/test", headers=trusted)
        assert response.status_code == 200
        assert "connect-src 'self'" in response.headers["content-security-policy"]
        for headers in [
            {"origin": "https://evil.test"},
            {"host": "evil.test"},
            {"host": "[invalid-ipv6"},
            {"sec-fetch-site": "cross-site"},
        ]:
            assert client.get("/api/test", headers=trusted | headers).status_code == 403
        client.cookies.set("yantra_session", issue_session("secret"))
        assert client.get("/api/test").status_code == 200


def test_calculation_with_explicit_unit_conversion() -> None:
    variables = {"rho": 1000.0, "g": 9.81, "flow": 36.0, "head": 22.0, "efficiency": 0.70}
    assert calculate("rho * g * (flow/3600) * head / 1000", variables) == pytest.approx(2.1582)
    assert calculate(
        "rho * g * (flow/3600) * head / (1000*efficiency)", variables
    ) == pytest.approx(3.083142857142857)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo bad')",
        "(1).__class__",
        "open('secret')",
        "[1]*100000000",
        "2**1000000",
        "float('inf')",
        "1/0",
        "(-1)**0.5",
    ],
)
def test_calculator_rejects_execution_and_unbounded_results(expression: str) -> None:
    with pytest.raises((ValueError, ZeroDivisionError, OverflowError)):
        calculate(expression, {})


def test_quantity_arithmetic_checks_units_and_converts_flow() -> None:
    from yantra_server.tools.builtin.quantities import Quantity, evaluate_quantity

    quantities = {
        "rho": Quantity(value=1000, unit="kg/m^3"),
        "g": Quantity(value=9.81, unit="m/s^2"),
        "q": Quantity(value=36, unit="m^3/h"),
        "h": Quantity(value=22, unit="m"),
        "eta": Quantity(value=70, unit="%"),
    }
    assert evaluate_quantity("rho*g*q*h", quantities, "kW") == pytest.approx(2.1582)
    assert evaluate_quantity("rho*g*q*h/eta", quantities, "kW") == pytest.approx(3.083142857)
    with pytest.raises(ValueError, match="dimensions"):
        evaluate_quantity("rho*g*q", quantities, "kW")
    with pytest.raises(ValueError, match="different dimensions"):
        evaluate_quantity("q+h", quantities, "m")
    with pytest.raises(ValueError):
        evaluate_quantity("__import__('os')", quantities, "1")
