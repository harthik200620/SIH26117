import React, { useEffect, useRef, useState } from "react";
import { getJson, postJson } from "../api.js";
import { Page } from "../components.js";
import { Icon } from "../Icon.js";
import { WorkspacePicker } from "../WorkspacePicker.js";
import { preferredWorkspace } from "../workspace.js";

type LocalFile = { path: string; bytes: number };
type Hit = { chunk_id: string; title: string; text: string; path: string; page?: number };
type Preview = { path: string; text: string; truncated?: boolean };
export function KnowledgePage(): React.ReactElement {
  const [workspace, setWorkspace] = useState("");
  const [roots, setRoots] = useState<string[]>([]);
  const [files, setFiles] = useState<LocalFile[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [indexing, setIndexing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const generation = useRef(0);
  useEffect(() => {
    let active = true;
    getJson<{ workspace: string; roots: string[] }>("/api/workbench/status").then(s => {
      if (active) { setWorkspace(preferredWorkspace(s)); setRoots(s.roots.length ? s.roots : [s.workspace]); }
    }).catch(e => { if (active) { setError(String(e)); setLoading(false); } });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!workspace) return;
    let active = true;
    generation.current++;
    setFiles([]); setPreview(null); setHits(null); setNote(""); setLoading(true); setError("");
    getJson<{ files: LocalFile[]; truncated: boolean }>(`/api/workbench/files?workspace_path=${encodeURIComponent(workspace)}`).then(r => {
      if (active) { setFiles(r.files); setTruncated(r.truncated); }
    }).catch(e => { if (active) setError(String(e)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [workspace]);
  const search = async (e: React.FormEvent) => {
    e.preventDefault(); if (!query.trim() || !workspace || busy) return;
    const epoch = generation.current;
    setBusy(true); setError("");
    try { const r = await postJson<{ hits: Hit[] }>("/api/workbench/search", { workspace, query: query.trim() }); if (epoch === generation.current) setHits(r.hits); }
    catch (e) { if (epoch === generation.current) setError(String(e)); }
    finally { setBusy(false); }
  };
  const index = async () => {
    setIndexing(true); setError("");
    try {
      const r = await postJson<{ documents: number; skipped: number; errors: number }>("/api/workbench/index", { workspace });
      setNote(`${r.documents} updated · ${r.skipped} unchanged · ${r.errors} errors`);
      const listing = await getJson<{ files: LocalFile[]; truncated: boolean }>(`/api/workbench/files?workspace_path=${encodeURIComponent(workspace)}`);
      setFiles(listing.files); setTruncated(listing.truncated);
    }
    catch (e) { setError(String(e)); } finally { setIndexing(false); }
  };
  const open = async (path: string) => {
    const epoch = ++generation.current;
    setError("");
    try { const r = await getJson<Preview>(`/api/workbench/file-preview?workspace_path=${encodeURIComponent(workspace)}&path=${encodeURIComponent(path)}`); if (epoch === generation.current) setPreview(r); }
    catch (e) { if (epoch === generation.current) setError(String(e)); }
  };
  const reference = (path: string) => {
    sessionStorage.setItem("blackbox.draft", `Read ${JSON.stringify(path)} and `);
    sessionStorage.setItem("blackbox.draftWorkspace", workspace);
    sessionStorage.removeItem("blackbox.openRun");
    location.hash = "workspace/chat";
  };
  const visible = files.filter(f => f.path.toLowerCase().includes(filter.toLowerCase()));
  return <Page title="Your source material" desc="Browse local files, search their contents, and bring evidence into a task." actions={<button onClick={index} disabled={!workspace || indexing || busy}>{indexing ? "Updating index…" : "Update search index"}</button>}>
    <div className="document-location"><Icon name="documents"/><div><label htmlFor="document-workspace">Workspace folder</label><input id="document-workspace" value={workspace} title={workspace} readOnly/></div><WorkspacePicker value={workspace} roots={roots} disabled={indexing || busy} onChange={selection => { setWorkspace(selection.workspace); setRoots(selection.roots); }}/></div>
    <form className="document-search" onSubmit={search}><Icon name="search"/><input aria-label="Search document contents" value={query} onChange={e => setQuery(e.target.value)} placeholder="Search manuals, inspection reports, or equipment tags…"/><button className="primary" disabled={!query.trim() || !workspace || busy || indexing}>{busy ? "Searching…" : "Search contents"}</button></form>
    {error && <p className="inline-error" role="alert">{error}</p>}{note && <p role="status" className="document-note">{note}</p>}
    {hits !== null && <section className="document-results" aria-label="Search results"><div className="document-section-heading"><h2>{hits.length} matching passages</h2><button onClick={() => setHits(null)}>Clear results</button></div>{!hits.length && <p className="muted">No matches. Update the search index if files have changed, or try a different phrase.</p>}{hits.map(hit => <article className="document-hit" key={hit.chunk_id}><div><strong>{hit.title}</strong><span>{hit.page ? `Page ${hit.page}` : "Source passage"}</span></div><p>{hit.text}</p><button onClick={() => open(hit.path)}>View source</button></article>)}</section>}
    <div className={`document-layout ${preview ? "has-preview" : ""}`}><section className="document-files"><div className="document-section-heading"><h2>Files <span>{files.length}{truncated ? "+" : ""}</span></h2><input aria-label="Filter filenames" placeholder="Filter by filename" value={filter} onChange={e => setFilter(e.target.value)}/></div>
      {loading ? <p role="status">Loading local files…</p> : !visible.length ? <div className="document-empty"><Icon name="documents" size={30}/><h3>{files.length ? "No matching filenames" : "Start with your source documents"}</h3><p>{files.length ? "Try a different filename." : "Place manuals, reports, or data in the workspace folder above, then update the search index."}</p></div> : <div className="table-wrap"><table className="document-table"><thead><tr><th>Name</th><th>Type</th><th>Size</th><th><span className="sr-only">Actions</span></th></tr></thead><tbody>{visible.map(file => <tr key={file.path}><td><button className="file-name" onClick={() => open(file.path)}><Icon name="documents" size={17}/><span>{file.path}</span></button></td><td className="file-type">{file.path.includes(".") ? file.path.split(".").pop() : "FILE"}</td><td>{file.bytes < 1024 ? `${file.bytes} B` : `${Math.round(file.bytes / 1024)} KB`}</td><td><button className="file-use" onClick={() => reference(file.path)} aria-label={`Use ${file.path} in a task`}>Use in task <Icon name="arrow" size={14}/></button></td></tr>)}</tbody></table></div>}
      {truncated && <p className="muted">Showing the first 300 files, up to three folders deep.</p>}
    </section>{preview && <aside className="document-preview" aria-label="File preview"><div className="document-section-heading"><h2>{preview.path}</h2><button aria-label="Close preview" onClick={() => setPreview(null)}><Icon name="close" size={18}/></button></div>{preview.truncated && <p className="muted">Preview truncated to 100 KB.</p>}<pre>{preview.text}</pre><button onClick={() => reference(preview.path)}>Use in a task <Icon name="arrow" size={15}/></button></aside>}</div>
  </Page>;
}
