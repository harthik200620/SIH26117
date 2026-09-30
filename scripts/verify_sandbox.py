"""Run synthetic execution/isolation probes inside the selected installed sandbox.

This checks behavior, not packet-level egress or a complete sandbox security audit.
It does not install a runtime, pull an image, or read confidential documents.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from yantra_server.sandbox.base import SandboxLimits  # noqa: E402
from yantra_server.sandbox.bwrap import BwrapSandbox  # noqa: E402
from yantra_server.sandbox.docker import DockerSandbox  # noqa: E402

PROBE = r"""
import json, pathlib, socket
checks = {}
checks["python_calculation"] = abs(1000 * 9.81 * (36 / 3600) * 22 / 1000 - 2.1582) < 1e-10
pathlib.Path("result.txt").write_text("isolated calculation complete", encoding="utf-8")
checks["workspace_write"] = pathlib.Path("result.txt").is_file()
checks["outside_file_hidden"] = not pathlib.Path(OUTSIDE_PATH).exists()
for label, family, address in [
    ("public_ipv4_connection_denied", socket.AF_INET, ("1.1.1.1", 443)),
    ("public_ipv6_connection_denied", socket.AF_INET6, ("2606:4700:4700::1111", 443)),
    ("metadata_connection_denied", socket.AF_INET, ("169.254.169.254", 80)),
]:
    try:
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(address)
        checks[label] = False
    except OSError:
        checks[label] = True
try:
    socket.getaddrinfo("example.com", 443)
    checks["public_dns_unavailable"] = False
except OSError:
    checks["public_dns_unavailable"] = True
print(json.dumps(checks))
"""


async def verify(backend: str) -> dict:
    cls = DockerSandbox if backend == "docker" else BwrapSandbox
    if not cls.available():
        return {
            "passed": False,
            "backend": backend,
            "error": "Backend is not installed; no fallback was used",
        }
    with tempfile.TemporaryDirectory(prefix="yantra-isolation-") as directory:
        parent = Path(directory)
        outside = parent / "outside-sentinel.txt"
        outside.write_text("synthetic boundary sentinel", encoding="utf-8")
        workspace = parent / "work"
        workspace.mkdir()
        # Non-root container users need to write this disposable probe directory.
        workspace.chmod(0o777)
        probe = workspace / "probe.py"
        probe.write_text("OUTSIDE_PATH = " + repr(str(outside)) + "\n" + PROBE, encoding="utf-8")
        sandbox = cls(workspace=workspace, limits=SandboxLimits(timeout_s=30))
        result = await sandbox.run([sys.executable, str(probe)], cwd=workspace, timeout_s=30)
        try:
            checks = json.loads(result.stdout.strip())
        except (ValueError, TypeError):
            checks = {}
        return {
            "passed": result.exit_code == 0
            and bool(checks)
            and all(v is True for v in checks.values()),
            "backend": backend,
            "checks": checks,
            "exit_code": result.exit_code,
            "elapsed_s": result.wall_s,
            "errors": result.stderr[-2000:],
            "scope": "Synthetic behavior probes only. Denied connections do not prove OS policy enforcement or zero packets; perform native policy review and packet capture too.",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["docker", "bwrap"], required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = asyncio.run(verify(args.backend))
    except (OSError, RuntimeError) as exc:
        report = {"passed": False, "backend": args.backend, "error": str(exc)}
    text = json.dumps(report, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
