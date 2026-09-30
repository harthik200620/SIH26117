"""Layer 5 — `yantra seal verify|demo|keygen` and the signed Seal Certificate (SPEC §14.5)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yantra_server.seal.env import assert_environment_locked
from yantra_server.seal.socket_guard import install as install_guard
from yantra_server.seal.socket_guard import self_test as guard_self_test

if TYPE_CHECKING:
    from yantra_server.state import AppState


@dataclass
class CheckOutcome:
    name: str
    passed: bool
    detail: str = ""
    skipped: bool = False


@dataclass
class SealReport:
    outcomes: list[CheckOutcome] = field(default_factory=list)
    certificate: dict[str, Any] | None = None
    signature: str | None = None

    def ok(self) -> bool:
        return all(o.passed or o.skipped for o in self.outcomes)

    def add(self, name: str, passed: bool, detail: str = "", skipped: bool = False) -> None:
        self.outcomes.append(CheckOutcome(name, passed, detail, skipped))


PY_NET_PROBE = (
    "import socket, sys\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
    "    print('LEAK'); sys.exit(1)\n"
    "except OSError:\n"
    "    print('SEALED'); sys.exit(0)\n"
)

NODE_NET_PROBE = (
    "const net = require('net');"
    "const s = net.connect({host: '1.1.1.1', port: 443, timeout: 3000});"
    "s.on('connect', () => { console.log('LEAK'); process.exit(1); });"
    "s.on('error', () => { console.log('SEALED'); process.exit(0); });"
    "s.on('timeout', () => { console.log('SEALED'); process.exit(0); });"
)


async def run_verification(state: AppState) -> SealReport:
    report = SealReport()
    config = state.config
    from yantra_server.seal.namespace import isolation_evidence

    namespace = isolation_evidence()
    report.add("os_network_namespace", bool(namespace["verified"]), json.dumps(namespace))

    # 1. environment lock
    problems = assert_environment_locked()
    if not config.sealed():
        report.add("env_locked", False, "YANTRA_SEALED=0 (dev mode)", skipped=False)
    else:
        report.add("env_locked", not problems, "; ".join(problems) or "all variables locked")

    # 2. in-process socket guard self-test
    install_guard(allowlist=config.seal.allowlist)
    guard = guard_self_test()
    report.add(
        "socket_guard_python",
        bool(guard.get("connect_blocked")) and bool(guard.get("dns_blocked")),
        json.dumps(guard),
    )

    # 3. node guard self-test (child process with the preload)
    node = shutil.which("node")
    preload = state.loaded.assets_dir / "tui" / "preload" / "seal.cjs"
    if node is None or not preload.is_file():
        report.add("socket_guard_node", True, "node or preload missing", skipped=True)
    else:
        # `--require` as a direct argv (not NODE_OPTIONS) so paths with spaces work in the
        # self-test; production installs are space-free (/opt/yantra) so NODE_OPTIONS is fine.
        env = _sealed_child_env(state)
        env.pop("NODE_OPTIONS", None)
        result = await asyncio.to_thread(
            subprocess.run,
            [node, "--require", str(preload), "-e", NODE_NET_PROBE],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        report.add(
            "socket_guard_node",
            "SEALED" in result.stdout,
            (result.stdout + result.stderr).strip()[:200],
        )

    # 4. compose networks declared internal
    compose = state.loaded.assets_dir / "docker-compose.yml"
    if compose.is_file():
        text = compose.read_text(encoding="utf-8")
        report.add("compose_internal", "internal: true" in text, "docker-compose.yml networks")
    else:
        report.add("compose_internal", True, "no compose file", skipped=True)

    # 5. sandbox self-test (needs an isolating backend)
    report.outcomes.append(await _sandbox_probe(state))

    # 6. agent smoke inside the sealed process (scripted mock run)
    report.outcomes.append(await _agent_smoke(state))

    # 7. bundle manifest
    manifest = state.loaded.assets_dir / "bundle" / "MANIFEST.sha256"
    if manifest.is_file():
        bad = _verify_manifest(manifest)
        report.add("bundle_manifest", not bad, bad or "all files match")
    else:
        report.add(
            "bundle_manifest", True, "no offline bundle present (source checkout)", skipped=True
        )

    report.certificate = _build_certificate(state, report)
    report.signature = _sign_certificate(state, report.certificate)
    state.audit.append(
        "system",
        "seal.verify",
        {"ok": report.ok(), "outcomes": [o.name for o in report.outcomes if not o.passed]},
    )
    return report


def _sealed_child_env(state: AppState) -> dict[str, str]:
    from yantra_server.seal.env import sealed_environment

    env = sealed_environment(allowlist=state.config.seal.allowlist)
    env["YANTRA_SEAL_EVENTS_FILE"] = str(state.config.paths.data_dir / "seal_events.jsonl")
    return env


async def _sandbox_probe(state: AppState) -> CheckOutcome:
    from yantra_server.sandbox import SandboxError, select_sandbox
    from yantra_server.sandbox.bwrap import BwrapSandbox
    from yantra_server.sandbox.docker import DockerSandbox

    if not (BwrapSandbox.available() or DockerSandbox.available()):
        return CheckOutcome(
            "sandbox_no_network",
            True,
            "no isolating sandbox on this host (bwrap/docker absent); process guard covers "
            "child python via sitecustomize",
            skipped=True,
        )
    import tempfile

    with tempfile.TemporaryDirectory(prefix="yantra-seal-") as tmp:
        try:
            sandbox = select_sandbox(state.config.sandbox, Path(tmp), sealed=True)
        except SandboxError as exc:
            return CheckOutcome("sandbox_no_network", False, str(exc))
        result = await sandbox.run([sys.executable, "-c", PY_NET_PROBE], timeout_s=30)
        return CheckOutcome(
            "sandbox_no_network",
            "SEALED" in result.stdout,
            f"{sandbox.name}: {(result.stdout + result.stderr).strip()[:200]}",
        )


async def _agent_smoke(state: AppState) -> CheckOutcome:
    """A short scripted agent run must complete with the network guard active."""
    from yantra_server.evals.seal_smoke import run_seal_smoke

    try:
        outcome: CheckOutcome = await run_seal_smoke(state)
        return outcome
    except Exception as exc:
        return CheckOutcome("agent_smoke", False, f"{type(exc).__name__}: {exc}")


def _verify_manifest(manifest: Path) -> str | None:
    root = manifest.parent
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            digest, name = line.split(maxsplit=1)
        except ValueError:
            return f"malformed manifest line: {line[:60]}"
        target = root / name.strip().lstrip("*")
        if not target.is_file():
            return f"missing file: {name}"
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != digest:
            return f"hash mismatch: {name}"
    return None


def _build_certificate(state: AppState, report: SealReport) -> dict[str, Any]:
    git_sha = "unknown"
    with contextlib.suppress(Exception):
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=state.loaded.assets_dir,
            timeout=5,
        )
        if result.returncode == 0:
            git_sha = result.stdout.strip()
    sbom = state.loaded.assets_dir / "docs" / "SBOM.json"
    sbom_hash = hashlib.sha256(sbom.read_bytes()).hexdigest() if sbom.is_file() else "not-built"
    head = state.audit.head()
    return {
        "product": "YANTRA Seal Certificate",
        "issued_at": datetime.now(UTC).isoformat(),
        "git_sha": git_sha,
        "profile": state.config.profile,
        "sbom_sha256": sbom_hash,
        "models": [
            {"id": m.id, "path": m.path, "params_b": m.params_b} for m in state.registry.all()
        ],
        "audit_head": head.hash if head else None,
        "audit_entries": state.audit.count(),
        "allowlist": state.config.seal.allowlist,
        "checks": [
            {"name": o.name, "passed": o.passed, "skipped": o.skipped, "detail": o.detail[:200]}
            for o in report.outcomes
        ],
        "verdict": "SEALED" if report.ok() and state.config.sealed() else "NOT SEALED",
    }


# ------------------------------------------------------------------ signing


def key_paths(data_dir: Path) -> tuple[Path, Path]:
    return data_dir / "seal_key.pem", data_dir / "seal_key.pub"


def keygen(data_dir: Path) -> tuple[Path, Path]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    private_path, public_path = key_paths(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return private_path, public_path


def _sign_certificate(state: AppState, certificate: dict[str, Any]) -> str | None:
    private_path, _ = key_paths(state.config.paths.data_dir)
    if not private_path.is_file():
        return None
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(private_path.read_bytes(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    payload = json.dumps(certificate, sort_keys=True, separators=(",", ":")).encode()
    return key.sign(payload).hex()


def format_report(report: SealReport) -> str:
    lines = ["YANTRA seal verification", "=" * 40]
    for outcome in report.outcomes:
        mark = "SKIP" if outcome.skipped else ("PASS" if outcome.passed else "FAIL")
        lines.append(f"[{mark:<4}] {outcome.name:<22} {outcome.detail[:90]}")
    cert = report.certificate or {}
    lines.append("-" * 40)
    lines.append(f"verdict: {cert.get('verdict')}")
    lines.append(
        f"git: {cert.get('git_sha', '')[:12]}  audit head: {str(cert.get('audit_head'))[:16]}"
    )
    lines.append(
        "signature: "
        + (
            report.signature[:32] + "…"
            if report.signature
            else "(unsigned — run `yantra seal keygen`)"
        )
    )
    return "\n".join(lines)
