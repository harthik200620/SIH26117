"""Local application boundaries. These checks complement, never replace, an OS firewall."""

from __future__ import annotations

import hmac
import ipaddress
import time
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def issue_session(token: str) -> str:
    expiry = str(int(time.time()) + 8 * 3600)
    return expiry + "." + hmac.new(token.encode(), expiry.encode(), "sha256").hexdigest()


def valid_session(cookie_header: str, token: str) -> bool:
    try:
        cookies = SimpleCookie()
        cookies.load(cookie_header)
        value = cookies["yantra_session"].value
        expiry, signature = value.split(".", 1)
        expected = hmac.new(token.encode(), expiry.encode(), "sha256").hexdigest()
        return int(time.time()) <= int(expiry) <= int(
            time.time()
        ) + 8 * 3600 and hmac.compare_digest(signature, expected)
    except (CookieError, KeyError, ValueError):
        return False


def local_endpoint(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Inference endpoints must be plain HTTP on loopback, without credentials")
    try:
        local = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        local = parsed.hostname == "localhost"
    if not local:
        raise ValueError("Only loopback inference endpoints are permitted")
    return url


def workspace_path(raw: str, roots: list[Path]) -> Path:
    if raw.startswith(("\\\\", "//")):
        raise ValueError("Network shares are disabled in local mode")
    path = Path(raw).expanduser().resolve()
    if roots and not any(path.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("Choose a folder inside a configured workspace root")
    if not path.is_dir():
        raise ValueError("Workspace folder does not exist")
    return path


class LocalBoundaryMiddleware:
    """Block DNS rebinding, cross-origin browser calls, remote clients and external assets."""

    def __init__(self, app: ASGIApp, token: str | None = None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        host = headers.get("host", "")
        try:
            hostname = urlsplit("http://" + host).hostname
        except ValueError:
            hostname = None
        client = (scope.get("client") or ("",))[0]
        origin = headers.get("origin")
        error = None
        allowed_hosts = {"127.0.0.1", "localhost", "::1"}
        if client == "testclient":
            allowed_hosts.add("testserver")
        if hostname not in allowed_hosts:
            error = "Untrusted host"
        if client not in {"127.0.0.1", "::1", "testclient", ""}:
            error = "Local clients only"
        if origin and origin not in {"http://" + host, "https://" + host}:
            error = "Cross-origin access denied"
        if headers.get("sec-fetch-site") == "cross-site":
            error = "Cross-site access denied"
        if (
            self.token
            and scope["path"].startswith(("/api/", "/rpc"))
            and not hmac.compare_digest(
                headers.get("x-yantra-token", "").encode(), self.token.encode()
            )
            and not valid_session(headers.get("cookie", ""), self.token)
        ):
            error = "Authentication required"
        if error:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4403})
            else:
                await JSONResponse({"error": error}, status_code=403)(scope, receive, send)
            return

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(
                    [
                        (
                            b"content-security-policy",
                            b"default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                        ),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"cache-control", b"no-store"),
                    ]
                )
            await send(message)

        await self.app(scope, receive, secure_send)
