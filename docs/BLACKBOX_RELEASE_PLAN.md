# BlackBox Workspace — active release requirements

28 September 2026: [SIH26117 requirements and evidence](SIH26117_REQUIREMENTS.md) is the current competition traceability map. [Round 2 preparation](ROUND2_SUBMISSION_BRIEF.md) records submission uncertainty. [Latest upgrade report](UPGRADE_REPORT_2026-09-28.md) records current tests, network observations, UI checks and failed real-model attempts. Recent industrial recovery runs take precedence over older mock successes; readiness remains unproven. Final demonstration hardware is undecided.

This file preserves the full requested outcome. An implemented component is not a passed release gate. The goal remains active until the gates have direct evidence.

## Product and interaction

- **In progress:** complete BlackBox Workspace rebrand, including browser, launchers, CLI, generated documents and current documentation. Internal legacy package/config names need an explicit compatibility migration; do not break installed model/state paths through blind replacement.
- **In progress:** task-first interface, persistent task history, readable activity, model selection, file preview/diffs, citations, permission review, keyboard access, small-screen layout, reconnect and error states. First interface pass is implemented; user-flow and accessibility verification remain.
- **Implemented and checked:** tab-local draft recovery after refresh and bounded plain-text file previews.
- **Open:** follow-up messages within a task, durable permission history, scoped folder grants and revocation, file-change review and approved application.

## Permission boundary

Implemented in this pass: unknown operations prompt; explicit denies remain denied; reusable tool grants bind exact arguments and run; approval replies bind the originating run; pending requests are queryable after browser reload; cancelled/expired requests cannot be resolved. Server restart must invalidate grants rather than silently authorize them.

Still required: explicit read/write capabilities for additional operator-selected folders, resource-specific approvals for unavailable tools, restricted sandbox mounts derived from grants, revoked-grant behavior, persisted request decisions and readable audit history. A permission button must never disable the sealed network boundary or enable an unsafe execution fallback.

## Strict offline deployment

Required invariant: all confidential runtime processes (application, model engines, parsers, tools and children) operate inside enforced network isolation. The provisioning process runs separately before confidential material is accessible. No runtime package/model downloads, CDN assets, telemetry, public inference, or model-generated external navigation.

Implemented: a fail-closed Linux CPU profile encloses the application, model engines and parsers in a network namespace, with a fixed Unix-socket bridge. Ubuntu WSL2 testing passed native IPv4/IPv6 TCP/UDP/metadata and child-process checks, nested sandbox probes, and a real-model exact-file task. See [strict deployment](STRICT_DEPLOYMENT.md). Still required: packet capture, adversarial review, GPU/device review and repeat validation on each supported deployment. This does not establish universal zero egress. State its kernel, administrator and browser trust assumptions. Unsupported hosts must say why strict operation is unavailable; never display a guaranteed-isolation badge from a configuration flag.

## Difficult work on smaller models

- **In progress:** startup reconciliation marks orphaned tasks interrupted; explicit resume rejects duplicate drivers, renews approvals, restores recorded usage and pinned models, and blocks uncertain operations. Missing/corrupt replay results fail closed. Durable SQLite activity history and paginated SSE replay now survive process restart; a local OS lock prevents two active coordinators sharing state. The same lock guards initialization/migrations before lifecycle startup. Remaining: distributed ownership, source/output revision validation, crash-window accounting and operator reconciliation of uncertain effects.
- Bounded task graph with acceptance contracts, role/model selection, durable evidence, source revision tracking, dependency invalidation and progress ledgers.
- Repair driven by failed checks, alternative approaches and bounded escalation. Track stalled work separately from waiting for user input; approval time must not masquerade as inference time.
- Context compaction preserving original constraints, decisions, failed attempts and unresolved issues. Retrieve evidence on demand rather than repeatedly including complete documents.
- Independent deterministic validators where possible; evidence-first review elsewhere; unresolved uncertainty remains visible. Explicit Python contracts now require recorded execution; freshly produced JSON moves to normal verification before additional editing. A repeatable code fixture checks the independent numeric answer and isolated execution. Small-model reviewer reliability remains a measured problem, not a solved feature.
- Held-out difficult tasks and repeated trials with smaller-model baselines: completion, correctness, false acceptance/rejection, recovery, latency, memory and token use. A single synthetic pump success is development evidence only.

## Upstream reuse and comparison

Established fact: the public [OpenAI Codex repository](https://github.com/openai/codex) has an [Apache-2.0 license](https://github.com/openai/codex/blob/main/LICENSE), checked on 20 September 2026. Its [sandbox boundary implementation](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/sandboxing.rs) is a primary reference. No upstream implementation was copied in this pass. Any later source reuse must pin a revision, retain notices, document modifications and validate the imported boundary.

The repository license does not establish that the entire Codex desktop product is open source. Superiority to Codex/Claude Code remains unproven. Define equal-budget nonconfidential tasks and independent graders before making that claim.

## Completion evidence

For each requirement retain source revision, configuration, test commands/results, real-model outputs, observed UI flows and isolation evidence. Mark unknown results as unknown. Do not replace this scope with the easiest passing subset.
