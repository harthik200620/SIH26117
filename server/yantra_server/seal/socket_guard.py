"""Layer 3 — process-level socket guard (SPEC §14.3).

Monkey-patches socket connect/sendto/getaddrinfo so any destination outside the allowlist
raises SealViolation (an OSError) and is reported; DNS for non-allowlisted names returns
EAI_NONAME. Installed explicitly by `yantra serve` and, via server/sitecustomize.py on
PYTHONPATH, by every sealed child process. Disabled only by YANTRA_SEALED=0.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

DEFAULT_ALLOWLIST = ["127.0.0.0/8", "::1/128"]
ALLOWED_NAMES_DEFAULT = {"localhost", "localhost.localdomain", "ip6-localhost"}


class SealViolation(OSError):
    """An outbound connection attempt outside the seal allowlist."""


class SealDNSViolation(socket.gaierror):
    """DNS denial specifically caused by the application guard."""


class _GuardState:
    def __init__(self) -> None:
        self.installed = False
        self.networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self.allowed_names: set[str] = set(ALLOWED_NAMES_DEFAULT)
        self.originals: dict[str, Any] = {}
        self.reporter: Any = None  # callable(event: dict) -> None


_state = _GuardState()


def parse_allowlist(raw: str | None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    entries = [e.strip() for e in (raw or "").split(",") if e.strip()] or DEFAULT_ALLOWLIST
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return networks


def _ip_allowed(host: str) -> bool | None:
    """True/False for numeric addresses; None when host is a name."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in _state.networks)


def _report(kind: str, dest: str, port: int | None) -> None:
    event = {
        "ts": datetime.now(UTC).isoformat(),
        "process": os.path.basename(sys.argv[0] or "python"),
        "pid": os.getpid(),
        "kind": kind,
        "dest": dest,
        "port": port,
        "stack": [
            f"{f.name} ({os.path.basename(f.filename)}:{f.lineno})"
            for f in traceback.extract_stack(limit=8)[:-3][-3:]
        ],
    }
    reporter = _state.reporter
    if reporter is not None:
        try:
            reporter(event)
            return
        except Exception:
            pass
    events_file = os.environ.get("YANTRA_SEAL_EVENTS_FILE")
    if events_file:
        try:
            with open(events_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
            return
        except OSError:
            pass
    import contextlib

    with contextlib.suppress(Exception):
        sys.stderr.write(f"[yantra-seal] blocked {kind} to {dest}:{port}\n")


def _check_address(address: Any, *, operation: str) -> None:
    if not isinstance(address, tuple) or len(address) < 2:
        return  # AF_UNIX and friends
    host, port = str(address[0]), address[1]
    verdict = _ip_allowed(host)
    if verdict is None:  # a hostname reached connect(): resolve through guarded getaddrinfo
        if host in _state.allowed_names:
            return
        _report(f"blocked_{operation}", host, int(port) if isinstance(port, int) else None)
        raise SealViolation(f"sealed: {operation} to non-allowlisted host {host!r} refused")
    if not verdict:
        _report(f"blocked_{operation}", host, int(port) if isinstance(port, int) else None)
        raise SealViolation(f"sealed: {operation} to {host}:{port} outside the allowlist refused")


def install(
    allowlist: list[str] | None = None,
    allowed_names: set[str] | None = None,
    reporter: Any = None,
) -> None:
    """Install the guard (idempotent)."""
    if reporter is not None:
        _state.reporter = reporter
    if allowlist is not None:
        _state.networks = parse_allowlist(",".join(allowlist))
    elif not _state.networks:
        _state.networks = parse_allowlist(os.environ.get("YANTRA_SEAL_ALLOWLIST"))
    if allowed_names:
        _state.allowed_names |= allowed_names
    _state.allowed_names |= {
        n.strip() for n in os.environ.get("YANTRA_SEAL_ALLOW_NAMES", "").split(",") if n.strip()
    }
    if _state.installed:
        return
    _state.installed = True

    _state.originals["connect"] = socket.socket.connect
    _state.originals["connect_ex"] = socket.socket.connect_ex
    _state.originals["sendto"] = socket.socket.sendto
    _state.originals["getaddrinfo"] = socket.getaddrinfo
    _state.originals["gethostbyname"] = socket.gethostbyname
    _state.originals["gethostbyname_ex"] = socket.gethostbyname_ex

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        _check_address(address, operation="connect")
        return _state.originals["connect"](self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        _check_address(address, operation="connect")
        return _state.originals["connect_ex"](self, address)

    def guarded_sendto(self: socket.socket, *args: Any) -> Any:
        if len(args) >= 2:
            _check_address(args[-1], operation="sendto")
        return _state.originals["sendto"](self, *args)

    def guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        name = str(host) if host is not None else ""
        if name and _ip_allowed(name) is None and name not in _state.allowed_names:
            _report("blocked_dns", name, int(port) if isinstance(port, int) else None)
            raise SealDNSViolation(socket.EAI_NONAME, f"sealed: DNS for {name!r} refused")
        results = _state.originals["getaddrinfo"](host, port, *args, **kwargs)
        for family, _type, _proto, _canon, sockaddr in results:
            if (
                family in (socket.AF_INET, socket.AF_INET6)
                and _ip_allowed(str(sockaddr[0])) is False
            ):
                _report("blocked_dns", f"{name}→{sockaddr[0]}", None)
                raise SealDNSViolation(
                    socket.EAI_NONAME, f"sealed: {name!r} resolves outside the allowlist"
                )
        return results

    socket.socket.connect = guarded_connect  # type: ignore[assignment]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[assignment]
    socket.socket.sendto = guarded_sendto  # type: ignore[assignment]
    socket.getaddrinfo = guarded_getaddrinfo

    def guarded_gethostbyname(host: str) -> str:
        results = guarded_getaddrinfo(host, None, socket.AF_INET)
        return str(results[0][4][0])

    def guarded_gethostbyname_ex(host: str) -> tuple[str, list[str], list[str]]:
        results = guarded_getaddrinfo(host, None, socket.AF_INET)
        return host, [], sorted({str(row[4][0]) for row in results})

    socket.gethostbyname = guarded_gethostbyname
    socket.gethostbyname_ex = guarded_gethostbyname_ex

    try:  # TLS handshake logging (belt and braces; connect is already guarded)
        import ssl

        original_wrap = ssl.SSLContext.wrap_socket
        _state.originals["wrap_socket"] = original_wrap

        def logged_wrap(
            self: ssl.SSLContext, sock: socket.socket, *args: Any, **kwargs: Any
        ) -> Any:
            return original_wrap(self, sock, *args, **kwargs)

        ssl.SSLContext.wrap_socket = logged_wrap  # type: ignore[method-assign]
    except ImportError:
        pass


def uninstall() -> None:
    """Restore the real socket functions (tests; never used in production)."""
    if not _state.installed:
        return
    socket.socket.connect = _state.originals["connect"]  # type: ignore[method-assign]
    socket.socket.connect_ex = _state.originals["connect_ex"]  # type: ignore[method-assign]
    socket.socket.sendto = _state.originals["sendto"]  # type: ignore[method-assign]
    socket.getaddrinfo = _state.originals["getaddrinfo"]
    socket.gethostbyname = _state.originals["gethostbyname"]
    socket.gethostbyname_ex = _state.originals["gethostbyname_ex"]
    if "wrap_socket" in _state.originals:
        import ssl

        ssl.SSLContext.wrap_socket = _state.originals["wrap_socket"]  # type: ignore[method-assign]
    _state.installed = False
    _state.originals.clear()


def install_from_env() -> None:
    if os.environ.get("YANTRA_SEALED", "1") == "0":
        return
    install()


def is_installed() -> bool:
    return _state.installed


def set_reporter(reporter: Any) -> None:
    _state.reporter = reporter


def self_test() -> dict[str, Any]:
    """Prove the guard bites: 1.1.1.1:443 connect and example.com DNS must fail."""
    results: dict[str, Any] = {"installed": _state.installed}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(("1.1.1.1", 443))
        results["connect_blocked"] = False
    except SealViolation:
        results["connect_blocked"] = True
    except OSError as exc:
        results["connect_blocked"] = False
        results["connect_note"] = f"{type(exc).__name__}: {exc}"
    try:
        socket.getaddrinfo("example.com", 443)
        results["dns_blocked"] = False
    except SealDNSViolation:
        results["dns_blocked"] = True
    except OSError as exc:
        results["dns_blocked"] = False
        results["dns_note"] = f"{type(exc).__name__}: {exc}"
    return results
