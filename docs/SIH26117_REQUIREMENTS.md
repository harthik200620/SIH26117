# SIH26117: requirements and evidence

Reviewed 28 September 2026. Status: **development build; not release-ready**.

## Source authority

**Established fact, 30 September 2026:** the [official SIH 2026 problem-statement page](https://sih.gov.in/sih2026PS) was accessible in this review. Entry 26117 is MRPL's sovereign on-premise industrial AI workbench. It specifies local multi-model routing, extensible model support, tools and iterative work, document grounding, multimodal input, real deliverables, sandboxed coding, and visible network evidence. Smaller models are explicitly allowed for the venue demonstration; the 120B wording is a hardware example. This confirms the engineering baseline below. It does not establish implementation success or confirm a separate Round 2 notice.

The following source-access notes describe the earlier review:

**Established fact:** [the SIH primary page](https://sih.gov.in/sih2026PS) and homepage returned HTTP 403 during verification. No authoritative 2026 Round 2 notice or official template has been supplied. Do not call a community template official, copy a deadline from a mirror, or apply a 2024 guideline to 2026.

**Established fact:** [this community archive](https://sih2026.vuce.in/ps/SIH26117) reproduces the MRPL statement and labels itself unofficial; [SIH Buddy](https://www.sihbuddy.in/ps/SIH26117) provides secondary interpretation. The archive's dataset link is broken. Its 120B-class wording describes a hardware example, not an unambiguous hard parameter ceiling. Our below-120B policy is conservative product policy pending primary confirmation.

**Informed inference:** use the reproduced statement as a provisional engineering baseline. The acceptance tests below are proposed engineering gates, not official judging weights or numerical SIH thresholds.

## Requirement-to-proof map

| Provisional requirement | Evidence in this workspace | Missing proof / acceptance gate |
|---|---|---|
| Local deployment and confidentiality | Strict CPU Linux namespace, native child probes and a 45-second packet sample; local inference | Repeat on venue hardware and across complete workflows. GPU profile is not attested. |
| Multiple open-weight models and automatic routing | Registry, capability router and local weights | Demonstrate two task types selecting appropriate actual models without manual pinning; log IDs and reasons. Simultaneous residency needs a memory demonstration. |
| Future models without redesign | Registry/configuration interfaces | Add a provisioned compatible model and run a smoke task; retain license, hash and metadata. |
| Multi-step planning, tools and iteration | Planner/executor/verifier, typed tools, bounded repair, durable activity | Repeat successful source-only industrial tasks; show a deliberate error corrected or rejected. |
| Local document grounding | Recovered lexical index and source locators | Independent questions, revision conflicts, unknown answers, source completeness and held-out evaluation. |
| Scans, handwriting, drawings and photographs | Local OCR and vision interfaces | OCR is not drawing understanding. Run unseen samples with actual vision weights, no truth sidecars. |
| Office files and calculations | Renderers, provenance, numeric spreadsheet cells | Correct contents, arithmetic, citations and layout; recalculate Excel in a real engine. |
| Isolated, verified code execution | Bubblewrap probes and execution records | Source-only code task must execute and pass independent numeric and negative-input checks. |
| Visible no-external-call evidence | Strict launcher and network probes | Show boundary, failed native outbound probes and monitored successful local work together. A badge is insufficient. |

## Current measured position

**Established fact:** recovery evaluation reports 153 indexed documents and 147,622 child chunks with zero indexing errors. It found at least one expected source in the first ten results for 73/74 known questions, but all expected sources for only 19/74. This is development retrieval evidence, not answer accuracy. See `data/00_catalog/recovered_workbench_validation.json`.

**Established fact:** the first ten captured industrial attempts did not establish end-to-end success. One application status of `done` was false acceptance: its spreadsheet contained six `#NAME?` errors and omitted required decision evidence. Scan arithmetic and generated Python also failed. Raw outputs remain in `data/90_evaluation/live_workflows`; check the latest report for subsequent attempts.

**Established fact:** independent review remains pending. No human expert pass rate, time-saving percentage, financial ROI, production certification or commercial-tool superiority has been established.

**Established fact:** [network evidence](validation/network-2026-09-28/namespace-headers-20260928T134140Z.json) recorded 1,168 IP packets, zero non-loopback IP packets and zero reported capture drops in 45.135 seconds during live tender run `ff069c6907ba41fea5598ca582c0cb85`. A native outbound connection returned errno 101 (network unreachable). The diagnostic observed only the strict workload's loopback-only namespace; it saved IP headers, not payloads. This is not host-wide or permanent certification.

## Priorities and release gates

**Recommendation / informed inference:** use this order. A polished interface does not compensate for a wrong engineering result.

1. **P0 — correctness and honest state:** valid artifacts, correct arithmetic, source completeness, executed tests and hard failures that the model cannot override. Static workbook validation is not formula recalculation.
2. **P0 — demonstrated PS coverage:** two automatic model routes, scan-to-DOCX, executed coding and monitored local operation. Retain original inputs and full evidence.
3. **P1 — generalization:** freeze unseen cases before tuning. Proposed development gate: ten variants for each of three workflows, every required output correct; every deliberately corrupted artifact rejected. Report all failures. Passing 30 cases is not a reliability guarantee.
4. **P1 — usability:** citations resolve to the right page; Office files open correctly; no clipped tables; keyboard/cancellation/reconnect tested. Error states identify unresolved work.
5. **P1 — reproducibility:** offline installation on a second clean host, pinned artifacts and licenses, recorded peak memory and cold/warm timings. CPU measurements do not establish GPU performance.
6. **P2 — breadth and scale:** handwriting/drawing fidelity, larger-model quality, full calculation-engine support, multi-user authorization and corpus scaling, each with separate evidence.

Keep general tool use. Do not hard-code benchmark answers, mount answer keys in the model workspace, or repair evaluation artifacts after a run and call them model success.

## Trade-offs

**Informed inference:** smaller models reduce memory but currently cost correctness and repeated attempts. Deterministic tools reduce arithmetic and format failures; they cannot fix misunderstood source values or prove that an equation applies. Larger models must earn their memory/latency cost through equal-task comparison.

**Informed inference:** strict CPU operation has a tested isolation boundary but limited speed. GPU operation needs its own validation. Multiple resident models may reduce switching latency while increasing memory pressure; serial inference is not concurrent serving.

**Informed inference:** repeatable correctness and transparent rejection are a stronger competitive position than unmeasured superiority claims. Winning cannot be inferred from a feature list.
