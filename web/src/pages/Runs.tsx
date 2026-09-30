import React, { useEffect, useState } from "react";
import { getJson, type SpanRow } from "../api.js";
import { Page, Pill, statusTone, timeAgo } from "../components.js";
import { Markdown } from "../md.js";
import { Icon } from "../Icon.js";
interface RunSummary { run_id: string; goal: string; status: string; mode: string; created_at: string }
interface RunDetail { final: { summary?: string; response_kind?: string; artifacts?: { path: string; name: string }[] } | null; budget_used: { seconds_used?: number; tool_calls_used?: number }; status: string }
export function RunsPage(): React.ReactElement {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [spans, setSpans] = useState<SpanRow[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    const refresh = () => getJson<{ runs: RunSummary[] }>("/api/runs").then(d => { if (active) { setRuns(d.runs); setError(""); } }).catch(e => { if (active) setError(String(e)); }).finally(() => { if (active) setLoading(false); });
    void refresh(); const timer = setInterval(refresh, 8000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  useEffect(() => {
    if (!selected) return;
    let active = true; setDetail(null); setSpans([]);
    Promise.all([getJson<RunDetail>(`/api/workbench/runs/${selected}`), getJson<{ spans: SpanRow[] }>(`/api/runs/${selected}/trace`)]).then(([r, t]) => { if (active) { setDetail(r); setSpans(t.spans); } }).catch(e => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [selected]);
  const open = (id: string) => { sessionStorage.setItem("blackbox.openRun", id); location.hash = "workspace/chat"; };
  const visible = runs.filter(r => r.goal.toLowerCase().includes(query.toLowerCase()));
  const chosen = runs.find(r => r.run_id === selected);
  return <Page title="Task history" desc="Revisit conversations, inspect outcomes, and return to work in progress.">
    <div className="history-toolbar"><span>{runs.length} recent tasks</span><label><Icon name="search" size={17}/><input aria-label="Search task history" placeholder="Search tasks" value={query} onChange={e => setQuery(e.target.value)}/></label></div>
    {error && <p role="alert" className="inline-error">{error}</p>}
    {loading ? <p role="status">Loading task history…</p> : <div className={`history-layout ${selected ? "has-selection" : ""}`}><div className="history-list">{visible.map(r => <button className={`history-item ${selected === r.run_id ? "selected" : ""}`} key={r.run_id} aria-pressed={selected === r.run_id} onClick={() => setSelected(r.run_id)}><div><span className={`task-dot ${r.status}`}/><strong>{r.goal}</strong></div><footer><Pill tone={statusTone(r.status)} dot={false}>{r.status.replaceAll("_", " ")}</Pill><time>{timeAgo(r.created_at)}</time><Icon name="arrow" size={16}/></footer></button>)}{!visible.length && <div className="document-empty"><h3>{runs.length ? "No matching tasks" : "Your work starts here"}</h3><p>{runs.length ? "Try another search." : "Start a conversation in the Assistant. Its outcome and files will appear here."}</p><a href="#workspace/chat">Go to Assistant →</a></div>}</div>
    {chosen && <section className="history-detail"><div className="document-section-heading"><h2>Task details</h2><button aria-label="Close task details" onClick={() => setSelected(null)}><Icon name="close" size={18}/></button></div><h3>{chosen.goal}</h3><button className="primary" onClick={() => open(chosen.run_id)}>Open in workspace <Icon name="arrow" size={16}/></button>{detail ? <><div className="history-facts"><div><span>Status</span><strong>{detail.status.replaceAll("_", " ")}</strong></div><div><span>Tools used</span><strong>{detail.budget_used?.tool_calls_used ?? 0}</strong></div><div><span>Time</span><strong>{Math.round(detail.budget_used?.seconds_used ?? 0)}s</strong></div></div><Markdown text={detail.final?.summary ?? "Open this task to see its progress and any pending approvals."}/>{detail.final?.artifacts?.map(a => <a className="history-artifact" key={a.path} href={`/api/workbench/runs/${chosen.run_id}/download?path=${encodeURIComponent(a.path)}`} download><Icon name="documents" size={17}/>{a.name}</a>)}<details className="advanced-details"><summary>Execution details · {spans.length} recorded steps</summary>{spans.map(span => <div className="history-span" key={span.span_id}><span>{span.name}</span><span>{((span.end_ns - span.start_ns) / 1e9).toFixed(2)}s</span></div>)}{!spans.length && <p>No execution trace recorded.</p>}</details></> : <p role="status">Loading task details…</p>}</section>}</div>}
  </Page>;
}
