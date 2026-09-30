# BlackBox upgrade and readiness assessment

28 September 2026; updated 29 September 2026.

The latest implementation, regression results and retained live trials are in [the 29 September correctness report](READINESS_2026-09-29.md). The measurements below retain the earlier chronology; they are not the latest test counts.

**Informed inference:** the product is materially safer to evaluate, but reliable industrial task completion has not been established. It is not defensible to call it a winning or finished entry yet.

## Changes delivered

**Established fact:** document creation now validates table shape, matches output extensions, constrains embedded image reads to the workspace and stages rendering before replacement. Spreadsheet files retain numeric inputs and have wrapped headers, frozen panes, filters, print settings and optional decision/evidence sheets. Markdown/PDF text conversion retains report-table values, finding evidence, assumptions and unresolved claims previously omitted.

**Established fact:** the shared static spreadsheet checker rejects broken references, missing names/sheets, circular dependencies, external workbook links and active/external functions. It has bounded work and does not expand worksheets when inspecting distant references. More useful repair feedback identifies row cell addresses. Final review checks declared and additionally reported Office artifacts; an optimistic model review cannot override a failed hard check.

**Established fact:** the workbench has a local download for a task's model routes, verification attempts and execution evidence. Failed attempts remain visible. Export limits and confidentiality are explicit. This is an application record, not independent certification.

**Established fact:** spreadsheet calculations can now use named input columns through a restricted expression compiler. The renderer generates the actual Excel references and keeps the original rule in header comments. Unsupported syntax and ambiguous or forward references fail before rendering. Compact execution hands a freshly rendered sole deliverable to normal verification immediately, avoiding repeated rewrites without review. Neither mechanism proves that the model chose the correct equation.

**Established fact:** the old pitch is marked superseded. A sourced requirements map, a current demo script and a Round 2 preparation brief separate actual evidence from proposed acceptance targets. Offline provisioning instructions now include analysis and OCR dependencies.

**Established fact:** the access screen now waits for authentication checking before mounting the workspace, suspends polling while locked and prevents duplicate unlock submissions. Navigation has an active-page announcement and a skip link; entry-screen contrast and keyboard focus were improved. The production build passed and the locked screen's keyboard order was inspected in the actual browser. [Bounded UI review](validation/access-screen-2026-09-28.md); full accessibility validation remains pending.

## Measured outcome

**Established fact:** source-only tender trial `ff069c69` using the actual local 4B quantized model read both inputs, then repeated an undefined-formula error three times. Each render attempt was rejected before delivery. No workbook was produced. The operator cancelled after 708.89 seconds; input hashes were unchanged. This proves rejection of the observed error in that run, not successful repair. Cell-address feedback was added afterward and was therefore absent from that particular trial.

See [the captured attempt](../data/90_evaluation/live_workflows/ff069c6907ba41fea5598ca582c0cb85/run.json), [inspection verdict](../data/90_evaluation/live_workflows/ff069c6907ba41fea5598ca582c0cb85/evaluation.json), and [the complete attempt history](../data/LIVE_WORKFLOW_REPORT.md). All are development evidence, not an unseen independent benchmark.

**Established fact:** subsequent run `59adcc74` produced real workbooks but timed out at 900.68 seconds. Later rewrites changed correct landed formulas into `basic + freight * (1 + tax)` and removed the decision/evidence sheet. Separate recalculation returned 494,160 / 466,240 / 492,000 INR against expected 580,560 / 546,340 / 580,560 INR. Source bytes and recorded implementation hashes were unchanged during that run. [Inspection and verdict](../data/90_evaluation/live_workflows/59adcc74080a41eb963d64ad7bf64fe1/evaluation.json). The named-column compiler and early review handoff were added afterward; a subsequent live trial is being evaluated separately.

**Established fact:** run `39010d70` completed at 387.04 seconds after one render and one review. Its totals were wrong static numbers; the model did not use the available computed-column compiler. The reviewer falsely asserted that formulas, citations and approval text existed. [Raw inspection and failed verdict](../data/90_evaluation/live_workflows/39010d7011b6473d9d5d83389afb9679/evaluation.json). The later hard check rejects this unchanged workbook when replayed against its explicit formula requirement. Reviewer excerpts now show actual addresses, cell types and expressions. All 28 focused review/conductor tests passed after that fix; [test log](validation/formula-review-2026-09-28-tests.txt). This does not prove general detection of incorrect decisions or citations.

**Established fact:** live run `e789d0f9` exercised the repaired formula-presence gate. Two verification attempts rejected zero-formula workbooks; a repair changed source prices and delivery periods into unsupported values. The evaluator cancelled at 902.52 seconds. [Verdict](../data/90_evaluation/live_workflows/e789d0f97bd24f55b08dc06e262d3be6/evaluation.json). This establishes rejection of this observed defect, not task success.

**Established fact:** subsequent rendering changes preserve separate local copies of each file and provenance revision, record task-scoped model IDs and expose hash-checked downloads. Forty-two focused rendering tests passed before the download integration. Original failed model files have not been repaired or relabelled as successful.

**Established fact:** the 29 September full regression suite passed **413 tests, with six skipped**, in 160.30 seconds. [Full log](validation/upgrade-2026-09-29-tests.txt). After that suite started, source-continuity changes were added to preserve bounded historical reads across retries; their two new tests and 13 conductor tests passed together. [15-test log](validation/retry-sources-2026-09-29-tests.txt). Snapshot-download integration separately passed 16 tests. These latest changes have not yet been exercised in a fresh real-model industrial run.

**Established fact:** a separate 45.135-second IP-header capture in the same runtime observed 1,168 IP packets, all loopback, with zero reported capture drops. A native external connection probe returned network-unreachable. Saved process membership lists the application and model server in that namespace. The packet-file SHA-256 matches the saved report. [Network record](validation/network-2026-09-28/namespace-headers-20260928T134140Z.json).

**Established fact:** the frontend production build and targeted lint/whitespace checks passed. The latest full server regression run passed **405 tests, with six skipped**, in 181.80 seconds; output is retained in [the test log](validation/upgrade-2026-09-28-final-tests.txt). A separate Artifact Tool engine recalculated all nine computed cells in a developer fixture to the expected values, including Boolean conditions and dependencies on earlier computed columns. [Expected values](validation/computed-columns-2026-09-28/expected-values.json), [recalculated values](validation/computed-columns-2026-09-28/recalculation/recalculated-values.json). The workbook preview was inspected. These are component tests, not model-reasoning or complete accessibility results.

## Remaining gates and trade-offs

**Informed inference:** the immediate priority is repeatable source-to-output correctness on the scan, tender and coding tasks, followed by unseen variants. The current small CPU-served model repeatedly failed to repair this spreadsheet. Compare stronger locally runnable weights under equal task/time limits when suitable hardware is available; do not assume that more personas or a larger corpus will solve it.

**Established fact:** static checks are not formula evaluation. Advanced named/structured spreadsheet references are currently rejected pending a calculation-engine validation path. Correct source interpretation, physical model selection, visual drawing understanding and human approval remain separate responsibilities. The [validation contract](DELIVERABLE_VALIDATION.md) documents these limits.

**Established fact:** independent review, automatic two-task/two-model demonstration, venue GPU isolation, full-workflow packet observations and a clean second-machine installation remain unverified.

**Established fact:** the user confirmed that final demonstration hardware is undecided. No GPU or larger model has been provisioned or assumed. The current model-quality limitation must be resolved through equal-case validation on the available hardware before promising a live demonstration.

**Established fact:** the official Round 2 notice is still missing, and the primary SIH site returned HTTP 403 during checking. Exact format, deadline and required uploads cannot be certified from conflicting community sources. Use [the submission brief](ROUND2_SUBMISSION_BRIEF.md) once the institution/organizer notice is provided. No external submission or deployment was performed.
