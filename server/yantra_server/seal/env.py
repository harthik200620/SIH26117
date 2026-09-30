"""Layer 2 — runtime environment lock (SPEC §14.2).

`sealed_environment()` is exported into every process the supervisor starts and asserted at
server startup. Each variable and the library it silences is documented in docs/SEAL.md.
"""

from __future__ import annotations

import os
from pathlib import Path

# Variables that force offline/no-telemetry behaviour in bundled libraries.
SEAL_ENV: dict[str, str] = {
    "HF_HUB_OFFLINE": "1",  # huggingface_hub: never hit the hub
    "TRANSFORMERS_OFFLINE": "1",  # transformers: local files only
    "HF_DATASETS_OFFLINE": "1",  # datasets: local only
    "HF_HUB_DISABLE_TELEMETRY": "1",  # huggingface_hub telemetry
    "DISABLE_TELEMETRY": "1",  # generic (gradio and others honour it)
    "DO_NOT_TRACK": "1",  # console DNT convention
    "VLLM_NO_USAGE_STATS": "1",  # vLLM usage stats
    "VLLM_DO_NOT_TRACK": "1",  # vLLM tracking
    "QDRANT__TELEMETRY_DISABLED": "true",  # qdrant server telemetry
    "TOKENIZERS_PARALLELISM": "false",  # tokenizers fork warning/threads
    "PIP_NO_INDEX": "1",  # pip can never reach PyPI
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",  # pip self-update check
    "UV_OFFLINE": "1",  # uv resolver offline
    "NO_PROXY": "*",  # never route via a proxy
    "npm_config_update_notifier": "false",  # npm update pings
    "YANTRA_SEALED": "1",
}

# Variables that must not leak into sealed child processes (credentials, exporters, proxies).
UNSET_PREFIXES = ("OPENAI_", "AWS_", "AZURE_", "GOOGLE_")
# Any hosted-AI or SaaS credential, whatever the vendor prefix: *_API_KEY / *_API_TOKEN /
# *_ACCESS_TOKEN / *_SECRET_KEY are scrubbed generically so new vendors need no code change.
UNSET_SUFFIXES = ("_API_KEY", "_API_TOKEN", "_ACCESS_TOKEN", "_SECRET_KEY")
UNSET_EXACT = {
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "SENTRY_DSN",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
}


def sealed_environment(
    base: dict[str, str] | None = None,
    *,
    allowlist: list[str] | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment for a sealed child process: base env, scrubbed, with the lock applied."""
    env = dict(base if base is not None else os.environ)
    env.pop("YANTRA_SERVER__ADMIN_TOKEN", None)
    for name in list(env):
        if name in UNSET_EXACT or name.startswith(UNSET_PREFIXES) or name.endswith(UNSET_SUFFIXES):
            del env[name]
    env.update(SEAL_ENV)
    if allowlist:
        env["YANTRA_SEAL_ALLOWLIST"] = ",".join(allowlist)
    server_dir = Path(__file__).resolve().parents[2]  # server/ (holds sitecustomize.py)
    sitecustomize_dir = str(server_dir)
    existing = env.get("PYTHONPATH", "")
    if sitecustomize_dir not in existing.split(os.pathsep):
        env["PYTHONPATH"] = (
            f"{sitecustomize_dir}{os.pathsep}{existing}" if existing else sitecustomize_dir
        )
    # Node children (TUI, MCP servers) load the socket guard via --require.
    preload = server_dir.parent / "tui" / "preload" / "seal.cjs"
    if preload.is_file():
        node_options = env.get("NODE_OPTIONS", "")
        require_flag = f'--require "{preload}"'  # quoted: the path may contain spaces
        if str(preload) not in node_options:
            env["NODE_OPTIONS"] = f"{node_options} {require_flag}".strip()
    if extra:
        env.update(extra)
    return env


def assert_environment_locked() -> list[str]:
    """Return the list of missing/incorrect seal variables in this process (empty = locked)."""
    problems: list[str] = []
    if os.environ.get("YANTRA_SEALED", "1") == "0":
        return []  # explicitly unsealed dev mode: assertion is moot, callers show the banner
    for name, want in SEAL_ENV.items():
        if name == "YANTRA_SEALED":
            continue
        if os.environ.get(name) != want:
            problems.append(f"{name}!={want}")
    for name in UNSET_EXACT:
        if name in os.environ:
            problems.append(f"{name} set")
    for name in list(os.environ):
        if name.startswith(UNSET_PREFIXES):
            problems.append(f"{name} set")
    return problems


def apply_seal_env_to_current_process() -> None:
    """Apply the lock in-place (used by `yantra serve` before ML libraries are imported)."""
    for name in list(os.environ):
        if name in UNSET_EXACT or name.startswith(UNSET_PREFIXES):
            del os.environ[name]
    os.environ.update({k: v for k, v in SEAL_ENV.items() if k != "YANTRA_SEALED"})
