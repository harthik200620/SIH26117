# BlackBox: correctness upgrade and competition evidence

29 September 2026. Status: **development validation in progress**.

**Established fact:** no implementation can guarantee a competition result. The official Round 2 format, deadline and upload requirements remain unverified. A fresh request to the primary SIH page returned HTTP 403. The existing [requirement map](SIH26117_REQUIREMENTS.md) remains a provisional engineering baseline.

## Changes in this pass

**Established fact:** CSV-backed spreadsheet rendering now reads original records directly. The model specifies which source columns are numeric, the calculation expressions, and the decision text. It cannot substitute its own rows through the `csv_table` request. The workbook keeps source order and identifiers, produces cell-reference formulas, and records the source-byte hash. Existing workspace restrictions, static formula checks and revision snapshots still apply.

**Established fact:** the loader rejects malformed rows, ambiguous headers, unsupported numeric formats, missing numeric values and excessive precision instead of inventing values. Literal source text beginning with `=` remains text. The [validation contract](DELIVERABLE_VALIDATION.md) lists the exact bounds and limitations.

**Established fact:** compact verification runs every hard check, then skips reviewer inference if the candidate already fails. The rejection retains the complete hard-check repair list. A candidate that passes hard checks still receives semantic review.

**Established fact:** the compact planner now removes explicitly read-only source paths from its proposed output list. The regression reproduces a planner incorrectly proposing to produce `offers.csv`, `rules.pdf` and the requested workbook. The corrected plan has one workbook task. An explicit read-then-edit request still retains its requested output, and multiple deliverables retain their dependencies.

**Established fact:** full-project type checking passes across 155 files. Typed table schemas preserve text-only report tables and numeric spreadsheet tables without an incompatible field override. Optional PDF-library typing, SQLite writer typing, a formula visitor variable, and slide-parser scope were corrected. Server lint passes.

**Established fact:** the latest full server suite passed **460 tests, six skipped**, in **164.86 seconds**. The skipped tests are not passes. See the [full test log](validation/source-binding-2026-09-29-review-tests.txt) and [type-check result](validation/source-binding-2026-09-29-types.txt). One upstream Starlette test-client deprecation warning remains.

**Established fact:** the bounded calculation language now accepts `IF`, `AND`, `OR`, `NOT` and Python conditional expressions. Comparison normalization preserves equals signs inside quoted text. Arbitrary function calls remain rejected; this expands safe expression syntax without deciding the correct business rule for the model.

**Established fact:** after the conditional-expression trial, the reviewer was given scoped, bounded source-read records and actual tool-routing records. Reads of output files and paths outside the workspace are excluded from source evidence. Its instructions now distinguish execution evidence from requested document content and require concrete evidence for selections, labels and approval statements. Seven focused source-record tests and two additional decimal-precision cases were added. The precision check inspects original decimal digits rather than a context-rounded normalization.

**Established fact:** the plain-language trial exposed PDF storage bytes being presented as text lines. `read_file` now rejects PDF, DOCX and PPTX with a concrete `read_pages` instruction. Five regression cases check document rejection, uppercase extensions and unchanged ordinary-text reading. This prevents the observed line-count confusion at the text-reader boundary; it does not prove subsequent model reasoning.

**Established fact:** the candidate's first attempt failed on an overlong display title before using tools. Compact planning now truncates only the displayed title to 100 characters; the complete task instruction and output path remain unchanged. All eight compact-planning tests passed after this change. The preceding 460-test full run did not include this additional regression.

The live evaluator also accepts `--prompt-style plain --variant reordered` for tender trials. This asks for the same work in ordinary language and reverses source columns and record order. The transformation is recorded in the report and source hashes. It is a development perturbation, not an unseen benchmark or an independent review.

## Retained live evidence

| Run | Observation | Verdict |
|---|---|---|
| [5a3cc6c1](../data/90_evaluation/live_workflows/5a3cc6c1580946b99120d6410e32aad9/evaluation.json) | Prior safeguards produced wrong static totals and zero formulas; operator cancelled during review at 385.46 seconds. Sources and recorded implementation hashes were unchanged. | Failed |
| [97d7b337](../data/90_evaluation/live_workflows/97d7b3376f5842b98158ea78109b36d9/evaluation.json) | New rendering guidance exposed a three-task plan that treated inputs as outputs. Cancelled at 123.53 seconds after one source read. Rendering was not reached. | Incomplete; planner fixed afterward |
| [a18b21e5](../data/90_evaluation/live_workflows/a18b21e52511445fa9016855a49b8b6a/evaluation.json) | Correct single-task plan and direct CSV loading. Two render attempts failed on unsupported conditional syntax; repair also changed a correct tax expression into an incorrect one. Cancelled at 431.59 seconds. | Failed; bounded conditional support added afterward |
| [987cd01f](../data/90_evaluation/live_workflows/987cd01fa6f3481098bbe9b271cbd5e3/evaluation.json) | Two real workbook revisions. Separate engine recalculation confirmed 580,560 / 546,340 / 580,560 INR. Final decision text repeated the instruction; two reviews missed substantive omissions and requested tool-schema content inside the workbook. Cancelled at 617.67 seconds. | Failed; source-record review support added afterward |
| [c72fbc5b](../data/90_evaluation/live_workflows/c72fbc5b30894ceab4d0977ee77948a2/evaluation.json) | Plain request, reversed rows/columns. Model read raw PDF text, mistook its 80 text lines for pages and repeated an out-of-range page request. No workbook or review; cancelled at 521.34 seconds. | Failed; document-reader guidance added afterward |
| [b2f056a8](../data/90_evaluation/live_workflows/b2f056a8317342728b424990f843660e/evaluation.json) | First candidate-weight trial failed task-title schema validation in 92.24 seconds before any tool use. | Incomplete; display-title handling corrected afterward |
| [121cbd7c](../data/90_evaluation/live_workflows/121cbd7c0afd4b02b849b289fe226bb8/evaluation.json) | Candidate completed in 494.52 seconds, with correct separately recalculated totals and source citations. Delivery remained text; the decision did not name the preferred offer. Model review falsely accepted a column as tie-break logic and missed the selection requirement. | Failed despite application done; CSV operand-type validation and original-objective review added afterward |

The failed files were not edited into successful examples. These are synthetic development cases, not an independent or unseen benchmark. Application completion and evaluator correctness are separate measurements.

## Release decision

**Established fact:** a separate Qwen3-4B-Instruct-2507 Q4_K_M candidate was provisioned from the [LM Studio community conversion](https://huggingface.co/lmstudio-community/Qwen3-4B-Instruct-2507-GGUF), with a pinned upstream revision and matching SHA-256. [Weight provenance](validation/qwen3-4b-instruct-2507-provenance.json). The [original publisher](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507) describes instruction-following improvements; that is a publisher claim, not our measured industrial result. The candidate remains separate from existing weights. Trials use 8,192-token serving context, the same CPU and the same source fixture. The PDF-reader fix was added between the old-model reordered trial and the candidate trial, so these are not an isolated causal comparison of weights.

**Informed inference:** direct source loading is a stronger division of responsibility than asking a small model to retype tables. It prevents that copying error within the new rendering path and reduces generated output, but it cannot prove that an equation or recommendation is correct. Unstructured sources still need extraction and validation. CSV imports are deliberately bounded; larger datasets need a separate ingestion strategy.

**Established fact:** successful repeated industrial workflows, unseen variants, independent expert review, actual drawing understanding, final-machine measurements, a clean second-machine installation and official submission compliance remain separate gates. A passing software suite must not be presented as a model accuracy percentage.

**Informed inference:** a competition demonstration should use only workflows with complete, inspected evidence and measured duration. Present a failure honestly when a live task fails. A recorded successful run may be shown only with its date, hardware and recorded status clearly identified.
