"""Portable offline workbench: Windows, Linux, macOS, workstations and private cloud VMs."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))


def configure(args: argparse.Namespace) -> tuple[Path, Path]:
    import yaml

    from yantra_server.gateway.registry import inspect_model_path

    data = args.data_dir.expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True)
    workspace = args.workspace.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    registry_path = data / "registry.local.yaml"
    existing = (
        yaml.safe_load(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else []
    )
    entries = {m["id"]: m for m in existing or []}
    for path in sorted(args.models_dir.glob("*.gguf")):
        if "mmproj" in path.name.lower():
            continue
        manifest = inspect_model_path(path)
        previous = entries.get(manifest.id)
        if previous and Path(previous.get("path", "")).resolve() != path.resolve():
            manifest.id += "-" + hashlib.sha256(path.name.encode()).hexdigest()[:8]
        manifest.serve_context_len = min(args.context, manifest.context_len or args.context)
        if "coder" in path.name.lower():
            manifest.capabilities.append("code")
            manifest.roles = ["coder"]
        elif "embedding" in path.name.lower():
            manifest.capabilities = ["embed"]
            manifest.roles = ["embed"]
            manifest.serve_args = ["--embedding"]
        elif "vl" in path.name.lower():
            continue  # explicit projector configuration required, never guess
        else:
            manifest.roles = ["planner", "executor", "reviewer", "utility", "router", "heavy"]
        if manifest.params_b >= 120:
            print(f"Skipping over-cap model: {manifest.id}")
            continue
        previous_model = entries.get(manifest.id, {})
        if previous_model.get("roles"):
            manifest.roles = previous_model["roles"]
        entries[manifest.id] = {
            **previous_model,
            **manifest.model_dump(mode="json", exclude_defaults=True),
        }
    registry_path.write_text(
        yaml.safe_dump(list(entries.values()), sort_keys=False), encoding="utf-8"
    )
    engines_path = data / "integrated_engines.yaml"
    saved = (
        yaml.safe_load(engines_path.read_text(encoding="utf-8")) if engines_path.exists() else []
    )
    engine_map = {e["id"]: e for e in saved or []}
    roles: dict[str, list[str]] = {}
    for m in sorted(entries.values(), key=lambda m: m.get("params_b", 0), reverse=True):
        if m.get("params_b", 0) >= 120:
            continue
        if not str(m.get("path", "")).lower().endswith(".gguf"):
            continue
        if engine_map.get(m["id"], {}).get("kind", "llamacpp") != "llamacpp":
            continue
        engine_map[m["id"]] = {
            **engine_map.get(m["id"], {}),
            "id": m["id"],
            "kind": "llamacpp",
            "model": m["id"],
            "device": "cpu" if args.gpu_layers == 0 else "auto",
            "ctx": args.context,
            "threads": args.threads,
            "gpu_layers": args.gpu_layers,
            "on_demand": True,
            "mode": "embedding" if "embed" in m.get("roles", []) else "chat",
        }
        for role in m.get("roles", []):
            roles.setdefault(role, []).append(m["id"])
    engines_path.write_text(
        yaml.safe_dump(list(engine_map.values()), sort_keys=False), encoding="utf-8"
    )
    for role, ids in roles.items():
        ids.sort(
            key=lambda identifier: entries[identifier].get("params_b", 0),
            reverse=role in {"planner", "reviewer", "heavy"},
        )
    # Automatic candidate order is separate from explicit operator overrides.
    routing = data / "routing.generated.yaml"
    routing.write_text(yaml.safe_dump({"roles": roles}), encoding="utf-8")
    return data, workspace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT / "workspace")
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".yantra/workbench")
    parser.add_argument("--models-dir", type=Path, default=ROOT / "models/weights")
    parser.add_argument("--port", type=int, default=7331)
    parser.add_argument(
        "--admin-token-file",
        type=Path,
        help="Read the local access key without exposing it in process arguments",
    )
    parser.add_argument(
        "--uds", type=Path, help="Private Unix socket for the strict runtime bridge"
    )
    parser.add_argument(
        "--require-namespace",
        action="store_true",
        help="Refuse startup without verified Linux network namespace isolation",
    )
    parser.add_argument("--context", type=int, default=8192)
    parser.add_argument("--threads", type=int, default=min(8, os.cpu_count() or 4))
    parser.add_argument(
        "--gpu-layers",
        type=int,
        default=0,
        help="0=CPU; -1=full GPU offload; positive=partial offload. Requires a matching llama.cpp runtime.",
    )
    parser.add_argument("--sandbox", choices=["disabled", "docker", "bwrap"], default="disabled")
    parser.add_argument("--retrieval", choices=["lexical", "hybrid"], default="lexical")
    parser.add_argument("--resident-models", type=int, default=1)
    parser.add_argument("--parallel-tasks", type=int, default=1)
    parser.add_argument(
        "--llama-server", type=Path, help="Path to an offline installed llama-server binary"
    )
    args = parser.parse_args()
    if args.admin_token_file is not None:
        token = args.admin_token_file.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            parser.error("Access key must contain at least 32 characters")
        os.environ["YANTRA_SERVER__ADMIN_TOKEN"] = token
    if not 1024 <= args.port <= 65535 or not 2048 <= args.context <= 131072:
        parser.error("Use a port from 1024 to 65535 and context from 2048 to 131072")
    if args.threads < 1 or args.resident_models < 1 or args.parallel_tasks < 1:
        parser.error("Threads, resident models and parallel tasks must be positive")
    for path in (args.workspace, args.models_dir, args.data_dir):
        if str(path).startswith(("\\\\", "//")):
            parser.error("Use local storage; network shares are not allowed")
    args.models_dir = args.models_dir.expanduser().resolve()
    if args.llama_server is None and args.gpu_layers == 0:
        for name in ("llama-server.exe", "llama-server"):
            candidate = ROOT / ".yantra/runtime/cpu/llama" / name
            if candidate.is_file():
                args.llama_server = candidate
                break
    if args.llama_server:
        if not args.llama_server.is_file():
            parser.error("llama-server binary does not exist")
        os.environ["YANTRA_LLAMA_SERVER"] = str(args.llama_server.resolve())
    from yantra_server.seal.env import apply_seal_env_to_current_process
    from yantra_server.seal.socket_guard import install

    os.environ["YANTRA_SEALED"] = "1"
    apply_seal_env_to_current_process()
    install(allowlist=["127.0.0.0/8", "::1/128"])
    data, workspace = configure(args)
    from yantra_server.config import load_config

    loaded = load_config(
        cli_overrides={
            "profile": "portable",
            "paths": {
                "data_dir": str(data),
                "models_dir": str(args.models_dir),
                "assets_dir": str(ROOT),
                "workspace_roots": [str(workspace)],
            },
            "server": {
                "host": "127.0.0.1",
                "port": args.port,
                "require_namespace": args.require_namespace,
            },
            "seal": {"allowlist": ["127.0.0.0/8", "::1/128"]},
            "gateway": {"max_resident_models": args.resident_models},
            "execution": {"max_parallel_tasks": args.parallel_tasks},
            "knowledge": {"lexical_only": args.retrieval == "lexical"},
            "sandbox": {"backend": args.sandbox},
        },
        assets_dir=ROOT,
    )
    import uvicorn

    from yantra_server.app import create_app

    print(
        f"BlackBox Workspace: http://127.0.0.1:{args.port} | workspace: {workspace} | code sandbox: {args.sandbox}"
    )
    uvicorn.run(
        create_app(loaded),
        host="127.0.0.1",
        port=args.port,
        uds=str(args.uds) if args.uds else None,
        proxy_headers=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
