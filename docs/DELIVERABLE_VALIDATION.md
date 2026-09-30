# Deliverable validation contract

Updated 28 September 2026. **Established fact:** these are implementation checks, not a claim of industrial correctness.

Updated checks on 29 September include direct CSV input binding, bounded conditional expressions and CSV expression type validation.

## At creation

- Table rows must match the number of columns. Each table supports up to 10,000 rows and 128 columns; spreadsheets support up to 64 sheets. Non-finite numeric inputs are rejected.
- Output extension must match its requested format; presentation output requires the presentation schema.
- Embedded report figures, slide figures and drawing overlays are resolved through the authorized workspace boundary. Missing or out-of-workspace images fail before rendering.
- Files render in a temporary directory before replacing the output. A failed render preserves the previous artifact and provenance. File and sidecar replacement are separate operations, not a two-file atomic transaction; the sidecar hash must be checked by consumers.
- Spreadsheet header order is deterministic. Inputs retain their numeric, text and boolean types. Headers wrap; panes freeze; sheets have filters and print settings. Decision, sources, assumptions and unresolved claims have a separate evidence sheet when supplied. Visual suitability still depends on the actual data and needs inspection.

## Static spreadsheet gate

**Established fact:** `data_table` sheets may supply `computed_columns`, each with a `name` and `expression`. Rows contain only the input columns. The renderer appends formulas bound to exact input or preceding computed-column names, so column reordering does not silently change the formula's meaning. Headers retain the original rule in a comment. Example: columns `price, quantity`, row `[25, 4]`, computed column `{name: "Total", expression: "price * quantity"}` produces `=A2*B2` with explicit parentheses.

Expressions support arithmetic `+ - * /`, comparisons, Boolean logic and `col("Unit price")` for labels containing spaces. They never execute Python. Unknown, duplicated, self-referencing and forward-referencing columns fail before rendering. Limits are 1,000 expression characters, 100 syntax nodes and 128 total columns. Raw A1 formulas remain supported. The compiler prevents reference translation errors; it cannot decide whether the selected equation or source column is appropriate.

**Established fact, 29 September:** conditional expressions also support `IF(condition, yes, no)`, `AND`, `OR`, `NOT`, and Python-style conditional expressions. Equality can use `=` or `==`; token-based normalization leaves quoted `=` characters untouched. Calls with unsupported names, keywords or wrong argument counts fail. Error feedback identifies the failing computed column and expression. Supporting this syntax does not validate the selected business rule.

Creation and final review use the same checker. It rejects stored Excel errors, broken error tokens, undeclared names, unknown sheets, invalid address bounds, circular dependencies, external links and active/external formula functions. Errors give row cell references where useful for repair. The final reviewer also checks Office files reported in the task's final output list, even if the planner omitted them.

**Established fact:** an explicit positive instruction such as “Use spreadsheet formulas” or “Include Excel formulas” adds a hard presence check. A zero-formula workbook fails even if the model reviewer claims it contains formulas. The recognizer is deliberately narrow; arbitrary paraphrases or negated clauses do not activate it. Formula presence does not establish correctness or coverage of every requested calculation. Reviewer excerpts now list actual cell addresses, types and formula expressions with an explicit warning that these are not recalculated values; truncated excerpts are labelled.

The current gate accepts ordinary A1 references and bounded ranges. Named ranges, structured references, whole-column ranges and other unsupported forms require a calculation-engine validation path and are rejected rather than treated as verified. Budget: 200,000 used cells / total referenced cells; at most 20,000 cells per range. Distant blank references are checked without expanding the worksheet.

**Limit:** this is not an Excel engine. It does not calculate caches, validate every function, catch every syntax/type error, detect every divide-by-zero path or establish that an equation is appropriate. Actual engine recalculation and source/result comparison remain necessary. A saved workbook requests recalculation on opening; that request is not evidence that recalculation happened.

## Downloadable validation record

**Established fact:** CSV-backed calculations validate operand types before rendering. Arithmetic requires numeric operands; comparisons require matching scalar types. Numeric source columns must be explicitly declared, including values used only in eligibility comparisons. Conditional branches preserve possible result types, so a later calculation cannot silently multiply a text result. This deliberately rejects implicit spreadsheet text/number coercion. It does not establish that the model chose the correct rule. Compact semantic review receives the original objective rather than treating the planner's paraphrase as the acceptance scope, together with bounded recorded source reads.

The workbench exposes a task's persisted model selections, verification attempts and execution evidence as a local JSON download. It retains failures, flags truncated history and avoids exporting tool arguments or raw model thoughts. The record contains task/review content and remains confidential. It does not include source files, packet captures or a signed independent opinion.

**Established fact:** successful render calls now copy the file and its provenance sidecar into local artifact storage. Each revision records run/task/step association; model IDs come from recorded routes for that task rather than model-authored claims. Source hashes are not inferred and can remain empty. The validation record lists up to 500 revision files with hashes and local download links; excess history is flagged. Revision downloads enforce run association and verify stored byte length and SHA-256 before returning content, with a 64 MiB download limit. Previous snapshots survive subsequent workspace overwrites. This is local history, not administrator-proof immutable storage. File creation and snapshot capture are separate operations: a capture error reports a failed tool call, but the newly rendered workspace file may already exist.

**Limit:** model review can be wrong. Neither a successful tool call nor application status `done` substitutes for source-grounded verification and any required human approval.

In compact mode, a successful render of the task's sole requested Office/PDF file immediately enters the normal verification path. It is not accepted on render success. This avoids spending further generation steps rewriting a completed candidate without first obtaining review feedback. Multi-output tasks still follow normal execution until the model finishes.

## Source continuity during repair

**Established fact:** later attempts retain up to four successful `read_file`/`read_pages` observations from earlier attempts of the same run and task. Excerpts are bounded to 6,000 characters total and 3,000 per observation; truncation is explicit. Failed reads, generated-output observations, other tasks and model thoughts are excluded. The repair context labels these as historical document data, not instructions or guarantees about current file contents. Changed or incomplete sources must be read again. This preserves evidence through retries; it does not validate its interpretation or guarantee that a model follows it.

## Direct CSV inputs — 29 September 2026

**Established fact:** `render_document` accepts `schema_id: csv_table` for XLSX. Its payload contains `source_path`, explicit `numeric_columns`, named `computed_columns`, `summary`, and `appendix`. The tool resolves the source inside the workspace and loads all records in their original order. The model does not supply replacement rows. The resolved sheet then passes through the existing formula compiler, renderer, Office checks and revision capture.

The reader accepts UTF-8 comma-separated records, including quoted fields and a UTF-8 BOM. It rejects malformed records, duplicate/blank column names, nonnumeric data in declared numeric columns, nonfinite values and numbers exceeding 15 significant decimal digits. Text identifiers remain text. Source text beginning with `=` remains a literal cell, not an executable formula. Limits are 2 MB, 10,000 records, 128 total columns and 100,000 input-plus-computed cells. Unsupported encodings, locale-specific numbers, units embedded in numeric fields and missing numeric values require explicit correction. Missing data never becomes zero.

The original CSV byte hash and path are recorded in the provenance sidecar and the workbook's evidence sheet. The hash identifies the bytes used at render time; it does not prove the source is authoritative, prevent later modification, or establish that the model selected the right formula. Summary text is not recalculated when a workbook input changes; a new analysis is required after changing inputs or rules. The original failed live artifacts remain preserved.

**Established fact:** compact verification now runs all deterministic checks first. If any fail, it records a deterministic rejection and the complete repair list without a reviewer-model call. Semantically reviewing valid candidates remains mandatory. This saves a redundant inference call; a hard-check pass is not an industrial correctness verdict.
