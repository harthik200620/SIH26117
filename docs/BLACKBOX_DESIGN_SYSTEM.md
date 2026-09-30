# BlackBox Workspace interface

## Direction

The workspace is a place to do work, not a dashboard of model internals. Use a neutral light surface, one main conversation column, a compact navigation/history rail, and an optional detail panel. Keep the task, current action and next required user decision visible. Show actual recorded events; never fabricate agent activity.

The implementation is in `web/src/workbench.css`, `blackbox.css`, `navigation.ts`, `App.tsx` and the page components. The September 30 consolidation applies shared navigation, typography, controls and responsive layouts to the secondary pages. This is an evolving design system, not a completed accessibility audit.

## Tokens

| Role | Value / intent |
|---|---|
| Main surface | `#ffffff` |
| Navigation surface | `#f7f7f4` |
| Primary text | `#20211f` |
| Primary action | Dark neutral; white foreground |
| Quiet borders | `#e3e3df` |
| Successful check | Muted green, accompanied by text/icon |
| Permission request | Warm tinted surface, clear explanation and two distinct actions |
| Body type | Local system font; no font downloads |
| Conversation text | 14–15 px, generous line height |
| Radius | 8–12 px controls; 18 px composer/message surface |

## Interaction contracts

- Task history remains reachable while a task executes. Navigation changes the view; it does not grant permission or cancel a run.
- The selected task ID survives a refresh in session storage. Task content and events reload from the local service.
- Pending approvals are checked against live server state. Replayed requests that have already been resolved are hidden. A failed/stale approval response is an error, never a success state.
- Permission cards show the operation and its arguments. Plan approval includes the actual planned steps. Argument previews must not silently omit part of the authorized action.
- Details are optional. The main column should remain usable without model counters, filenames and execution diagnostics competing for attention.
- Task age derives from persisted creation time; refresh must not reset it. Waiting time and inference time still need separate reporting.
- “Application guard” and “OS isolation verified” are different states. No unverified green guarantee badge.

## Components and outstanding states

| Component | Implemented | Remaining verification/work |
|---|---|---|
| Navigation/history | Current page, task list, live marker | Search, grouping, accessible selected-task state |
| Composer | Model/mode selection, disabled submit, stop, tab-local draft recovery, IME-safe Enter | Attachments, follow-up turns, keyboard shortcuts |
| Activity | Tool success/error, progress, validation, retry | Event batching, long-run virtualization, elapsed waiting state |
| Approval | Allow once/decline, plan/details, stale-request error | Folder capabilities, revocation, file diffs, durable decision history |
| Detail panel | Toggle, plan, files, source search, usage, bounded text preview | File diffs and change review |
| Responsive layout | CSS breakpoints, overlay detail panel | Device-width/browser and keyboard testing |

## Verified behavior in this pass

The real local-model task `7515c11837064ec29fbf55538e06d2aa` paused for plan approval and separately for one file write. The file was absent before approval. The pending file approval survived a browser refresh; the already-approved plan did not reappear as actionable. Approval produced the exact 18-byte text `BlackBox Workspace`, with a successful deterministic content check and download link. See [the captured evidence](validation/blackbox-ui-permissions.json).

Do not infer general model quality, screen-reader conformance, folder-access security, or network isolation from this UI test.


## Recovery interaction

An interrupted task shows a recovery card. A safe checkpoint offers an explicit Resume task action and explains that approval requests renew. If an operation has an unconfirmed outcome, the card shows its arguments and blocks automatic continuation. The user must inspect the affected files before new work. This is not a success state. The card and selected task were visually checked on synthetic records, including after refresh; see the validation report for scope and limitations.

## Draft and preview verification — 21 September 2026

**Established fact:** a separate synthetic preview instance preserved the composer text after reload. Selecting a workspace file showed its text, including literal script tags without executing them, and the close control worked. Preview reads are limited to 100,000 bytes and reject hidden paths, out-of-workspace paths and binary content. Drafts use this browser tab’s session storage; they are not server-side backups or a cross-device feature. The preview instance was stopped after the check.


## PS26117 navigation consolidation — 30 September 2026

**Established fact:** the official [PS26117 entry](https://sih.gov.in/sih2026PS) was consulted for this interface pass. Eight primary destinations are now four:

| Primary section | Included views | Purpose |
|---|---|---|
| Workspace | Assistant, task history | Conversation, local work, outputs, and resume |
| Documents | Files and source search | Browse, preview, search, and reference local evidence |
| Assurance | Network/privacy, activity log, evaluations | Keep privacy and verification evidence accessible |
| Settings | Models/routing, runtime/logs | Operate the local installation |

Legacy hash links map to the corresponding consolidated destination. The sidebar shows six recent tasks and links to searchable history. The second task composer and duplicate recent-task panel were removed. History prioritizes outcomes; execution traces are expandable. Models prioritize installed weights and routing, while discovery, provisioning guidance, and benchmarks are expandable. Disk discovery runs on demand rather than every 15 seconds. Advanced configuration is also expandable.

The revised shell and shared surfaces use `web/src/workbench.css`, local SVG icons, and system fonts. Source previews remain bounded UTF-8 previews; binary preview support was not added. Document references populate a draft and preserve the chosen workspace without sending a task. UI simplification does not change permissions, confinement, or model quality.

**Informed inference:** keeping daily work in two primary sections and operational evidence in two supporting sections is a better fit for industrial users than eight peer-level diagnostic pages. It is a design decision, not an official SIH judging criterion or proof of superiority.
