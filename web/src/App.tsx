import React, { useEffect, useState } from "react";
import { getJson, postJson } from "./api.js";
import { Icon } from "./Icon.js";
import { readRoute, routeHash, sections, type SectionKey } from "./navigation.js";
import { ChatPage } from "./pages/Chat.js";
import { RunsPage } from "./pages/Runs.js";
import { SealPage } from "./pages/Seal.js";
import { ModelsPage } from "./pages/Models.js";
import { KnowledgePage } from "./pages/Knowledge.js";
import { EvalsPage } from "./pages/Evals.js";
import { AuditPage } from "./pages/Audit.js";
import { SettingsPage } from "./pages/Settings.js";

interface RecentTask { run_id: string; goal: string; status: string }

interface Health {
  network_isolation?: { verified: boolean };
  status: string;
  version: string;
  profile: string;
  sealed: boolean;
}

export function App(): React.ReactElement {
  const [route, setRoute] = useState(() => readRoute(location.hash));
  const [recent, setRecent] = useState<RecentTask[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [locked, setLocked] = useState(false);
  const [checkingAccess, setCheckingAccess] = useState(true);
  const [unlocking, setUnlocking] = useState(false);
  const [accessKey, setAccessKey] = useState("");
  const [loginError, setLoginError] = useState("");

  useEffect(() => {
    const onHash = () => setRoute(readRoute(location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (locked) return;
    let active = true;
    const refresh = async () => {
      try { const h = await getJson<Health>("/api/health"); if (active) { setHealth(h); setLocked(false); } }
      catch (e) { if (active) { setHealth(null); if (String(e).includes("Authentication required")) { setRecent([]); setLocked(true); } } }
      finally { if (active) setCheckingAccess(false); }
    };
    void refresh();
    const timer = setInterval(refresh, 10000);
    return () => { active = false; clearInterval(timer); };
  }, [locked]);

  useEffect(() => {
    if (checkingAccess || locked || !health) return;
    let active = true;
    const refresh = () => getJson<{ runs: RecentTask[] }>("/api/runs?limit=8").then(data => { if (active) setRecent(data.runs); }).catch(() => {});
    void refresh(); const timer = setInterval(refresh, 5000); return () => { active = false; clearInterval(timer); };
  }, [checkingAccess, locked, Boolean(health)]);
  const openTask = (id: string) => {
    sessionStorage.setItem("blackbox.openRun", id);
    location.hash = "workspace/chat"; setRoute({ section: "workspace", tab: "chat" });
    window.dispatchEvent(new CustomEvent("blackbox:open-run", { detail: id }));
  };
  const newTask = () => {
    sessionStorage.removeItem("blackbox.openRun");
    sessionStorage.removeItem("blackbox.draft");
    sessionStorage.removeItem("blackbox.draftWorkspace");
    location.hash = "workspace/chat";
    setRoute({ section: "workspace", tab: "chat" });
    window.dispatchEvent(new Event("blackbox:new-task"));
  };
  const section = sections[route.section];
  const assistant = route.section === "workspace" && route.tab === "chat";
  const Current = route.section === "documents" ? KnowledgePage
    : route.section === "settings" ? (route.tab === "models" ? ModelsPage : SettingsPage)
    : route.section === "assurance" ? (route.tab === "activity" ? AuditPage : route.tab === "evaluations" ? EvalsPage : SealPage)
    : RunsPage;
  if (checkingAccess) return <main className="login-screen"><p role="status">Connecting to your workspace…</p></main>;
  if (locked) return <main className="login-screen"><form className="login-card" aria-busy={unlocking} onSubmit={async e => { e.preventDefault(); if (unlocking) return; setUnlocking(true); setLoginError(""); try { await postJson("/auth/login", {token:accessKey}); const h = await getJson<Health>("/api/health"); setAccessKey(""); setHealth(h); setLocked(false); } catch (error) { setLoginError(String(error)); } finally { setUnlocking(false); } }}><div className="brand-mark" aria-hidden="true">▣</div><h1>Unlock BlackBox Workspace</h1><p>Enter the access key configured on your inference host.</p><label htmlFor="access-key">Access key</label><input id="access-key" type="password" autoComplete="current-password" value={accessKey} disabled={unlocking} aria-invalid={Boolean(loginError)} aria-describedby={loginError ? "login-error" : undefined} onChange={e => { setAccessKey(e.target.value); setLoginError(""); }} required /><button className="primary" type="submit" disabled={unlocking}>{unlocking ? "Unlocking…" : "Unlock"}</button>{loginError && <p id="login-error" role="alert">{loginError}</p>}<small>Your session stays on this host and expires in eight hours.</small></form></main>;
  return (
    <div className="app refined-app">
      <a className="skip-link" href="#workspace-content" onClick={e => { e.preventDefault(); document.getElementById("workspace-content")?.focus(); }}>Skip to content</a>
      <aside className="sidebar" aria-label="Workspace navigation">
        <a className="brand" href="#workspace/chat" aria-label="BlackBox home">
          <div className="brand-mark" aria-hidden="true"><span /></div>
          <div><div className="brand-name">BlackBox<span className="brand-period">.</span></div><div className="brand-sub">Industrial AI workspace</div></div>
        </a>
        <button className="sidebar-new-task" aria-label="New task" onClick={newTask} title="New conversation or task"><Icon name="plus" size={18} /><span>New task</span></button>
        <div className="nav-caption">WORKBENCH</div>
        <nav className="nav" aria-label="Main navigation">
          {(Object.keys(sections) as SectionKey[]).map(key => (
            <a key={key} className={key === route.section ? "active" : ""} aria-current={key === route.section ? "page" : undefined} aria-label={sections[key].label} title={sections[key].label} href={routeHash({ section: key, tab: (Object.keys(sections[key].tabs)[0] ?? "chat") })}>
              <span className="nav-ico"><Icon name={key} /></span><span className="nav-label">{sections[key].label}</span>
              {key === route.section && <span className="nav-active-dot" />}
            </a>
          ))}
        </nav>
        <section className="sidebar-tasks" aria-label="Recent tasks">
          <div className="sidebar-section-label"><span>Recent tasks</span><a href="#workspace/history" title="View all tasks">View all <Icon name="arrow" size={13}/></a></div>
          {recent.slice(0, 6).map(run => <button key={run.run_id} onClick={() => openTask(run.run_id)} title={run.goal}><span className={`task-dot ${run.status}`} /><span>{run.goal}</span>{["running", "intake", "planning"].includes(run.status) && <small>Live</small>}</button>)}
          {!recent.length && <p>Your conversations and work will appear here.</p>}
        </section>
        <a className="runtime-status" href="#assurance/network">
          <span className={`runtime-led ${health === null ? "offline" : ""}`} />
          <span><strong>{health ? "Local runtime" : "Connection unavailable"}</strong><small>{health?.network_isolation?.verified ? "Isolation checks passed" : health?.sealed ? "OS isolation not verified" : "Development environment"}</small></span>
          <Icon name="arrow" size={15}/>
        </a>
        <div className="sidebar-version"><span>BLACKBOX WORKSPACE</span><span>v{health?.version ?? "—"}</span></div>
      </aside>
      <main className="main" id="workspace-content" tabIndex={-1}>
        <header className="section-bar"><div><span className="section-breadcrumb">Workbench</span><span className="breadcrumb-divider">/</span><strong>{section.label}</strong></div><span className="local-label"><span />On this computer</span></header>
        <nav className="section-tabs" aria-label={`${section.label} sections`}>
          {Object.entries(section.tabs).map(([tab, label]) => <a key={tab} href={routeHash({ section: route.section, tab })} aria-current={route.tab === tab ? "page" : undefined} className={route.tab === tab ? "selected" : ""}>{label}</a>)}
        </nav>
        {assistant ? <ChatPage /> : <div className="section-content"><Current /></div>}
      </main>
    </div>
  );
}
