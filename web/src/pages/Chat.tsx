import React, { useEffect, useRef, useState } from "react";
import { getJson, postJson } from "../api.js";
import { Markdown } from "../md.js";
import { Icon } from "../Icon.js";
import { WorkspacePicker } from "../WorkspacePicker.js";
import { preferredWorkspace, type WorkspaceStatus } from "../workspace.js";
import { RpcClient } from "../rpc.js";

type Event = { method: string; params: Record<string, any> };
function savedDraft(): string { try { return sessionStorage.getItem("blackbox.draft") ?? ""; } catch { return ""; } }
const terminal = new Set(["done", "done_with_gaps", "failed", "cancelled", "planned", "interrupted"]);
const starters: [string, string, string][] = [
  ["▤", "Review an inspection", "Read inspection-P101.md and maintenance-procedure.md. Create review.md with cited observations, missing information and proposed actions. Do not invent OEM limits."],
  ["ƒ", "Calculate with evidence", "Read inspection-P101.md. Use calculate_quantity with original source values and units to compute hydraulic and shaft power. Write calculation.md showing inputs, formula, units, results, assumptions and source filenames. Mark the example synthetic."],
  ["⌘", "Build a local tool", "Write pump_power.py with a hydraulic_power_kw(flow_m3_h, head_m, density_kg_m3=1000) function. Reject negative inputs. Write tests using known reference values. Report whether tests were actually executed."],
];

export function ChatPage(): React.ReactElement {
  const [preview, setPreview] = useState<any>(null);
  const [recovery, setRecovery] = useState<any>(null);
  const [pendingIds, setPendingIds] = useState<string[] | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [status, setStatus] = useState<any>(null), [workspace, setWorkspace] = useState("");
  const [model, setModel] = useState(""), [mode, setMode] = useState("ask");
  const [input, setInput] = useState(savedDraft), [goal, setGoal] = useState("");
  const [events, setEvents] = useState<Event[]>([]), [runId, setRunId] = useState<string | null>(null);
  const [running, setRunning] = useState(false), [error, setError] = useState("");
  const [files, setFiles] = useState<any[]>([]);
  const [tab, setTab] = useState("workflow"), [query, setQuery] = useState("");
  const [hits, setHits] = useState<any[]>([]), [indexing, setIndexing] = useState(false);
  const [indexNote, setIndexNote] = useState(""), [streamState, setStreamState] = useState("idle");
  const [runStartedAt, setRunStartedAt] = useState(Date.now());
  const [elapsed, setElapsed] = useState(0), [answer, setAnswer] = useState("");
  const [resolved, setResolved] = useState<string[]>([]);
  const source = useRef<EventSource | null>(null), bottom = useRef<HTMLDivElement>(null), initialised = useRef(false);
  const currentWorkspace = useRef(workspace);
  currentWorkspace.current = workspace;
  const refresh = async () => {
    try { const s = await getJson<any>("/api/workbench/status"); setStatus(s);
      if (!initialised.current) { setWorkspace(preferredWorkspace(s)); initialised.current = true; }
    } catch (e) { setError(String(e)); }
  };
  useEffect(() => { try { if (input) sessionStorage.setItem("blackbox.draft", input); else sessionStorage.removeItem("blackbox.draft"); } catch {} }, [input]);
  const previewFile = async (path: string) => {
    try { const result = await getJson(`/api/workbench/file-preview?workspace_path=${encodeURIComponent(workspace)}&path=${encodeURIComponent(path)}`); if (currentWorkspace.current === workspace) setPreview(result); }
    catch (e) { if (currentWorkspace.current === workspace) setError(String(e)); }
  };
  useEffect(() => { setPreview(null); setHits([]); setIndexNote(""); setFiles([]); }, [workspace]);
  const refreshFiles = async () => {
    if (!workspace) return;
    try { const result = await getJson<any>(`/api/workbench/files?workspace_path=${encodeURIComponent(workspace)}`); if (currentWorkspace.current === workspace) setFiles(result.files); }
    catch (e) { if (currentWorkspace.current === workspace) setError(String(e)); }
  };
  useEffect(() => { void refresh(); const t = setInterval(refresh, 15000); return () => { clearInterval(t); source.current?.close(); }; }, []);
  useEffect(() => { void refreshFiles(); }, [workspace]);
  useEffect(() => { if (!running) return; const update = () => setElapsed(Math.max(0, Math.floor((Date.now() - runStartedAt) / 1000))); update(); const t = setInterval(update, 1000); return () => clearInterval(t); }, [running, runStartedAt]);
  useEffect(() => {
    if (events.length) bottom.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    else if (!goal) bottom.current?.closest(".conversation-scroll")?.scrollTo({ top: 0 });
  }, [events.length, goal]);
  const connect = (id: string) => {
    source.current?.close(); const stream = new EventSource(`/api/workbench/runs/${id}/events`); source.current = stream;
    setStreamState("connecting"); stream.onopen = () => setStreamState("live"); stream.onerror = () => setStreamState("reconnecting");
    stream.onmessage = message => { const event: Event = JSON.parse(message.data); setEvents(prev => [...prev, event]);
      if (event.method === "run.finished") { if (event.params.status === "interrupted") void getJson<any>(`/api/workbench/runs/${id}/recovery`).then(setRecovery).catch(e => setError(String(e))); stream.close(); setStreamState("complete"); setRunning(false); void refresh(); void refreshFiles(); }
    };
  };
  const send = async () => {
    if (!input.trim() || running) return;
    setError(""); setRecovery(null); setEvents([]); setGoal(input.trim()); setResolved([]); setElapsed(0); setRunStartedAt(Date.now()); setRunning(true);
    try { const r = await postJson<any>("/api/workbench/runs", { workspace, goal: input.trim(), model: model || null, mode }); sessionStorage.setItem("blackbox.openRun", r.run_id); setRunId(r.run_id); setInput(""); connect(r.run_id); }
    catch (e) { setError(String(e)); setRunning(false); }
  };
  const resume = async (id: string) => {
    sessionStorage.setItem("blackbox.openRun", id);
    try { const r = await getJson<any>(`/api/workbench/runs/${id}`); initialised.current = true; if (r.created_at) setRunStartedAt(Date.parse(/Z$|[+-]\d{2}:\d{2}$/.test(r.created_at) ? r.created_at : r.created_at + "Z")); setRecovery(r.status === "interrupted" ? await getJson<any>(`/api/workbench/runs/${id}/recovery`) : null); setMode(r.mode ?? "ask"); setModel(r.model ?? ""); setRunId(id); setGoal(r.goal); setWorkspace(r.workspace); setEvents([]); setResolved([]); setRunning(!terminal.has(r.status)); connect(id); }
    catch (e) { setError(String(e)); }
  };
  const continueRun = async () => {
    if (!runId) return;
    setError("");
    try {
      await postJson(`/api/workbench/runs/${runId}/resume`, {});
      setRecovery(null); setEvents([]); setResolved([]); setRunning(true); connect(runId);
    } catch (e) { setError(String(e)); }
  };
  const stop = async () => { try { if (runId) await postJson(`/api/workbench/runs/${runId}/cancel`, {}); } catch (e) { setError(String(e)); } };
  const index = async () => {
    setIndexing(true); setIndexNote("");
    try { const r = await postJson<any>("/api/workbench/index", { workspace }); setIndexNote(`${r.documents} updated · ${r.skipped} unchanged · ${r.chunks} chunks · ${r.errors} errors · ${r.seconds}s`); }
    catch (e) { setError(String(e)); } finally { setIndexing(false); }
  };
  const search = async () => {
    try { const r = await postJson<any>("/api/workbench/search", { workspace, query }); setHits(r.hits); setIndexNote(r.hits.length ? `${r.hits.length} local passages` : "No matching passages. Index this workspace first."); }
    catch (e) { setError(String(e)); }
  };
  const approve = async (requestId: string, decision: string) => {
    const client = new RpcClient(() => {}); client.connect();
    try { const response = await client.call<{resolved: boolean}>("run.approve", { run_id: runId, request_id: requestId, decision, answers: answer ? [answer] : [] }); if (!response.resolved) throw new Error("This request has expired or was already answered. Refresh the task to see its current state."); setResolved(p => [...p, requestId]); setAnswer(""); }
    catch (e) { setError(String(e)); } finally { client.close(); }
  };
  const plan = [...events].reverse().find(e => e.method === "plan.updated")?.params.plan?.tasks ?? [];
  const finished = [...events].reverse().find(e => e.method === "run.finished")?.params;
  const conversational = finished?.response_kind === "conversation";
  const liveStats = [...events].reverse().find(e => e.method === "run.stats")?.params;
  const stats = finished?.budget_used ? { ...liveStats, tokens_in: finished.budget_used.prompt_tokens, tokens_out: finished.budget_used.completion_tokens, elapsed_s: finished.budget_used.seconds_used } : liveStats;
  const taskState = (id: string) => [...events].reverse().find(e => e.method === "task.updated" && (e.params.task?.task_id ?? e.params.task?.id)?.endsWith(id))?.params.task?.status ?? "pending";

  useEffect(() => {
    const open = (event: Event) => { void resume((event as unknown as CustomEvent<string>).detail); };
    const fresh = () => { source.current?.close(); setRecovery(null); setRunId(null); setGoal(""); setInput(""); setEvents([]); setRunning(false); setError(""); if (status) setWorkspace(preferredWorkspace(status)); };
    window.addEventListener("blackbox:open-run", open as unknown as EventListener);
    window.addEventListener("blackbox:new-task", fresh);
    return () => { window.removeEventListener("blackbox:open-run", open as unknown as EventListener); window.removeEventListener("blackbox:new-task", fresh); };
  }, [status]);
  useEffect(() => {
    setPendingIds(null); if (!runId) return;
    let disposed = false;
    const poll = async () => { try { const r = await getJson<{pending_ids: string[]}>(`/api/workbench/runs/${runId}/permissions`); if (!disposed) setPendingIds(r.pending_ids); } catch {} };
    void poll(); const timer = setInterval(poll, 2500);
    return () => { disposed = true; clearInterval(timer); };
  }, [runId]);
  useEffect(() => { const id = sessionStorage.getItem("blackbox.openRun"); if (id) void resume(id); }, []);
  const changeWorkspace = (selection: WorkspaceStatus) => {
    source.current?.close(); sessionStorage.removeItem("blackbox.openRun");
    setRecovery(null); setRunId(null); setGoal(""); setEvents([]); setResolved([]); setError("");
    setWorkspace(selection.workspace); setStatus((previous: any) => ({ ...previous, roots: selection.roots }));
  };
  return <div className={`studio ${inspectorOpen ? "with-inspector" : "without-inspector"}`}>
    <header className="studio-top"><div><span className="eyebrow">BLACKBOX WORKSPACE</span><div className="studio-title">{goal ? "Current conversation" : "Assistant"}</div></div><div className="top-actions"><span className="connection-dot" /><span>{status ? "Private inference" : "Connecting"}</span><button aria-expanded={inspectorOpen} onClick={() => setInspectorOpen(v => !v)}>Task details</button></div></header>
    <div className="workspace-bar"><span>▱</span><label htmlFor="workspace">Workspace</label><input id="workspace" value={workspace} readOnly title={workspace} /><WorkspacePicker value={workspace} roots={status?.roots ?? []} disabled={running} onChange={changeWorkspace}/><button className="browse-workspace" onClick={() => { setInspectorOpen(true); setTab("files"); void refreshFiles(); }}>Browse files ↗</button></div>
    <div className="studio-body"><section className="conversation"><div className="conversation-scroll" aria-live="polite">
      {!goal && <div className="welcome"><div className="welcome-emblem" aria-hidden="true">▣</div><div className="eyebrow">YOUR LOCAL WORKSPACE</div><h1>From evidence to action.</h1><p>Ask a question, investigate your documents, or create a deliverable.<br />Your work stays on your infrastructure.</p><div className="starter-grid">{starters.map(s => <button key={s[1]} className="starter" onClick={() => setInput(s[2])}><span className="starter-icon"><Icon name={s[0] === "▤" ? "documents" : s[0] === "ƒ" ? "assurance" : "workspace"} size={21}/></span><b>{s[1]}</b><span>{s[0] === "▤" ? "Findings, sources & next steps" : s[0] === "ƒ" ? "Inputs, units & working shown" : "Code, checks & execution"} <Icon name="arrow" size={13}/></span></button>)}</div><div className="readiness"><span className={status?.ready ? "ready-indicator" : "missing-indicator"} />{!status ? "Checking local models…" : status.ready ? `${status.models.length} private model${status.models.length === 1 ? "" : "s"} available` : "Model setup required"}<span>·</span><a href="#models">Manage models →</a></div>{status?.security.code_execution === "disabled" && <div className="quiet-note">File tools, retrieval and arithmetic are available. Running generated code requires an isolated sandbox.</div>}</div>}
      {goal && <><div className="user-message"><span className="avatar">YOU</span><div>{goal}</div></div><div className="run-heading"><span className="agent-avatar">▣</span><b>BlackBox</b><span>{finished ? String(finished.status).replaceAll("_", " ") : running ? pendingIds?.length ? "Waiting for you" : "Working" : "Task"}</span>{running && <span className="working-pulse" />}</div></>}
      <div className="event-timeline">{events.map((event, i) => {
        const p = event.params;
        if (event.method === "assistant.delta") return <div className="progress-text" key={i}>{p.text}</div>;
        if (event.method === "plan.updated") return <div className="milestone" key={i}><span>✓</span> Plan created · {p.plan?.tasks?.length ?? 0} steps</div>;
        if (event.method === "tool.started") {
          const result = events.slice(i).find(e => e.method === "tool.finished" && e.params.step_id === p.step_id);
          const output = events.filter(e => e.method === "tool.output" && e.params.step_id === p.step_id).map(e => e.params.text).join("");
          return <details className={`action-card ${result?.params.ok === false ? "action-failed" : ""}`} key={i}><summary><span className="action-icon">{result ? result.params.ok ? "✓" : "!" : "◌"}</span><b>{String(p.tool).replaceAll("_", " ")}</b><span>{result ? result.params.summary : "Running…"}</span><i>⌄</i></summary><pre>{JSON.stringify(p.args, null, 2)}</pre>{output && <pre>{output}</pre>}</details>;
        }
        if (event.method === "verify.result") return <details className="verification-card" key={i}><summary>{p.report?.verdict === "pass" ? "✓" : "↻"} Validation: {p.report?.verdict} <span>{p.report?.reviewer?.method === "deterministic" ? "Exact content checks" : "Checks + reviewer"}</span></summary><div className="review-evidence">{p.report?.checks?.map((c: any, j: number) => <div key={`check-${j}`} className={c.passed ? "criterion-met" : "criterion-gap"}><b>{c.passed ? "✓" : "!"} {String(c.check?.kind ?? "Check").replaceAll("_", " ")}</b><p>{c.detail}</p></div>)}{p.report?.reviewer?.criteria?.map((c: any, j: number) => <div key={`criterion-${j}`} className={c.status === "met" ? "criterion-met" : "criterion-gap"}><b>{c.status === "met" ? "✓" : "!"} {c.requirement}</b><p>{c.evidence}</p><small>{c.status}</small></div>)}{!p.report?.reviewer?.criteria?.length && p.report?.reviewer?.failures?.map((f: any, j: number) => <div key={`failure-${j}`} className="criterion-gap"><b>{f.what}</b><p>{f.why}</p></div>)}</div><details className="review-record"><summary>Full verification record</summary><pre>{JSON.stringify(p.report, null, 2)}</pre></details></details>;
        if (event.method === "escalation") return <div className="retry-note" key={i}>↻ Revising after feedback · attempt {p.attempt}</div>;
        if (event.method === "error") return <div className="inline-error" key={i}>{p.message}</div>;
        if ((event.method === "permission.request" || event.method === "question") && !resolved.includes(p.request_id) && (pendingIds === null || pendingIds.includes(p.request_id))) return <div className="approval-card" key={i}><div className="permission-eyebrow">PERMISSION REQUEST</div><b>{p.explanation ?? "BlackBox needs your approval"}</b><p>{p.tool ?? p.questions?.join("\n")}</p>{p.tool === "plan" ? <ol className="approval-plan">{plan.map((step: any) => <li key={step.id}><b>{step.title}</b><p>{step.intent}</p></li>)}</ol> : p.args && <pre>{JSON.stringify(p.args, null, 2)}</pre>}{event.method === "question" && <input aria-label="Answer" value={answer} onChange={e => setAnswer(e.target.value)} />}<button className="primary" onClick={() => approve(p.request_id, "once")}>{event.method === "question" ? "Send answer" : "Allow once"}</button><button onClick={() => approve(p.request_id, "deny")}>Decline</button></div>;
        return null;
      })}</div>
      {runId && finished && !conversational && <div className="validation-export"><a href={`/api/workbench/runs/${encodeURIComponent(runId)}/validation-record`} download>Download validation record ↓</a><small>Includes model choices, checks and execution evidence. Contains task content; human approval remains separate.</small></div>}
      {finished && <section className={`final-response ${conversational ? "assistant-reply" : finished.status === "done" ? "verified" : "has-gaps"}`}><div className="eyebrow">{conversational ? "BLACKBOX" : finished.status === "done" ? "WORKFLOW COMPLETE" : "WORKFLOW RESULT"}</div><Markdown text={finished.summary || "No summary available."} />{finished.unverified?.length > 0 && <p className="retry-note">Unverified: {finished.unverified.join("; ")}</p>}<div className="deliverables">{finished.artifacts?.map((a: any, i: number) => <a key={i} href={`/api/workbench/runs/${runId}/download?path=${encodeURIComponent(a.path ?? a.name)}`} download>▤ {a.name ?? a.path} <span>↓</span></a>)}</div></section>}
      {recovery && <section className="approval-card" aria-label="Task recovery"><div className="permission-eyebrow">TASK INTERRUPTED</div><b>Your saved work is still available.</b>{recovery.can_resume ? <><p>Continue from the saved plan with the remaining budget. Approval requests will be renewed.</p><button className="primary" onClick={continueRun}>Resume task</button></> : <><p>An operation may have changed files before its result was recorded. Automatic continuation is blocked. Inspect the affected files before starting another task.</p>{recovery.uncertain_operations?.map((op: any) => <details key={op.call_id}><summary>{String(op.tool).replaceAll("_", " ")}</summary><pre>{JSON.stringify(op.args, null, 2)}</pre></details>)}</>}</section>}
      {error && <div role="alert" className="inline-error">{error}</div>}<div ref={bottom} />
    </div><div className="composer-area"><div className="composer"><textarea aria-label="Task instructions" value={input} onChange={e => setInput(e.target.value)} placeholder="Ask a question, discuss an idea, or describe work to do…" rows={3} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }} /><div className="composer-bottom"><select aria-label="Model" value={model} onChange={e => setModel(e.target.value)} disabled={running}><option value="">◆ Auto-select model</option>{status?.models.filter((m: any) => !m.roles.includes("embed")).map((m: any) => <option key={m.id} value={m.id}>{m.id}</option>)}</select><select aria-label="Execution mode" value={mode} onChange={e => setMode(e.target.value)} disabled={running}><option value="auto">Auto</option><option value="ask">Ask before changes</option><option value="plan">Plan only</option></select><span className="composer-spacer" />{running ? <button className="stop-button" onClick={stop}>■ Stop</button> : <button className="send-button" aria-label="Run task" onClick={send} disabled={!input.trim() || !status?.ready}>↑</button>}</div></div><div className="composer-caption"><span>Workspace access only · Enter to run · Shift + Enter for a new line</span><span>{running ? `${elapsed}s · ${streamState}` : "Runs on your infrastructure"}</span></div></div></section>
    <aside className="inspector"><div className="inspector-tabs">{["workflow", "files", "evidence"].map(t => <button key={t} className={tab === t ? "selected" : ""} onClick={() => setTab(t)}>{t}</button>)}</div>
      {tab === "workflow" && <><div className="inspector-section"><span className="eyebrow">EXECUTION PLAN</span>{plan.length ? <ol className="plan-list">{plan.map((t: any, i: number) => <li key={t.id} className={taskState(t.id)}><span>{taskState(t.id) === "done" ? "✓" : i + 1}</span><div><b>{t.title}</b><small>{t.role} · {taskState(t.id)}</small></div></li>)}</ol> : <div className="empty-inspector"><div>◈</div>The plan appears here when you start a task.<small>Specialist agents. Shared evidence. Bounded retries.</small></div>}</div><div className="inspector-section"><span className="eyebrow">THIS RUN</span><dl className="run-metrics"><dt>Tools completed</dt><dd>{events.filter(e => e.method === "tool.finished").length}</dd><dt>Input tokens</dt><dd>{stats?.tokens_in?.toLocaleString() ?? "—"}</dd><dt>Output tokens</dt><dd>{stats?.tokens_out?.toLocaleString() ?? "—"}</dd><dt>Total tokens</dt><dd>{stats ? ((stats.tokens_in ?? 0) + (stats.tokens_out ?? 0)).toLocaleString() : "—"}</dd><dt>Elapsed</dt><dd>{stats ? `${Math.round(stats.elapsed_s)}s` : running ? `${elapsed}s` : "—"}</dd><dt>Model</dt><dd className="model-metric">{stats?.active_model || "Awaiting inference"}</dd></dl></div></>}
      {tab === "files" && <div className="inspector-section"><div className="section-heading"><span className="eyebrow">{files.length} WORKSPACE FILES</span><button aria-label="Refresh files" onClick={refreshFiles}>↻</button></div><div className="file-list">{files.map(f => <div key={f.path}><span>▤</span><button onClick={() => previewFile(f.path)} title="Preview file">{f.path}</button><button aria-label={`Reference ${f.path}`} onClick={() => setInput(p => `${p}${p ? " " : ""}${f.path}`)}>＋</button><small>{Math.max(1, Math.round(f.bytes / 1024))} KB</small></div>)}</div><p className="quiet-note">Click a filename to preview it, or + to reference it. Access stays inside the configured workspace.</p></div>}
      {tab === "evidence" && <div className="inspector-section"><span className="eyebrow">DOCUMENT SEARCH</span><p className="quiet-note">{status?.retrieval}. Indexing runs when a request needs a workspace workflow.</p><button className="wide-button" onClick={index} disabled={indexing || running}>{indexing ? "Indexing…" : "↻ Index workspace"}</button><div className="evidence-search"><input aria-label="Search local evidence" value={query} onChange={e => setQuery(e.target.value)} placeholder="Equipment tag, requirement…" onKeyDown={e => { if (e.key === "Enter" && query.trim()) void search(); }} /><button aria-label="Search" onClick={search} disabled={!query.trim()}>→</button></div>{indexNote && <p className="quiet-note">{indexNote}</p>}{hits.map(h => <details className="evidence-hit" key={h.chunk_id}><summary>▤ {h.title}<small>page {h.page} · {h.chunk_id.slice(0, 8)}</small></summary><p>{h.text}</p><small>{h.path}</small></details>)}</div>}
      {preview && <section className="file-preview"><div className="section-heading"><b>{preview.path}</b><button aria-label="Close file preview" onClick={() => setPreview(null)}>×</button></div>{preview.truncated && <p className="quiet-note">Showing the first 100 KB.</p>}<pre>{preview.text}</pre></section>}
      <div className="inspector-security"><span>◈</span><div><b>{status?.security.application_guard ? "Application network guard active" : "Checking network guard"}</b><small>{status?.security.os_isolation_verified ? "Network namespace checked" : "OS isolation: not attested"} <a href="#seal">View evidence ↗</a></small></div></div>
    </aside></div>
  </div>;
}
