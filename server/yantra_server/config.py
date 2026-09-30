"""Layered configuration: profile defaults -> yantra.yaml -> YANTRA_* env -> CLI overrides.

Every resolved value remembers which layer set it, so `yantra config show` can print the
effective configuration with sources (SPEC §19.4).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

ENV_PREFIX = "YANTRA_"
# Env vars that are runtime switches, not config-tree overrides.
ENV_EXCLUDE = {
    "YANTRA_SEALED",
    "YANTRA_CONFIG",
    "YANTRA_ASSETS_DIR",
    "YANTRA_TUI_PATH",
    "YANTRA_BUILD_DOWNLOAD_MODELS",
    "YANTRA_SEAL_ALLOWLIST",
    "YANTRA_BIND",
    "YANTRA_PROFILE_FILE",
    "YANTRA_LLAMA_SERVER",
    "YANTRA_SANDBOX_IMAGE",
}

Decision = Literal["allow", "ask", "deny"]
Mode = Literal["ask", "auto", "plan"]


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or validated."""


class PathsConfig(BaseModel):
    # Defaults use factories, not literal "~" paths: pydantic v2 does not run validators on
    # defaults, so a literal Path("~/...") default would never be expanduser()'d and state
    # would land in a directory named "~" under the CWD (bug found post-1.0.0 on Windows).
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".yantra")
    models_dir: Path = Field(default_factory=lambda: Path.home() / ".yantra" / "models")
    workspace_roots: list[Path] = Field(default_factory=list)
    assets_dir: Path = Path(".")

    @field_validator("data_dir", "models_dir", "assets_dir", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()

    @field_validator("workspace_roots", mode="after")
    @classmethod
    def _expand_roots(cls, v: list[Path]) -> list[Path]:
        return [p.expanduser() for p in v]


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7331
    admin_token: str | None = None
    require_namespace: bool = False


class DbConfig(BaseModel):
    url: str | None = None  # default: sqlite file under paths.data_dir
    echo: bool = False


class GatewayConfig(BaseModel):
    max_resident_models: int = 0
    request_timeout_s: float = 300.0
    max_retries: int = 3
    cache_ttl_s: int = 86400
    queue_size: int = 256
    registry_file: Path = Path("models/registry.yaml")
    routing_file: Path = Path("models/routing.yaml")


class BudgetsConfig(BaseModel):
    max_tokens: int = 400_000
    max_seconds: int = 3600
    max_tool_calls: int = 400
    max_sandbox_cpu_s: int = 1200
    warn_ratio: float = 0.8


class ContextConfig(BaseModel):
    executor: int = 24_000
    planner: int = 32_000
    reviewer: int = 24_000
    utility: int = 8_000


class PermissionRule(BaseModel):
    tool: str = "*"
    path_glob: str | None = None
    cmd_regex: str | None = None
    side_effects: str | None = None
    decision: Decision
    modes: list[Mode] | None = None  # None = all modes


class KnowledgeConfig(BaseModel):
    lexical_only: bool = False
    auto_index_workspace: bool = False
    local_ocr: bool = True
    max_ocr_pages: int = Field(default=50, ge=1, le=500)
    collections: list[str] = Field(default_factory=list)
    chunk_tokens_child: int = 400
    chunk_tokens_parent: int = 1200
    chunk_overlap_ratio: float = 0.15
    fusion_weights: dict[str, float] = Field(
        default_factory=lambda: {"lexical": 1.0, "dense": 1.0, "visual": 0.8}
    )
    rerank_depth: int = 60
    final_k: int = 10
    tag_patterns_file: Path = Path("knowledge/tag_patterns.yaml")
    ingest_workers: int = 2
    # None = embedded Qdrant under data_dir; "server:http://host:6333" (or a bare http(s)
    # URL) targets a Qdrant server — docker-compose sets this to the qdrant service.
    qdrant_location: str | None = None


class VisionConfig(BaseModel):
    drawing_dpi: int = 200
    text_dpi: int = 110
    tile_size: int = 1280
    tile_overlap: float = 0.15
    max_images_per_call: int = 4


class RenderConfig(BaseModel):
    templates_dir: Path = Path("templates")
    libreoffice_path: Path | None = None


class SealConfig(BaseModel):
    allowlist: list[str] = Field(
        default_factory=lambda: [
            "127.0.0.0/8",
            "::1/128",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
        ]
    )
    nftables_expected: bool = False


class ObserveConfig(BaseModel):
    retention_days: int = 365
    redaction: Literal["off", "hash", "mask"] = "off"
    otlp_endpoint: str | None = None
    cost_per_mtoken_inr: float = 450.0

    @field_validator("redaction", mode="before")
    @classmethod
    def _yaml_off_is_string(cls, v: Any) -> Any:
        # YAML 1.1 parses a bare `off` as boolean False; operators mean the string.
        return "off" if v is False else v


class MemoryConfig(BaseModel):
    shared_team_scope: bool = False


class EvalsConfig(BaseModel):
    suites_dir: Path = Path("server/yantra_server/evals/suites")


class SandboxConfig(BaseModel):
    backend: Literal["auto", "bwrap", "docker", "local", "disabled"] = "auto"
    max_seconds: int = 120
    max_seconds_hard: int = 600
    max_rss_mb: int = 4096
    max_pids: int = 256
    gpu: bool = False


class ExecutionConfig(BaseModel):
    compact_planning: bool = False
    max_parallel_tasks: int = 2
    max_steps_per_task: int = 30
    max_task_retries: int = 3
    delegate_max_depth: int = 2
    delegate_max_children: int = 6


class YantraConfig(BaseModel):
    profile: str = "lite"
    paths: PathsConfig = Field(default_factory=PathsConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    db: DbConfig = Field(default_factory=DbConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    permissions: list[PermissionRule] = Field(default_factory=list)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    seal: SealConfig = Field(default_factory=SealConfig)
    observe: ObserveConfig = Field(default_factory=ObserveConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    evals: EvalsConfig = Field(default_factory=EvalsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)

    def db_url(self) -> str:
        if self.db.url:
            return self.db.url
        return f"sqlite:///{(self.paths.data_dir / 'yantra.db').as_posix()}"

    def sealed(self) -> bool:
        return os.environ.get("YANTRA_SEALED", "1") != "0"


def default_permission_rules() -> list[PermissionRule]:
    """The strict default policy (SPEC §9.4), used when the config defines none."""
    return [
        PermissionRule(tool="delete_file", decision="deny"),
        PermissionRule(
            tool="bash",
            cmd_regex=r"rm\s+-rf|mkfs|dd\s+if=|shutdown|reboot|:\(\)\{",
            decision="deny",
        ),
        PermissionRule(tool="*", side_effects="none", decision="allow"),
        PermissionRule(tool="*", side_effects="read", decision="allow"),
        PermissionRule(tool="render_document", decision="allow"),
        PermissionRule(tool="*", decision="ask", modes=["ask"]),
        PermissionRule(tool="*", decision="allow", modes=["auto"]),
        PermissionRule(tool="*", decision="deny", modes=["plan"]),
    ]


# --------------------------------------------------------------------------- layering


def _deep_merge(
    base: dict[str, Any],
    over: dict[str, Any],
    sources: dict[str, str],
    source_name: str,
    prefix: str = "",
) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            existing = out.get(key)
            out[key] = _deep_merge(
                existing if isinstance(existing, dict) else {},
                value,
                sources,
                source_name,
                prefix=f"{path}.",
            )
        else:
            out[key] = value
            sources[path] = source_name
    return out


def _env_overrides() -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for name, raw in os.environ.items():
        if not name.startswith(ENV_PREFIX) or name in ENV_EXCLUDE:
            continue
        dotted = name[len(ENV_PREFIX) :].lower().split("__")
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError:
            value = raw
        node = tree
        for part in dotted[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigError(f"env override {name} conflicts with a scalar parent")
        node[dotted[-1]] = value
    return tree


def find_config_file(explicit: Path | None = None) -> Path | None:
    """Search order: explicit/--config, $YANTRA_CONFIG, ./yantra.yaml, ~/.yantra/yantra.yaml."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    if env_path := os.environ.get("YANTRA_CONFIG"):
        candidates.append(Path(env_path))
    candidates.append(Path.cwd() / "yantra.yaml")
    candidates.append(Path("~/.yantra/yantra.yaml").expanduser())
    for cand in candidates:
        if cand.expanduser().is_file():
            return cand.expanduser()
    if explicit:  # explicitly named but missing is an error
        raise ConfigError(f"config file not found: {explicit}")
    return None


def find_assets_dir(start: Path | None = None) -> Path:
    """Directory holding models/, agents/, knowledge/, templates/ (repo root or bundle root)."""
    if env_dir := os.environ.get("YANTRA_ASSETS_DIR"):
        return Path(env_dir).expanduser()
    probe = (start or Path.cwd()).resolve()
    for cand in [probe, *probe.parents]:
        if (cand / "models" / "registry.yaml").is_file():
            return cand
    return probe


def _load_yaml_file(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return loaded


def _profile_defaults(profile: str, assets_dir: Path) -> dict[str, Any]:
    path = assets_dir / "models" / "profiles" / f"{profile}.yaml"
    if not path.is_file():
        return {}
    data = _load_yaml_file(path)
    defaults = data.get("config_defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError(f"config_defaults in {path} must be a mapping")
    return defaults


class LoadedConfig(BaseModel):
    """Effective config plus provenance and the paths that produced it."""

    model_config = {"arbitrary_types_allowed": True}

    config: YantraConfig
    sources: dict[str, str]
    config_file: Path | None
    assets_dir: Path


def load_config(
    cli_overrides: dict[str, Any] | None = None,
    config_path: Path | None = None,
    assets_dir: Path | None = None,
) -> LoadedConfig:
    """Resolve the effective configuration with per-key source tracking."""
    sources: dict[str, str] = {}
    cli_overrides = cli_overrides or {}

    config_file = find_config_file(config_path)
    file_layer = _load_yaml_file(config_file) if config_file else {}
    env_layer = _env_overrides()

    # The profile can itself be set at any layer; resolve it first (highest layer wins).
    profile = str(
        cli_overrides.get("profile")
        or env_layer.get("profile")
        or file_layer.get("profile")
        or "lite"
    )
    assets = assets_dir or find_assets_dir(config_file.parent if config_file else None)
    profile_layer = _profile_defaults(profile, assets)

    merged: dict[str, Any] = {}
    merged = _deep_merge(merged, profile_layer, sources, f"profile:{profile}")
    merged = _deep_merge(merged, file_layer, sources, str(config_file) if config_file else "file")
    merged = _deep_merge(merged, env_layer, sources, "env")
    merged = _deep_merge(merged, cli_overrides, sources, "cli")
    merged["profile"] = profile

    try:
        config = YantraConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc

    if not config.permissions:
        config.permissions = default_permission_rules()
        sources["permissions"] = "default"
    if merged.get("paths", {}).get("assets_dir") is None or "assets_dir" not in merged.get(
        "paths", {}
    ):
        config.paths.assets_dir = assets

    return LoadedConfig(config=config, sources=sources, config_file=config_file, assets_dir=assets)


def effective_report(loaded: LoadedConfig) -> list[tuple[str, str, str]]:
    """Flat (key, value, source) rows for `yantra config show`."""
    dumped = loaded.config.model_dump(mode="json")

    def walk(node: Any, prefix: str) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        if isinstance(node, dict):
            for key, value in node.items():
                path = f"{prefix}.{key}" if prefix else key
                rows.extend(walk(value, path))
        else:
            source = _nearest_source(loaded.sources, prefix)
            rows.append((prefix, yaml.safe_dump(node, default_flow_style=True).strip(), source))
        return rows

    return walk(dumped, "")


def _nearest_source(sources: dict[str, str], key: str) -> str:
    probe = key
    while probe:
        if probe in sources:
            return sources[probe]
        probe = probe.rpartition(".")[0]
    return "default"
