# BlackBox Workspace validation

Observed 19–21 September 2026. **Established fact** below means an inspected output or executed test. **Not established** means the result must not be claimed from this work. All industrial fixtures used here are synthetic.

## Environment

- Windows x64, AMD Ryzen 7 7840U, 16 logical cores, approximately 15.3 GiB reported RAM, Radeon 780M integrated graphics.
- Python 3.12, local FastAPI service, bundled React interface, SQLite and local Tantivy retrieval.
- llama.cpp release b11048, official CPU Windows archive SHA-256 `850cbe89ea5c5ac9e687454f58442d5df1771676e53e07223c7fc1ba9b5593b8`.
- Qwen2.5 1.5B Instruct Q4_K_M and officially downloaded Qwen3 4B Q4_K_M. GGUF metadata identifies the latter as `qwen3-4b-instruct-awq-gguf`; this identifier is not a different downloaded model.
- The host had substantial competing memory use. Results are measurements of this host/session, not performance promises for other machines.

## Automated checks

**Established fact:** the latest full suite passed **340 tests**, skipped **6**, with one dependency deprecation warning, in **159.99 seconds**. The Linux-only resource-meter test was skipped on Windows and then passed separately on Ubuntu. Type checking passed across **146 source files**, including both launchers; Python lint, TypeScript compilation and the production web build passed. The final suite includes exact-content completion/rejection, criterion-first review, strict independent evaluation fields, summary preservation and sandbox construction regressions.

The earlier concurrent test/model run had 284 passes and one `MemoryError` during a database fixture setup. The failed case passed separately after unloading the model, then the complete suite passed. This failure is retained here because free memory matters to deployment sizing.

Coverage includes:

- Loopback inference URL restrictions, hostname/origin rejection and authentication sessions.
- Cookie integrity and key rotation; workspace/network-share/path escapes.
- Arithmetic execution injection, unbounded powers and invalid physical dimensions.
- Correct automatic m³/h conversion and efficiency-percent conversion.
- Updating/deleting indexed source text without stale lexical answers.
- Single-deliverable plan consolidation while preserving long user constraints; multiple deliverables retain dependencies.
- Terminal SSE behavior, invalid event cursors and download traversal rejection.
- Existing conductor retry, escalation, cancellation-related lifecycle, tool permissions, sandbox command construction and mock evaluation scenarios.

**Not established:** the six skipped tests in the Windows run are not passes; one Linux-only test was independently run and passed on Ubuntu. Scripted mock responses establish harness behavior, not real-model reasoning quality. Inspect test markers before quoting an evaluation score as an industrial result.

## Live inference evidence

| Run | Observed result | Interpretation |
|---|---|---|
| `47f5b6a30e40482f95ab052005979ba8` | Real 1.5B model wrote the exact 17-byte text `Hello from YANTRA`; workflow completed; 17.5 s, 1 tool call, 1,626 input + 230 output tokens | A genuine small local file task, not a mock or a broad competence benchmark |
| `7b114b27117b4e95a40743eeebdc5c18` | 4B model failed to load using the Vulkan runtime on this memory-constrained host | A capacity/backend failure; the CPU runtime subsequently loaded the same weights |
| `0e80a913427f41cc89039d99b559e82d` | CPU model used 36 m³/h as 36 m³/s; run was cancelled during investigation | Ordinary arithmetic does not validate source units; prompted addition of unit-aware quantity algebra |
| `20e7569ddefa4add851e9e92d044dadb` | Correct numeric calculations, missing output requirements; reviewer failed; repair loop reached budget; terminal state `done_with_gaps`, 1,024.3 s, 40,293 tokens, 13 tool calls | Not a successful industrial task. Exposed duplicated planning, lost repair context and reviewer mistakes; planning/context fixes and further evaluation followed |
| `4d3802912869404cb5b20b31e44311d4` | All seven independent output assertions passed; reviewer made unsupported objections; recovery loop stalled; `done_with_gaps`, 688.8 s, 30,580 tokens | Overall **failed**. A correct artifact does not make a failed workflow successful. See [failed review evidence](validation/pump-failed-review.json) |
| `c872d82836044884a1b7c1bab0cd93e7` | Completed using real local 4B inference after criterion-first review was introduced; seven independent assertions passed; `done`, 390.0 s, 16,573 input + 1,481 output = 18,054 tokens, 5 tool calls | **Passed this synthetic case**. One mistaken directory-list call recovered within the run. See [run evidence](validation/pump-passed.json), [actual output](validation/pump-result.json) and [review records](validation/review-comparison.json) |
| `2fd47824eb7f4a3bbeb65095193d7812` | 1.5B model wrote the correct exact text but continued acting until the loop guard stopped it; `done_with_gaps`, 45.3 s, 6,898 tokens | Overall **failed**. Prompted deterministic completion for narrowly specified exact-byte contracts; see [failure evidence](validation/exact-failed-loop.json) |

**Established fact:** the final automatic-routing exact-file run `bdcc7d9069ac4864b66c6bbdf45e6973` passed: 17 exact UTF-8 bytes, one permitted file write, **11.1 seconds**, **559 input + 98 output = 657 tokens**. The recorded route selected the installed 1.5B model for execution; byte verification required no reviewer-model call. See [automatic-routing evidence](validation/exact-auto-passed.json). This is a deliberately narrow deterministic contract, not a general writing benchmark.

Runtime logs for 4B CPU inference showed roughly 8–9 generated tokens/second during these runs. These are log samples, not a controlled median. No dedicated GPU performance claim is made.

The repeatable evaluator writes JSON evidence with individual assertions, terminal state, measured usage and artifact SHA-256. Exports in `docs/validation/` preserve observed results and normalize host-specific paths to workspace-relative paths. A correct numeric output with a failed workflow or missing required fields does **not** count as passing. These are development fixtures used while repairing the system, not held-out performance benchmarks. The passing pump run's final summary was truncated by the old 300-character aggregation limit; its downloaded JSON contains the complete required notice. Aggregation now preserves the bounded task summary, covered by a regression assertion; historical records were not rewritten.

## Reproduce on another host

Start the workbench with installed weights and a compatible runtime, then run:

```text
python scripts/evaluate_workbench.py --case exact --model <installed-model-id> --output .yantra/exact-evaluation.json
python scripts/evaluate_workbench.py --case pump --model <installed-model-id> --output .yantra/pump-evaluation.json
```

Set `YANTRA_SERVER__ADMIN_TOKEN` in the evaluator environment if access-key authentication is configured. It contacts a loopback API, disables proxies/redirects, and creates a fresh subdirectory within the allowed workspace. For the pump case it copies the two synthetic source fixtures. Gold checks are independent of the model's reviewer: JSON validity, both numeric answers within absolute tolerance 0.0001 kW, source filenames, a synthetic flag, assumptions and the exact unknown-OEM-limits notice. The overall result also requires terminal `done`.

Use `--run-id <id> --case pump` to inspect an existing run. Use `--workspace` to choose another allowed root and `--timeout` to bound the evaluator. The evaluator cancels only a run it started itself if its deadline expires. It never marks a timeout as a pass.

## Security evidence and its limits

**Established fact:** application tests cover several outbound socket/DNS denial paths, local API boundaries and filesystem scopes. Local model transport ignores external proxy settings and redirects. Managed llama.cpp processes receive random private API keys through their environment. Download code is confined to explicitly invoked provisioning utilities; the portable launch path does not download models, packages or frontend assets.

An unauthenticated live request to the managed model's `/v1/models` returned HTTP 401 while authenticated workbench inference succeeded. Generated Markdown external URLs are displayed as text instead of clickable navigation; rendering checks covered HTTPS, protocol-relative, JavaScript and data URLs. Browser inspection confirmed live task events, calculation outputs, readable criterion evidence, downloadable results and input/output/total token counts. The [sandbox probe](validation/sandbox-unavailable.json) failed explicitly because Docker was absent; it did not execute through an unsafe fallback.

**Not established:** no native all-process packet capture or Windows firewall attestation was performed. No cloud VM was provisioned in this session. The Linux systemd policy is a reference configuration requiring kernel support and deployment-specific native negative tests. Native Windows generated-code isolation was not demonstrated. Ubuntu WSL2 nested bubblewrap probes now pass; the real-model calculation failure is recorded below. Browser content policy does not attest the host's other applications.

## Remaining release gates

1. Native zero-egress tests and packet captures on every supported production deployment profile.
2. Broader isolated code-generation/test execution, fault recovery and adversarial workspace/tool/prompt-injection review; one synthetic generated-code case now passes.
3. Local embedding, reranker and vision/OCR integration with held-out industrial documents and scanned drawings.
4. Repeated real-model task evaluation: unknown evidence, conflicting revisions, safety-critical false acceptance, large files and recovery after interruption.
5. Measured GPU memory/offload profiles, cold/warm latency, peak RAM/VRAM and resource admission under load.
6. Authenticated multi-user isolation, authorization-aware retrieval, secrets/retention/backup policy and dependency supply-chain review before enterprise rollout.
7. An authoritative SIH statement check for the exact parameter cap, followed by requirement-by-requirement evidence.
8. A fair commercial-tool comparison. There is currently no evidence supporting “better than Codex/Claude Code.”

**Recommendation:** demonstrate the tested subset and its failure handling clearly. Do not present this report as a production security certification or the synthetic fixture as operating advice.

## BlackBox interface and permissions pass

**Established fact:** the earlier interface regression run passed 299 tests with 5 skipped. New coverage checks task-bound approvals, invalid/stale replies, cancellation cleanup, and exact-operation grant scope. A real browser workflow separately approved its plan and file write, restored a pending approval after refresh, and produced the exact requested file. See [UI permission evidence](validation/blackbox-ui-permissions.json). The updated TypeScript build, Python lint, and type checks passed.

**Still incomplete:** external-folder capability grants, full rebrand compatibility migration, broader interaction/accessibility validation, the remaining recovery/isolation release gates and the comparative held-out quality benchmark. The [active release plan](BLACKBOX_RELEASE_PLAN.md) preserves the full objective.


## Interrupted-task recovery — 21 September 2026

**Established fact:** twelve additional automated cases cover startup reconciliation, duplicate-resume rejection, cumulative budgets and model pin restoration, fresh plan approvals, stale/wrong-run approval rejection, uncertain-operation blocking, missing/corrupt result records, conflicting operation keys and the recovery API. The existing scripted crash/resume test still passes. The full suite result above includes these tests.

The browser was also checked against a separate local synthetic preview instance: a safe interrupted task displayed an explicit Resume task button; an uncertain file write displayed the exact affected operation with no resume button; refreshing restored the recovery card. This preview was stopped afterwards. No real-model inference or confidential data was used for that browser check. The original service was not restarted: automatic approval review had previously rejected that restart with only “blocked by policy.” The new backend is saved and tested but is not loaded in that existing process.

**Limits:** this is single-conductor recovery, not distributed ownership or exactly-once execution across processes. A killed inference request may consume unreported tokens; elapsed time since the last accounting checkpoint can be lost on abrupt process death. Approval waiting time is still included in elapsed execution time. Completed output/source revisions are not yet revalidated on resume. The subsequent durable-event pass persists run-scoped activity frames in SQLite and replays them in ordered pages after restart. Global non-run events remain in memory. Crash-window accounting limitations still apply. Uncertain writes remain blocked with no automatic or blind acknowledgement override. No strict network-isolation claim follows from these tests.

## Strict Linux runtime and durable events — 21 September 2026

**Established fact:** Ubuntu WSL2 kernel `6.18.33.1-microsoft-standard-WSL2`, Python 3.12 and official llama.cpp b11048 CPU were exercised. The Linux runtime archive SHA-256 was `519f67ef2edd1dfb9217af1641b5eea8db3d26691d71400b124092cf1a331ecf`. Parent and child checks observed only loopback, zero effective capabilities, NoNewPrivs, no external IPv4 routes, and native kernel denials for IPv4/IPv6 public and metadata TCP/UDP probes. [Namespace evidence](validation/strict-linux-namespace.json).

The real 1.5B exact-file run `b07de9266e2844fca26d23e826af210f` completed in **32.2 seconds**, using **678 tokens**, with independently verified exact output bytes. [Run evidence](validation/strict-linux-exact.json). A separate nested sandbox probe passed **7 checks**, including arithmetic, file output, outside-file isolation, public IPv4/IPv6, metadata and DNS denial. [Sandbox evidence](validation/strict-linux-sandbox.json). Packet capture was not performed; these results are not a security certification or universal host guarantee. The current strict profile does not expose GPU devices.

**Established fact:** run events are saved before live broadcast and replayed by sequence after restart, including histories larger than one page. Automated checks cover restart replay, pagination and persistence failure. One OS-held coordinator lock protects initialization/migrations and active lifecycle/reconciliation; distributed deployments remain open. Draft refresh and bounded text preview were checked in a separate browser instance using synthetic content.

## Generated-code failure and corrective checks

**Established fact:** run `e629a04cfcb04ba2b164d40b1833a63d` used real 1.5B inference in the strict runtime and failed the sum-of-squares case: **313.7 seconds**, **40,167 tokens**, **20 tool calls**, terminal `done_with_gaps`. It invented an input file, expanded intermediate steps into separate deliverables, and produced invalid/incorrect final output. [Preserved failure](validation/strict-code-failed-plan.json). This is a failed task, regardless of successful individual tool calls.

Corrective changes consolidate one explicitly requested output instead of treating invented scratch files as user requirements, route an explicit leading Python-tool request to the coder, and require persisted task-scoped execution evidence. Missing, failed, timed-out, corrupt, other-run and other-task records fail the check. The reviewer now receives execution arguments and results as untrusted evidence. A successful process exit proves execution only; it does not prove the formula or output is correct. Natural-language execution detection is deliberately narrow and does not cover every paraphrase. General contract extraction and independent numeric validation remain release work.

The second 1.5B run `129acf21c7eb4f3e8da27553ae778ad2` produced independently correct JSON and recorded successful bubblewrap Python execution, but continued modifying its output until a loop guard stopped it: **141.9 seconds**, **18,594 tokens**, terminal `done_with_gaps`. Overall **failed**, despite correct final bytes. [Preserved loop failure](validation/strict-code-failed-loop.json).

The executor now hands freshly generated, bounded, valid JSON outputs to normal verification once a required Python call produces all named outputs. It does not mark them correct or bypass review. A scripted integration case rejects an incorrect numeric artifact at review, repairs it, and only then completes. This behavior is limited to these explicit output/execution contracts; it is not a general proof of task completion.

The next pinned 1.5B run `ef8d6a38e3bb48f78e75080f9032cc37` reached verification, but its reviewer repeatedly marked observed execution unverifiable and repairs eventually changed the correct formula to an incorrect one: **166.5 seconds**, **14,588 tokens**, `done_with_gaps`. [Preserved review failure](validation/strict-code-failed-review.json). A 1.5B model is not established as a reliable industrial reviewer.

Automatic-routing run `69e94bed18c244128fdf77905ad146ce` generated correct code and output, but failed because a process-wide child-resource counter incorrectly charged earlier model-engine CPU time to the sandbox: a **0.292-second** code call was charged **1,329.4 CPU-seconds**. [Preserved accounting failure](validation/strict-code-failed-accounting.json). This was a harness defect, not actual sandbox resource consumption.

The Linux backend now launches a dedicated resource-meter parent for each command. Only that parent's child usage is recorded; its reporting descriptor is closed in the untrusted command. A native Linux unittest passed after deliberately running an unrelated CPU-heavy child first. Timeout enforcement also now covers a child that closes both output streams before sleeping. Unknown measurements after forced termination remain unknown; CPU/memory figures in earlier reports affected by the shared counter must not be treated as per-command measurements.

## Passing isolated code workflow — 21 September 2026

**Established fact:** automatic-routing run `1a7ddc9e30ed428e9692c1ce9b0ba14f` completed with terminal `done`, **244.0 seconds**, **2,321 tokens** (1,855 input + 466 output), and **one Python tool call**. The independent evaluator verified JSON validity, exact integer **385**, and a persisted successful bubblewrap execution record. No gaps were reported. [Passing evidence](validation/strict-code-passed.json).

The command's measured CPU time was **0.052567 seconds**, wall time **0.483 seconds**, and peak RSS **11.09375 MiB**. These are command-tree measurements, not the application/model total. The task includes model loading and switching; the run shared the host with other processes, including regression tests. It is not a controlled latency baseline or a performance promise.

The passing code run used ordinary evidence-based review after automatic execution handoff. The external expected value was used only by the evaluator, not supplied to the worker as its answer. All preceding planning, looping, review and accounting failures remain above. These development fixtures do not establish general industrial reliability or superiority to commercial agents.

Recorded routes used the installed **4B model for planning and review**, and the **1.5B model for code execution decisions**. [Routing records](validation/strict-code-routing.json). This is measured role selection; it does not prove these choices are optimal for other tasks. The [source hash manifest](validation/strict-source-manifest.json) identifies the working-tree source used for this pass; the base commit alone excludes these uncommitted changes.
