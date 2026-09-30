import subprocess
from pathlib import Path
from typing import Any

from yantra_server.config import load_config
from yantra_server.gateway.profile_spec import EngineSpec, ProfileSpec
from yantra_server.gateway.registry import ModelManifest, ModelRegistry
from yantra_server.gateway.supervisor import Supervisor

REPO = Path(__file__).resolve().parents[3]


def make_registry(tmp_path: Path) -> ModelRegistry:
    registry = ModelRegistry(tmp_path / "r.yaml", tmp_path / "models")
    registry.register(
        ModelManifest(
            id="brain",
            path="Brain-FP8",
            engine="vllm",
            params_b=27,
            serve_context_len=131072,
            serve_args=["--reasoning-parser", "qwen3"],
        )
    )
    registry.register(
        ModelManifest(
            id="tiny-gguf",
            path="tiny.gguf",
            engine="llamacpp",
            params_b=0.8,
            serve_context_len=8192,
        )
    )
    return registry


def make_supervisor(tmp_path: Path, engines: list[EngineSpec]) -> Supervisor:
    loaded = load_config()
    loaded.config.paths.models_dir = tmp_path / "models"
    loaded.config.paths.data_dir = tmp_path / "data"
    profile = ProfileSpec(name="test", engines=engines)
    return Supervisor(loaded.config, make_registry(tmp_path), profile)


def test_vllm_command_construction(tmp_path: Path) -> None:
    supervisor = make_supervisor(
        tmp_path,
        [
            EngineSpec(
                id="brain",
                kind="vllm",
                model="brain",
                device="cuda:0,1",
                gpu_memory_utilization=0.9,
                max_model_len=65536,
                kv_cache_dtype="fp8",
                tensor_parallel=2,
            )
        ],
    )
    ep = supervisor.processes[0]
    cmd = supervisor.command_for(ep)
    assert cmd is not None
    text = " ".join(cmd)
    assert cmd[0:2] == ["vllm", "serve"]
    assert "--served-model-name brain" in text
    assert "--gpu-memory-utilization 0.9" in text
    assert "--max-model-len 65536" in text
    assert "--enable-prefix-caching" in text
    assert "--kv-cache-dtype fp8" in text
    assert "--tensor-parallel-size 2" in text
    assert "--reasoning-parser qwen3" in text
    assert '"backend": "xgrammar"' in text
    env = supervisor.environment_for(ep)
    assert env["CUDA_VISIBLE_DEVICES"] == "0,1"


def test_llamacpp_command_construction(tmp_path: Path) -> None:
    supervisor = make_supervisor(
        tmp_path,
        [
            EngineSpec(
                id="util",
                kind="llamacpp",
                model="tiny-gguf",
                device="cpu",
                threads=6,
                mode="embedding",
            )
        ],
    )
    cmd = supervisor.command_for(supervisor.processes[0])
    assert cmd is not None
    text = " ".join(cmd)
    assert Path(cmd[0]).stem == "llama-server"
    assert "--alias tiny-gguf" in text
    assert "-t 6" in text
    assert "--embedding" in text
    env = supervisor.environment_for(supervisor.processes[0])
    assert env["CUDA_VISIBLE_DEVICES"] == ""


def test_sealed_env_exported(tmp_path: Path) -> None:
    supervisor = make_supervisor(tmp_path, [EngineSpec(id="brain", kind="vllm", model="brain")])
    env = supervisor.environment_for(supervisor.processes[0])
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["VLLM_NO_USAGE_STATS"] == "1"
    assert env["PIP_NO_INDEX"] == "1"
    assert "OPENAI_API_KEY" not in env
    assert "YANTRA_SEAL_ALLOWLIST" in env


def test_port_allocation_and_replicas(tmp_path: Path) -> None:
    supervisor = make_supervisor(
        tmp_path,
        [
            EngineSpec(id="brain", kind="vllm", model="brain", replicas=2),
            EngineSpec(id="util", kind="llamacpp", model="tiny-gguf"),
            EngineSpec(id="pool", kind="pooling", models=["e1"]),
        ],
    )
    ports = [ep.port for ep in supervisor.processes]
    assert ports[0] != ports[1] and ports[1] != ports[2]
    assert supervisor.processes[3].port is None  # pooling has no port


async def test_missing_binary_marks_unavailable(tmp_path: Path, monkeypatch: Any) -> None:
    import yantra_server.gateway.supervisor as sup_mod

    monkeypatch.setattr(sup_mod.shutil, "which", lambda name: None)
    supervisor = make_supervisor(tmp_path, [EngineSpec(id="brain", kind="vllm", model="brain")])
    await supervisor.start(supervisor.processes[0])
    assert supervisor.processes[0].status == "unavailable"
    assert "binary" in (supervisor.processes[0].last_error or "")
    assert not supervisor.model_available("brain")


async def test_missing_weights_marks_unavailable(tmp_path: Path, monkeypatch: Any) -> None:
    import yantra_server.gateway.supervisor as sup_mod

    monkeypatch.setattr(sup_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    supervisor = make_supervisor(tmp_path, [EngineSpec(id="brain", kind="vllm", model="brain")])
    await supervisor.start(supervisor.processes[0])
    ep = supervisor.processes[0]
    assert ep.status == "unavailable"
    assert "weights missing" in (ep.last_error or "")


async def test_spawn_called_with_sealed_env(tmp_path: Path, monkeypatch: Any) -> None:
    import yantra_server.gateway.supervisor as sup_mod

    monkeypatch.setattr(sup_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    (tmp_path / "models").mkdir(parents=True, exist_ok=True)
    (tmp_path / "models" / "tiny.gguf").write_bytes(b"GGUF")
    captured: dict[str, Any] = {}

    class FakeProc:
        pid = 4242

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def fake_spawn(cmd: list[str], **kw: Any) -> Any:
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        captured["stderr"] = kw.get("stderr")
        return FakeProc()

    loaded = load_config()
    loaded.config.paths.models_dir = tmp_path / "models"
    loaded.config.paths.data_dir = tmp_path / "data"
    supervisor = Supervisor(
        loaded.config,
        make_registry(tmp_path),
        ProfileSpec(name="t", engines=[EngineSpec(id="util", kind="llamacpp", model="tiny-gguf")]),
        spawn=fake_spawn,
    )
    await supervisor.start(supervisor.processes[0])
    ep = supervisor.processes[0]
    assert ep.status == "starting" and ep.proc is not None
    assert captured["env"]["HF_HUB_OFFLINE"] == "1"
    assert captured["stderr"] == subprocess.STDOUT
    assert ep.log_file is not None and ep.log_file.parent.name == "logs"
    status = supervisor.status()[0]
    assert status["pid"] == 4242 and status["status"] == "starting"


def test_bundled_profiles_parse() -> None:
    for name in ("lite", "standard", "refinery", "mock"):
        profile = ProfileSpec.load(REPO, name)
        assert profile.name == name
        if name != "mock":
            assert any(e.kind in ("vllm", "llamacpp") for e in profile.engines)


def test_compose_profiles_attach_by_url() -> None:
    for name in ("lite-compose", "standard-compose", "refinery-compose"):
        profile = ProfileSpec.load(REPO, name)
        served = [e for e in profile.engines if e.kind in ("vllm", "llamacpp")]
        assert served and all(e.url and e.url.startswith("http://") for e in served), name


def test_profile_file_env_override(tmp_path: Path, monkeypatch: Any) -> None:
    alt = tmp_path / "alt.yaml"
    alt.write_text(
        "name: standard\nengines:\n  - {id: brain, kind: vllm, model: brain, url: http://b:8000}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("YANTRA_PROFILE_FILE", str(alt))
    profile = ProfileSpec.load(REPO, "standard")
    assert profile.engines[0].url == "http://b:8000"


async def test_real_engine_shadows_mock(tmp_path: Path) -> None:
    """The mock claims every model id; a healthy real engine must always win for its model."""
    supervisor = make_supervisor(
        tmp_path,
        [
            EngineSpec(id="mock", kind="mock"),
            EngineSpec(id="brain", kind="vllm", model="brain", url="http://vllm-brain:8000"),
        ],
    )
    mock_ep, real_ep = supervisor.processes
    await supervisor.start(mock_ep)
    await supervisor.start(real_ep)
    real_ep.status = "healthy"  # health loop would do this after a probe
    for _ in range(4):  # round-robin must never fall back to the mock
        engine = await supervisor.engine_for_model("brain")
        assert engine is real_ep.engine
    # models only the mock serves still route to it (GPU-less dev keeps working)
    engine = await supervisor.engine_for_model("something-else")
    assert engine is mock_ep.engine


async def test_attach_mode_skips_spawn(tmp_path: Path) -> None:
    spawned: list[Any] = []
    supervisor = make_supervisor(
        tmp_path,
        [EngineSpec(id="brain", kind="vllm", model="brain", url="http://vllm-brain:8000")],
    )
    supervisor._spawn = lambda *a, **k: spawned.append(a) or subprocess.Popen  # type: ignore[assignment,func-returns-value]
    ep = supervisor.processes[0]
    await supervisor.start(ep)
    assert not spawned  # attached, never spawned
    assert ep.status == "starting" and ep.proc is None
    assert ep.engine is not None and "vllm-brain:8000" in getattr(ep.engine, "base_url", "")
