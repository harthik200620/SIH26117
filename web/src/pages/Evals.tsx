import React, { useEffect, useState } from "react";
import { getJson } from "../api.js";
import { Card, Empty, Page, Pill, Section } from "../components.js";

interface EvalCase {
  case_id: string;
  passed: boolean;
  score: number | null;
  metrics: Record<string, unknown>;
  detail: string;
}

interface EvalRun {
  id: string;
  suite: string;
  profile: string;
  status: string;
  started_at: string;
  pass_rate: number | null;
  cases: EvalCase[];
}

function rateColor(rate: number): string {
  return rate >= 0.999 ? "#12b76a" : rate >= 0.7 ? "#f79009" : "#f04438";
}

function History({ runs }: { runs: EvalRun[] }): React.ReactElement {
  const suites = [...new Set(runs.map((r) => r.suite))];
  return (
    <div className="grid">
      {suites.map((suite) => {
        const history = runs.filter((r) => r.suite === suite).slice(0, 12).reverse();
        const latest = history[history.length - 1]?.pass_rate ?? 0;
        return (
          <Card key={suite}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
              <span style={{ fontWeight: 650 }}>{suite}</span>
              <Pill tone={latest >= 0.999 ? "ok" : latest >= 0.7 ? "warn" : "bad"}>
                {(latest * 100).toFixed(0)}%
              </Pill>
            </div>
            <div className="spark">
              {history.map((r) => {
                const rate = r.pass_rate ?? 0;
                return (
                  <div
                    key={r.id}
                    className="bar-v"
                    title={`${r.started_at.slice(0, 19).replace("T", " ")} — ${(rate * 100).toFixed(0)}%`}
                    style={{ height: Math.max(4, rate * 40), background: rateColor(rate) }}
                  />
                );
              })}
            </div>
          </Card>
        );
      })}
    </div>
  );
}

export function EvalsPage(): React.ReactElement {
  const [runs, setRuns] = useState<EvalRun[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    const refresh = () =>
      getJson<{ eval_runs: EvalRun[] }>("/api/evals")
        .then((r) => setRuns(r.eval_runs))
        .catch(() => undefined);
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, []);

  if (!runs) return <Page title="Evaluations"><div className="muted">Loading…</div></Page>;

  return (
    <Page
      title="Evaluations"
      desc="Pass-rate history per suite; scenario, retrieval, drawing and seal checks."
    >
      {runs.length === 0 ? (
        <Empty glyph="✓" title="No evaluation runs yet" hint="Recorded evaluation results appear here after a suite is run on this installation." />
      ) : (
        <>
          <History runs={runs} />

          <Section>Recent runs</Section>
          <Card pad={false}>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Started</th>
                    <th>Suite</th>
                    <th>Profile</th>
                    <th>Pass rate</th>
                    <th className="num">Cases</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((r) => (
                    <React.Fragment key={r.id}>
                      <tr className="rowlink" onClick={() => setOpen(open === r.id ? null : r.id)}>
                        <td className="mono">{r.started_at.slice(0, 19).replace("T", " ")}</td>
                        <td style={{ fontWeight: 550 }}>{r.suite}</td>
                        <td className="dim">{r.profile}</td>
                        <td>
                          <Pill tone={(r.pass_rate ?? 0) >= 0.999 ? "ok" : "warn"}>
                            {r.pass_rate == null ? r.status : `${(r.pass_rate * 100).toFixed(0)}%`}
                          </Pill>
                        </td>
                        <td className="num dim">{r.cases.length}</td>
                      </tr>
                      {open === r.id &&
                        r.cases.map((c) => (
                          <tr key={c.case_id} style={{ background: "var(--surface-2)" }}>
                            <td></td>
                            <td className="mono">{c.case_id}</td>
                            <td><Pill tone={c.passed ? "ok" : "bad"}>{c.passed ? "pass" : "fail"}</Pill></td>
                            <td colSpan={2} className="muted">{c.detail}</td>
                          </tr>
                        ))}
                    </React.Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </Page>
  );
}
