import React, { useEffect, useState } from "react";
import { getJson } from "../api.js";
import { Card, Page, Pill, Section, Stat } from "../components.js";

interface AuditData {
  ok: boolean;
  entries: number;
  head: string | null;
  detail: string | null;
  recent: string[];
}

export function AuditPage(): React.ReactElement {
  const [data, setData] = useState<AuditData | null>(null);

  useEffect(() => {
    const refresh = () => getJson<AuditData>("/api/audit").then(setData).catch(() => undefined);
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, []);

  if (!data) return <Page title="Activity log"><div className="muted">Loading…</div></Page>;
  const parsed = data.recent
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return null;
      }
    })
    .filter(Boolean);

  return (
    <Page
      title="Activity log"
      desc="Hash-chained record of every side-effecting action. Verified continuously."
    >
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))" }}>
        <Card>
          <div className="stat-label">Chain integrity</div>
          <div style={{ marginTop: 8 }}>
            <Pill tone={data.ok ? "ok" : "bad"}>{data.ok ? "verified" : "TAMPERED"}</Pill>
          </div>
          {data.detail && <div className="muted" style={{ color: "var(--bad)", marginTop: 6 }}>{data.detail}</div>}
        </Card>
        <Stat label="Entries" value={data.entries.toLocaleString()} />
        <Card>
          <div className="stat-label">Head hash</div>
          <div className="mono" style={{ marginTop: 8, wordBreak: "break-all", fontSize: 11.5 }}>
            {data.head ?? "(empty chain)"}
          </div>
        </Card>
      </div>

      <Section>Recent events</Section>
      <Card pad={false}>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="num">Seq</th>
                <th>Actor</th>
                <th>Event</th>
                <th>Hash</th>
              </tr>
            </thead>
            <tbody>
              {parsed.reverse().map((e: any) => (
                <tr key={e.seq}>
                  <td className="num mono dim">{e.seq}</td>
                  <td><span className="chip">{e.actor}</span></td>
                  <td style={{ fontWeight: 550 }}>{e.event}</td>
                  <td className="mono muted">{e.hash.slice(0, 20)}…</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </Page>
  );
}
