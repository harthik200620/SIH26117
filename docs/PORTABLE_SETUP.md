# BlackBox Workspace setup

Status labels in this document: **Established fact** means inspected implementation or an observed test; **recommendation** is an engineering judgment, not a demonstrated deployment guarantee. Read [VALIDATION_REPORT.md](VALIDATION_REPORT.md) before interpreting a feature as validated.

## 1. Choose the deployment boundary

**Recommendation:** begin with one operator, one machine or private cloud VM, and one workspace directory. The browser, API, model server and retrieval store communicate locally. On a cloud VM, open the browser through an SSH tunnel; inference and documents remain on that VM. This is self-hosted cloud inference, not a public model API. Cloud provider access, backups, snapshots and region selection remain part of your confidentiality assessment.

| Host | Runtime choice | Suggested first test | Execution isolation |
|---|---|---|---|
| Windows x64 | CPU initially; Vulkan/CUDA after testing | Small Q4 GGUF | Disabled initially; Docker backend requires a preloaded Linux image |
| Linux x64 | CPU, Vulkan or NVIDIA CUDA | Qwen3 4B/8B as memory permits | bubblewrap or Docker after native isolation tests |
| Apple Silicon | llama.cpp Metal | Small Q4 GGUF, shared RAM budget | Docker after validation; otherwise disabled |
| Linux ARM/cloud | Compatible CPU build or explicit locally built runtime | Model suited to actual RAM/instruction set | Linux isolation, private networking |
| Large private GPU server | Compatible llama.cpp; advanced vLLM profiles separately | Benchmark 8B, then larger candidates | Dedicated service account and OS egress denial |

**Established fact:** portable operation was exercised on Windows x64; the [strict Linux CPU profile](STRICT_DEPLOYMENT.md) was exercised on Ubuntu WSL2 with real inference and native namespace checks. Other platforms have launch/configuration support; they have not all been physically tested. “Any machine” means supported operating system, architecture, drivers and sufficient memory—not that every model fits every device.

## 2. Prepare dependencies while connected

Use Python **3.12**. The existing web build is included, so Node is only needed to rebuild the interface. Do setup on a connected staging machine without confidential workspace files. After installation, runtime never invokes a package installer.

```text
python -m venv .venv
```

Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[knowledge,vision,render]"
.\.venv\Scripts\python.exe scripts/setup_workbench.py --runtime --backend cpu
.\.venv\Scripts\python.exe scripts/setup_workbench.py --download qwen3-4b
```

Linux/macOS:

```sh
.venv/bin/python -m pip install -e '.[knowledge,vision,render]'
.venv/bin/python scripts/setup_workbench.py --runtime --backend cpu
.venv/bin/python scripts/setup_workbench.py --download qwen3-4b
```

Run `python scripts/setup_workbench.py` without flags to see the bundled model catalogue without downloading. Copy the exact catalogue identifier shown; the model's runtime identifier is separately derived from GGUF metadata. The Models page also lists upstream links and setup commands. It estimates capacity; it does not claim that a model has loaded successfully.

**Established fact:** explicit provisioning downloads official release assets, pins the model revision, checks SHA-256, and records a provenance sidecar. A hash from the same upstream establishes integrity against that metadata; it is not an independent security audit of the weights, runtime or license. Read each model license before deployment. The code does not automatically run remote model Python code.

For disconnected installation, prepare a wheelhouse and native dependencies on the **same target OS/architecture**, transfer the source, wheels, model weights, runtime and provenance files, and install with `pip --no-index --find-links <wheelhouse>`. An editable Python installation is convenient for development; production packaging should use a reviewed wheel and a reproducible dependency lock. Never copy a Windows virtual environment onto Linux.

If a compatible release binary is unavailable, build llama.cpp on a staging machine and pass its absolute `--llama-server` path. CUDA installations may require companion runtime archives and a compatible installed driver. The provisioner selects matching upstream assets when available and fails if it cannot verify an archive.

## 3. Start the offline application

For enforced runtime network isolation, use the separate [strict Linux launcher](STRICT_DEPLOYMENT.md). The commands below start the portable profile; they do not attest OS network isolation.

Windows:

```powershell
.\Start-BlackBox.ps1
```

Linux/macOS:

```sh
sh start-blackbox.sh
```

Open **http://127.0.0.1:7331**. This starts real inference on demand; a missing model is reported as missing, never replaced with a mock response. In this checkout, the separately validated CPU runtime can be selected explicitly:

```powershell
.\.venv\Scripts\python.exe scripts/run_workbench.py --llama-server .yantra/runtime/cpu/llama/llama-server.exe
```

Common options:

```text
--workspace <directory>        The allowed document and output tree
--models-dir <directory>       Offline GGUF weights
--data-dir <directory>         Database, indices, traces and local configuration
--llama-server <binary>        Explicit inference runtime
--context 8192                Context affects KV cache and peak memory
--threads 8                   CPU inference threads
--gpu-layers 0                CPU; -1 full GPU offload; positive partial offload
--resident-models 1           Memory-saving default; larger values retain models
--parallel-tasks 1            Workflow task concurrency, distinct from inference concurrency
--sandbox disabled           Or docker/bwrap after installing and validating isolation
--retrieval lexical          Or hybrid after configuring a working local embedding model
--port 7331
```

**Recommendation:** retain CPU mode until a real short task passes. Then test GPU offload, inspect the engine log, and measure latency and memory. Integrated GPU memory is shared with RAM; an advertised VRAM number does not establish usable capacity. A model failing to load is not proof it cannot run on another backend or at a smaller context. Use pre-quantized GGUF files first; this workbench does not implement quantization training or automatic hardware tuning.

In Models, inspect the detected host, installed files and current role assignments. The workbench model selector pins a model for that run. Automatic routing prefers smaller executors/utilities and larger planners/reviewers among available candidates; explicit local routing overrides take precedence. Local role lists live in `<data-dir>/routing.local.yaml`; generated candidate order lives separately in `routing.generated.yaml`. Restart after manual configuration changes. An `embed` role requires embedding weights and an embedding-mode server; a vision model requires its matching projector.

## 4. Exercise real work

The provided `workspace` contains **synthetic** inspection and maintenance documents. Start with:

```text
Write verification.txt containing exactly Hello from YANTRA
```

Then:

```text
Read inspection-P101.md and maintenance-procedure.md. Use calculate_quantity with original source values and units to calculate hydraulic and shaft power in kW. Write a concise report with sources, assumptions and unknown OEM limits. Mark it synthetic.
```

Watch the live plan, tool events and verification. Inspect the actual output file, not just the final summary. The synthetic reference is **2.1582 kW hydraulic** and **3.083142857 kW shaft** for 36 m³/h, 22 m head, density 1000 kg/m³, g=9.81 m/s² and efficiency 0.70. These are arithmetic references for the fixture, not real equipment acceptance criteria.

The Models page runs a local latency/throughput benchmark. For correctness, run the included portable evaluation script described in the validation report. A throughput benchmark does not measure engineering accuracy.

## 5. Private cloud/server deployment

Provision files to `/opt/yantra`, weights to `/srv/yantra/models`, workspace to `/srv/yantra/workspace` and writable state to `/var/lib/yantra`. Use a dedicated non-root `yantra` account; make the code, weights and runtime read-only to it. Create `/etc/yantra/environment`, owned by root with mode 0600, containing a high-entropy `YANTRA_SERVER__ADMIN_TOKEN`. Never commit that key.

Review and adapt [../deploy/yantra.service](../deploy/yantra.service), then install it as a systemd service. Its loopback-only IP policy covers the service cgroup, including native model subprocesses **if the kernel and systemd support and enforce the required filtering**. A unit file alone is not an attestation. The initial unit intentionally disables arbitrary generated code; enable a validated sandbox in a reviewed service override.

On the operator's machine:

```sh
ssh -N -L 7331:127.0.0.1:7331 your-user@your-private-host
```

Browse `http://127.0.0.1:7331` and enter the configured key. The app uses an HttpOnly, SameSite session cookie, expiring after eight hours. Do not expose port 7331 or inference ports to the public Internet. This release is **single operator**, without tenant isolation or enterprise SSO/RBAC. A multi-user production service needs those controls before rollout.

## 6. Establish the zero-egress boundary

**Established fact:** application socket restrictions, offline library settings, same-origin browser policy and local endpoints reduce accidental outbound traffic. They cannot prevent a native binary or arbitrary subprocess from bypassing Python restrictions. The interface explicitly says OS isolation is not attested.

**Recommendation:** enforce outbound denial for the entire application process tree using OS/container controls, or disconnect the host. On cloud, also deny outbound IPv4/IPv6 at the network boundary, disable public IP/NAT routes, and restrict cloud metadata access. Keep administrative SSH separate from workload egress rules. Downloading is a separate provisioning phase; do not temporarily open the running confidential workspace to install models.

Before confidential use, run native negative probes **inside the same service/cgroup/container** for public TCP, UDP, DNS, IPv6, cloud metadata and redirect/proxy attempts. Observe packets on every relevant interface. Confirm local inference, retrieval and file work still succeed. Record OS, policy, runtime hashes, commands, timestamps and packet-capture results. Neither an unreachable destination nor a missing network route proves that a specific firewall rule was enforced.

Docker code execution uses `--pull=never` and `--network=none`; preload its image during setup. Do not give the workbench a root-equivalent Docker socket on a shared production host. Prefer a dedicated worker VM or an appropriately constrained rootless runtime. On unsupported or unverified hosts, leave generated code execution disabled; file work, retrieval and bounded calculators still function.

### Preparing the optional code runner

On a connected **Linux target-compatible staging host**, preload a reviewed Python 3.12 base image and prepare the sandbox wheels. Versions below come from this repository's `uv.lock`; review the lock and include transitive wheel hashes in the transferred bundle manifest.

```sh
python -m pip download --only-binary=:all: --dest wheelhouse pytest==9.1.1 numpy==2.5.2 sympy==1.14.0
docker build --pull=false --network=none -f deploy/Dockerfile.sandbox -t yantra-sandbox:latest .
docker save -o yantra-sandbox.tar yantra-sandbox:latest
```

For reproducible production images, pass `--build-arg PYTHON_IMAGE=python@sha256:<reviewed-digest>` with the preloaded digest. The reference tag is not a supply-chain attestation. Transfer and verify the archive on the disconnected execution host, then `docker load -i yantra-sandbox.tar`. Use an operator-owned writable workspace; the runner uses a non-root UID. Windows/macOS Docker hosts require a compatible Linux VM. Dependencies needed by generated code must be preloaded during this provisioning phase.

Before enabling the runner:

```sh
python scripts/verify_sandbox.py --backend docker --output .yantra/sandbox-check.json
python scripts/run_workbench.py --sandbox docker
```

For bubblewrap use `--backend bwrap` and `--sandbox bwrap`; the host's Python environment must already contain the execution dependencies. The probe uses only disposable synthetic files, checks calculation/file writing, tests an outside-workspace sentinel, and attempts public IPv4/IPv6, metadata and DNS access. It fails without falling back when the backend is unavailable. This behavioral test supplements native policy and packet verification; it does not certify containment against a malicious workload. The reference image was **not built or executed** on this Windows host because Docker was unavailable.

## Failure modes

- Missing runtime/weights: setup instructions; no automatic download during work.
- Out of memory: reduce context/model size/residency, close competing applications, or select a different backend.
- Unsupported GPU/architecture: compatible explicit runtime or CPU mode.
- Model produces bad tool arguments or units: visible failure, bounded retry and review; no correctness guarantee from a reviewer alone.
- Scanned-only document without OCR: ingestion error, not a fabricated answer.
- No embedding model: lexical retrieval remains available; hybrid mode requires setup.
- Sandbox absent: arbitrary code tools stay unavailable; no unsafe fallback.
- Service restart or cancelled run: inspect persisted history and artifacts; interrupted tool side effects may remain.
- Changed/deleted sources: reindex before querying; current implementation removes stale text chunks, but full version lifecycle management remains future work.

Upstream references: [llama.cpp](https://github.com/ggml-org/llama.cpp), [Qwen3 4B GGUF](https://huggingface.co/Qwen/Qwen3-4B-GGUF), [Qwen3 embedding GGUF](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF), [systemd resource controls](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html).
