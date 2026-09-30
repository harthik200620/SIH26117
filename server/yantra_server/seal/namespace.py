"""Linux offline boundary. No dependencies: runs before importing the application."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import platform
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any


def isolation_evidence() -> dict[str, Any]:
    """Inspect kernel state and exercise libc sockets, bypassing Python monkey patches.

    This is a process-local observation, not a remote attestation or kernel audit.
    """
    if platform.system() != "Linux":
        return {"verified": False, "reason": "Linux network namespace required"}
    interfaces = [name for _, name in socket.if_nameindex()]
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    caps = int(status.get("CapEff", "1").strip(), 16)
    no_new_privs = status.get("NoNewPrivs", "0").strip() == "1"
    routes = Path("/proc/net/route").read_text().splitlines()[1:]
    routes = [line for line in routes if line.split()[0] != "lo"]
    if interfaces != ["lo"] or routes or caps != 0 or not no_new_privs:
        return {
            "verified": False,
            "reason": "Kernel namespace preconditions not met; no network probes attempted",
            "interfaces": interfaces,
            "effective_capabilities": caps,
            "no_new_privileges": no_new_privs,
        }
    libc = ctypes.CDLL(None, use_errno=True)
    libc.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    libc.socket.restype = ctypes.c_int
    libc.connect.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    libc.connect.restype = ctypes.c_int
    libc.close.argtypes = [ctypes.c_int]
    probes = []
    for family, address, label in [
        (socket.AF_INET, "1.1.1.1", "public_ipv4"),
        (socket.AF_INET, "169.254.169.254", "cloud_metadata"),
        (socket.AF_INET6, "2606:4700:4700::1111", "public_ipv6"),
    ]:
        for kind, suffix in [(socket.SOCK_STREAM, "tcp"), (socket.SOCK_DGRAM, "udp_dns")]:
            nonblocking = getattr(socket, "SOCK_NONBLOCK", 0)
            if not nonblocking:
                raise RuntimeError("Nonblocking native socket probes are unsupported")
            fd = libc.socket(family, kind | int(nonblocking), 0)
            if fd < 0:
                probes.append(
                    {"name": f"{label}_{suffix}", "blocked": False, "errno": ctypes.get_errno()}
                )
                continue
            try:
                # sockaddr_in / sockaddr_in6; family is host order, port network order.
                address_bytes = int(family).to_bytes(2, sys.byteorder) + (
                    53 if kind == socket.SOCK_DGRAM else 443
                ).to_bytes(2, "big")
                if family == socket.AF_INET:
                    address_bytes += socket.inet_pton(family, address) + bytes(8)
                else:
                    address_bytes += bytes(4) + socket.inet_pton(family, address) + bytes(4)
                packed = ctypes.create_string_buffer(address_bytes)
                result = libc.connect(fd, packed, len(address_bytes))
                error = ctypes.get_errno()
                # Refused/timeout/in-progress are NOT proof of isolation.
                blocked = result == -1 and error in (errno.ENETUNREACH, errno.EACCES, errno.EPERM)
                probes.append({"name": f"{label}_{suffix}", "blocked": blocked, "errno": error})
            finally:
                libc.close(fd)
    return {
        "verified": interfaces == ["lo"]
        and not routes
        and caps == 0
        and no_new_privs
        and all(p["blocked"] for p in probes),
        "method": "linux_network_namespace_libc_probes",
        "kernel": platform.release(),
        "interfaces": interfaces,
        "network_namespace": os.readlink("/proc/self/ns/net"),
        "effective_capabilities": caps,
        "no_new_privileges": no_new_privs,
        "non_loopback_routes": routes,
        "probes": probes,
        "scope": "current namespace and inheriting children; trusted host/kernel/browser",
        "packet_capture": "not performed",
    }


def reject_special_files(root: Path) -> None:
    """Do not expose existing host IPC sockets/devices through approved mounts."""
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif not entry.is_file(follow_symlinks=False):
                    raise ValueError(f"Special file cannot be mounted into strict runtime: {entry.path}")


def namespace_command(
    argv: list[str],
    *,
    readonly: list[Path],
    writable: list[Path],
    cwd: Path,
    environment: dict[str, str] | None = None,
) -> list[str]:
    """Explicit mount allowlist: no host home, /run, /tmp, socket or device mounts."""
    cmd = [
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
    ]
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp/home",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        **(environment or {}),
    }
    for key, value in env.items():
        cmd += ["--setenv", key, value]
    for root in ("/usr", "/lib", "/lib64", "/bin", "/sbin"):
        if Path(root).exists():
            cmd += ["--ro-bind", root, root]
    for path in readonly:
        cmd += ["--ro-bind", str(path), str(path)]
    cmd += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/tmp/home"]
    for path in writable:
        cmd += ["--bind", str(path), str(path)]
    cmd += ["--chdir", str(cwd), "--", *argv]
    return cmd


def check_with_child() -> dict[str, Any]:
    evidence = isolation_evidence()
    # -I ignores sitecustomize/PYTHONPATH. Loading this dependency-free file cannot install guards.
    child = subprocess.run(
        [sys.executable, "-I", str(Path(__file__).resolve())],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    try:
        child_evidence = json.loads(child.stdout)
    except ValueError:
        child_evidence = {"verified": False, "error": child.stderr[-500:]}
    evidence["child"] = child_evidence
    evidence["verified"] = bool(
        evidence["verified"] and child.returncode == 0 and child_evidence.get("verified")
    )
    return evidence


if __name__ == "__main__":
    result = isolation_evidence()
    print(json.dumps(result))
    raise SystemExit(0 if result["verified"] else 1)
