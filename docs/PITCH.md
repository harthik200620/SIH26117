# YANTRA — judge demo script (SIH26117, MRPL)

> **SUPERSEDED, 28 September 2026. Do not present this historical script.** Its mock/sidecar results and broad security/timing claims do not establish real-model performance. Use [the current evidence-led script](PITCH_CURRENT.md) and [requirements audit](SIH26117_REQUIREMENTS.md).

One line: **a sealed, on-premise agentic AI workbench for refinery engineering** — plans,
executes, verifies and renders cited deliverables from plant documents, with zero egress,
on open-weight models under 120B.

Total stage time: **3 minutes** talking + one long-running scenario continuing in the
background. Timings below are measured; `standard`-profile times are targets for the GPU
host and marked (t). Run everything with:

```bash
bash scripts/demo.sh
```

## 0:00 — The claim (say it over the seal proof)

"Nothing leaves this machine. Not telemetry, not embeddings, not a DNS lookup."

```bash
yantra seal verify && yantra seal demo
```

- `seal verify` — 5 checks + Ed25519-signed certificate (measured: ~4 s).
- `seal demo` — a live outbound attempt from inside the agent sandbox; it is **blocked and
  audit-logged** on screen (measured: <1 s). Point at the `blocked_connect` line.

## 0:45 — Pump reliability investigation (start it, let it run)

"Why does P-3101A keep failing?" — the flagship scenario: logs → MTBF chart → cited DOCX
root-cause report → XLSX register → work-order draft, with automatic retry visible in the
task tree.

```bash
yantra run "Investigate why pump P-3101A keeps failing; produce a cited root-cause report, an equipment register and a work-order draft." --collections demo
```

- Mock profile (this laptop): ~8 s for the full 5-task DAG, real chart/DOCX/XLSX out.
- `standard` (t): 4–6 min, 7 tasks, 40–70 steps — start it now, narrate other scenarios,
  return to it at the end (or show the pre-recorded run if time is short).

## 1:15 — P&ID review (the vision showpiece)

"Review P&ID 3-1201 Rev C against our checklists." The rule engine finds the **three planted
design deviations** (missing check valve on P-3101B, FCV-3201 without bypass, V-3110 without
a PSV) — show the eval line proving 3/3:

```bash
yantra eval run pid
```

Measured: <1 s (ground-truth sidecar); detector inference path is the same code with the
RF-DETR weights on the GPU host.

## 1:45 — Code modernisation + consolidation (two quick wins)

```bash
yantra eval run tasks
```

All four scenarios pass end-to-end (measured: ~8 s total on this box): the legacy
`tank_gauging.py` cm→m unit bug is fixed, typed, CLI'd and **proved in the no-network
sandbox**; the inspection consolidation **fans out with `delegate`** and merges into an
XLSX register plus follow-up email drafts.

## 2:15 — Retrieval quality + extensibility

```bash
yantra eval run retrieval        # recall@10 = 1.00 lexical on the truth set (11 planted facts)
yantra models list && yantra agents list
```

"Drop a new GGUF in `models/`, `yantra models add`, and the router uses it. Add
`agents/hse_auditor.yaml` and the planner can staff it — no code changes."

## 2:45 — Crash and resume (close on trust)

"Kill -9 the server mid-run; `yantra resume` completes the run with no duplicated files —
crash-only design, checkpointed ledger, idempotent tools." Backed by property-style tests
(kill at every scheduler transition): `yantra eval run resilience`.

Close: "Sealed. Cited. Verified. Yours." — return to the pump run's finished task tree and
open `root_cause_report.docx`.

## If a judge asks

- **"How do we know it didn't hallucinate?"** — every claim carries a `[[c:chunk]]` citation
  resolved to *document, revision, page*; the verifier gates on citation coverage and an
  independent reviewer pass; unverified claims are listed as such in the appendix.
- **"What hardware?"** — `lite`: 1×24 GB GPU; `standard`: 2×48 GB; `refinery`: 4×80 GB.
  Everything <120B parameters, open weights, servable by vLLM/llama.cpp.
- **"What reaches the internet?"** — nothing: no-egress is enforced at four layers (build
  hermeticity, env lock, process socket guard, host firewall/compose `internal: true`) and
  *proved* by `yantra seal verify`, not asserted.
