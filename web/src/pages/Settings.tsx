import React, { useEffect, useRef, useState } from "react";
import { getJson } from "../api.js";
import { Card, Page, Section } from "../components.js";

function logClass(line: string): string {
  const l = line.toLowerCase();
  if (l.includes("error") || l.includes("traceback") || l.includes("critical")) return "lg-err";
  if (l.includes("warn")) return "lg-warn";
  if (l.startsWith("debug") || l.includes(" debug ")) return "lg-dim";
  return "";
}

function LogView({ lines }: { lines: string[] }): React.ReactElement {
  const ref = useRef<HTMLPreElement>(null);
  const pinned = useRef(true);
  useEffect(() => {
    const el = ref.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [lines]);
  return (
    <pre
      className="logview"
      ref={ref}
      onScroll={() => {
        const el = ref.current;
        if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
    >
      {lines.length === 0
        ? "(empty)"
        : lines.map((l, i) => {
            const cls = logClass(l);
            return (
              <div key={i} className={cls || undefined}>{l || " "}</div>
            );
          })}
    </pre>
  );
}

interface ConfigData {
  profile: string;
  sealed: boolean;
  config_file: string | null;
  assets_dir: string;
  data_dir: string;
  models_dir: string;
  entries: { key: string; value: string; source: string }[];
}

interface LogFile { name: string; size_kb: number }

export function SettingsPage(): React.ReactElement {
  const [cfg, setCfg] = useState<ConfigData | null>(null);
  const [logs, setLogs] = useState<LogFile[]>([]);
  const [openLog, setOpenLog] = useState<string | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);

  useEffect(() => {
    getJson<ConfigData>("/api/config").then(setCfg).catch(() => undefined);
    const refresh = () => getJson<{ files: LogFile[] }>("/api/logs").then((d) => setLogs(d.files)).catch(() => undefined);
    refresh();
    const timer = setInterval(refresh, 8000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!openLog) return;
    const load = () =>
      getJson<{ lines: string[] }>(`/api/logs/${encodeURIComponent(openLog)}?tail=300`)
        .then((d) => setLogLines(d.lines))
        .catch(() => undefined);
    load();
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [openLog]);

  if (!cfg) return <Page title="Runtime & logs"><div className="muted">Loading…</div></Page>;

  return (
    <Page title="Runtime & logs" desc="Effective configuration, storage locations, and live engine logs.">
      <Section>This installation</Section>
      <Card pad={false}>
        <div className="table-wrap">
          <table>
            <tbody>
              <tr><td style={{ width: 160 }} className="dim">Profile</td><td><span className="chip">{cfg.profile}</span></td></tr>
              <tr><td className="dim">Privacy controls</td><td><a href="#assurance/network">View network controls and verification →</a></td></tr>
              <tr><td className="dim">Config file</td><td className="mono">{cfg.config_file ?? "(defaults — create yantra.yaml to override)"}</td></tr>
              <tr><td className="dim">Data &amp; logs</td><td className="mono">{cfg.data_dir}</td></tr>
              <tr><td className="dim">Models</td><td className="mono">{cfg.models_dir}</td></tr>
              <tr><td className="dim">Assets</td><td className="mono">{cfg.assets_dir}</td></tr>
            </tbody>
          </table>
        </div>
      </Card>

      <Section>Engine &amp; server logs</Section>
      {logs.length === 0 ? (
        <Card><span className="muted">No log files yet — logs appear once an engine starts.</span></Card>
      ) : (
        <Card pad={false}>
          <div className="table-wrap">
            <table>
              <thead><tr><th>File</th><th className="num">Size</th><th></th></tr></thead>
              <tbody>
                {logs.map((f) => (
                  <tr key={f.name}>
                    <td className="mono">{f.name}</td>
                    <td className="num dim">{f.size_kb} KB</td>
                    <td style={{ textAlign: "right" }}>
                      <button onClick={() => setOpenLog(openLog === f.name ? null : f.name)}>
                        {openLog === f.name ? "Close" : "View log"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
      {openLog && (
        <>
          <Section>{openLog} · live tail</Section>
          <Card pad={false}>
            <LogView lines={logLines} />
          </Card>
        </>
      )}

      <details className="advanced-details"><summary>Advanced configuration</summary>
      <Card pad={false}>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Key</th><th>Value</th><th>Set by</th></tr></thead>
            <tbody>
              {cfg.entries.map((e) => (
                <tr key={e.key}>
                  <td className="mono">{e.key}</td>
                  <td className="mono dim">{e.value}</td>
                  <td><span className="chip">{e.source}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <p className="muted" style={{ marginTop: 10 }}>
        Change settings in <span className="cmd">yantra.yaml</span> (workspace or <span className="mono">~/.yantra</span>),
        via <span className="cmd">YANTRA_*</span> environment variables, or CLI flags — precedence is
        profile &lt; file &lt; env &lt; CLI, and this table always shows which layer won.
      </p></details>
    </Page>
  );
}
