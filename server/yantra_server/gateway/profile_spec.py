"""Hardware profile files (models/profiles/*.yaml): engine launch specs + defaults."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class EngineSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: Literal["vllm", "llamacpp", "pooling", "mock"]
    model: str | None = None
    models: list[str] = Field(default_factory=list)  # pooling workers serve several
    url: str | None = None  # attach to an already-running server (compose sibling) — no spawn
    device: str = "auto"  # cpu | cuda:N | "cuda:0,1"
    gpu_memory_utilization: float | None = None
    max_model_len: int | None = None
    kv_cache_dtype: str | None = None
    enable_prefix_caching: bool = True
    tensor_parallel: int = 1
    replicas: int = 1
    threads: int | None = None
    ctx: int | None = None
    gpu_layers: int = 0
    mmproj: str | None = None
    mode: Literal["chat", "embedding", "reranking"] | None = None
    on_demand: bool = False
    vram_fraction: float | None = None

    def served_models(self) -> list[str]:
        if self.model:
            return [self.model]
        return list(self.models)


class ProfileSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str = ""
    db: str = "sqlite"
    config_defaults: dict[str, Any] = Field(default_factory=dict)
    engines: list[EngineSpec] = Field(default_factory=list)
    ingestion: dict[str, Any] = Field(default_factory=dict)
    concurrency: dict[str, Any] = Field(default_factory=dict)
    measured: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, assets_dir: Path, name: str) -> ProfileSpec:
        # YANTRA_PROFILE_FILE points at an alternate profile YAML (docker-compose mounts
        # one whose engines carry `url:` attach targets instead of local launch specs).
        override = os.environ.get("YANTRA_PROFILE_FILE")
        path = Path(override) if override else assets_dir / "models" / "profiles" / f"{name}.yaml"
        if not path.is_file():
            return cls(name=name)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data.setdefault("name", name)
        return cls.model_validate(data)
