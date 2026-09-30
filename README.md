# BlackBox Workspace

A private industrial AI workbench for local machines and self-hosted cloud servers. It uses locally installed open-weight models to plan work, retrieve workspace documents, use tools, write files and check outputs.

**Established fact:** real local inference, file creation and unit-aware calculations have been exercised on Windows. A strict Linux CPU runtime has separately passed namespace/native-socket checks and a bounded packet observation. The interface distinguishes verified runtime namespace checks from an application guard with unverified OS isolation. Scripted harness tests are not evidence of model intelligence: recent industrial development trials still failed source-to-output correctness.

Start here:

- [Latest correctness work and competition readiness evidence](docs/READINESS_2026-09-29.md)
- [Active BlackBox release requirements](docs/BLACKBOX_RELEASE_PLAN.md)
- [BlackBox interaction and design system](docs/BLACKBOX_DESIGN_SYSTEM.md)
- [Detailed architecture and implementation boundaries](docs/WORKBENCH_DESIGN.md)
- [Portable setup, model installation and private-cloud deployment](docs/PORTABLE_SETUP.md)
- [Strict Linux CPU deployment and verified boundary](docs/STRICT_DEPLOYMENT.md)
- [Validation results, failures and remaining release gates](docs/VALIDATION_REPORT.md)

## Run the workbench

Use Python 3.12. Install dependencies and weights on a connected staging machine before using confidential documents:

```text
python -m venv .venv
python -m pip install -e ".[knowledge,vision,render,analysis,ocr]"
python scripts/setup_workbench.py --runtime --backend cpu
python scripts/setup_workbench.py --download qwen3-4b
```

Use the virtual environment's Python for the last three commands; platform-specific commands are in the setup guide. Provisioning is separate from runtime. Running the setup script without download flags only lists the bundled catalogue.

Windows: `Start-BlackBox.ps1`. Linux/macOS: `sh start-blackbox.sh`. Legacy launcher names remain compatible.

Open **http://127.0.0.1:7331**. The portable launcher uses real model files and reports missing setup. It does not silently substitute a mock model. Default workspace: `workspace/`; default state: `.yantra/workbench/`.

Requests are classified before workspace indexing or planning. Greetings, general questions,
and drafts requested in chat use a tool-free assistant reply; they do not create workspace
files. Requests that need local files or saved deliverables enter the existing workflow.
Conversation history and usage records still persist in the application state directory.
Intent classification depends on the selected local model; malformed routing results stop
the run instead of granting tool access.

```text
python scripts/run_workbench.py --workspace <directory> --models-dir <directory> --llama-server <binary>
```

Generated code execution is disabled until an isolated Docker/bubblewrap backend is explicitly selected and verified. File operations, retrieval, arithmetic and unit-aware calculations remain available. For cloud use, run the application on the private host and access it through an SSH tunnel; do not publish its port publicly.

## What is implemented

- A browser workbench with task history, model choice, local files, evidence search, live SSE events, stop controls, verification and downloadable outputs.
- A planner/executor/reviewer workflow with task dependencies, bounded retries, loop detection and token/time/tool budgets.
- Workspace-scoped local retrieval: BM25 by default; optional local embeddings and reranking after configuration.
- Typed, permission-checked tools for files, calculations and isolated execution. Physical quantity calculations preserve original units and check dimensions.
- GGUF discovery, local model routing and managed inference processes; CPU/GPU/context settings and a one-resident-model default for constrained hosts.
- A separate checksum-verifying model/runtime provisioner and a curated model catalogue with advisory memory estimates.
- Local audit/traces, optional access-key login, origin/host checks, private managed inference keys and Python network restrictions.

**Limitations:** no demonstrated superiority over Claude Code or Codex; no production security certification; no universal hardware optimizer; no enterprise multi-tenancy; no end-to-end multimodal quality validation on this host. A model review is not proof that an engineering conclusion is correct. Read the validation report before using any result as operational guidance.

## Verify a deployment

```text
python scripts/evaluate_workbench.py --case exact
python scripts/evaluate_workbench.py --case pump --model <installed-model-id>
```

The evaluator creates a fresh workspace subdirectory, runs the actual model and compares the output with independent synthetic references. It reports failures and usage. It does not certify the network boundary.

For development, install the test tools listed in `pyproject.toml`, then run `python -m pytest`, `python -m mypy` and `python -m ruff check server`. Rebuild the interface from `web/` using its checked-in package configuration. Do not install packages or pull container images during confidential runtime operation.

## Repository map

| Directory | Purpose |
|---|---|
| `server/` | API, conductor, gateway, retrieval, tools, sandbox and tests |
| `web/` | React interface, compiled into the server's local static assets |
| `tui/` | Existing terminal client |
| `models/` | Profiles, catalogue and routing/registry configuration |
| `agents/`, `knowledge/`, `templates/` | Agent definitions and industrial document tooling |
| `workspace/` | Synthetic demonstration inputs and local test outputs |
| `scripts/` | Provisioning, launch, evaluation and advanced deployment utilities |
| `deploy/` | Reference private Linux service policy |
| `docs/` | Current setup/design/validation plus historical design documents |

Earlier architecture, pitch and evaluation documents describe broader targets and mock demonstrations. The three current documents linked above qualify those claims and are the starting point for this portable implementation.

SIH26117's authoritative parameter limit still needs confirmation: mirrors indicate 120B-class models, while the request mentioned 128B. This implementation conservatively uses a below-120B ceiling. See the design document for sources and the distinction between total and active parameters.

License: Apache-2.0 for this repository. Model and third-party component licenses are separate; see their upstream cards and `NOTICE.md`.
# Competition readiness

**Established fact, 28 September 2026:** the build remains under validation. Start with the [SIH26117 requirement-to-evidence map](docs/SIH26117_REQUIREMENTS.md), [Round 2 submission brief](docs/ROUND2_SUBMISSION_BRIEF.md), and [current demonstration script](docs/PITCH_CURRENT.md). The official Round 2 notice is still needed to verify submission format and deadline. Historical mock successes do not establish real-model industrial accuracy.
- [Latest upgrades, live failures and measured checks](docs/UPGRADE_REPORT_2026-09-28.md)
- [SIH26117 requirement mapping](docs/SIH26117_REQUIREMENTS.md)
- [Round 2 preparation and unresolved official requirements](docs/ROUND2_SUBMISSION_BRIEF.md)
- [Review bundle contents and limitations](docs/REVIEW_BUNDLE.md)
