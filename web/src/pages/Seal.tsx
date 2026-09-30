import React, { useEffect, useState } from "react";
import { getJson, postJson, type SealStatus } from "../api.js";
import { Card, Empty, Page, Pill, Section } from "../components.js";

const LAYER_LABEL: Record<string, string> = {
  bundle_verified: "Bundle verification (not attested)",
  env_locked: "Environment lock",
  socket_guard_active: "Process socket guard",
  compose_internal: "Container isolation (not attested)",
  nftables_present: "Host firewall",
};

export function SealPage(): React.ReactElement {
  const [status, setStatus] = useState<SealStatus | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [verdict, setVerdict] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => getJson<SealStatus>("/api/seal").then(setStatus).catch((e) => setError(String(e)));
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
  }, []);

  const runVerify = async () => {
    setVerifying(true);
    setError(null);
    try {
      setVerdict(await postJson<any>("/api/seal/verify", {}));
    } catch (e) {
      setError(String(e));
    } finally {
      setVerifying(false);
      refresh();
    }
  };

  if (error && !status) return <Page title="Network & privacy"><Empty glyph="◈" title="Server unreachable" hint={error} /></Page>;
  if (!status) return <Page title="Network & privacy"><div className="muted">Loading…</div></Page>;

  const blocked = status.blocked_attempts_total;
  return (
    <Page
      title="Network & privacy"
      desc="Inspect observed controls and blocked attempts. An application guard alone does not prove operating-system isolation."
      actions={
        <button className="primary" onClick={runVerify} disabled={verifying}>
          {verifying ? "Verifying…" : "Run verification"}
        </button>
      }
    >
      <Card pad={false}>
        <div className="seal-hero">
          <div className={`seal-glyph ${status.sealed ? "ok" : "bad"}`}>{status.sealed ? "🔒" : "⚠"}</div>
          <div style={{ flex: 1 }}>
            <div className="seal-title" style={{ color: status.sealed ? "var(--ok)" : "var(--warn)" }}>
              {status.sealed ? "Offline policy enabled" : "Unsealed — development mode"}
            </div>
            <div className="seal-sub">
              {blocked} egress attempt{blocked === 1 ? "" : "s"} blocked · allowlist {status.allowlist.join(", ")}
            </div>
          </div>
        </div>
      </Card>

      {status.network_isolation && <Card><h3>OS network isolation</h3><Pill tone={status.network_isolation.verified ? "ok" : "warn"}>{status.network_isolation.verified ? "Namespace checks passed" : "Not verified"}</Pill>{status.network_isolation.verified && <><p>{status.network_isolation.scope}</p><p className="mono">Kernel: {status.network_isolation.kernel}</p><p>Native child: {status.network_isolation.child?.verified ? "passed" : "not verified"} · Packet capture: {status.network_isolation.packet_capture}</p><ul>{status.network_isolation.probes?.map(p => <li key={p.name}>{p.name.replaceAll("_", " ")}: {p.blocked ? "blocked by kernel" : "failed"}</li>)}</ul></>}</Card>}
      <Section>Defence layers</Section>
      <div className="layers">
        {Object.entries(status.layers).map(([layer, ok]) => (
          <div className="layer" key={layer}>
            <span>{LAYER_LABEL[layer] ?? layer.replace(/_/g, " ")}</span>
            <Pill tone={ok ? "ok" : "warn"}>{ok ? "observed" : "not attested"}</Pill>
          </div>
        ))}
      </div>

      <Section>Blocked attempts</Section>
      {status.last_attempts.length === 0 ? (
        <Empty glyph="◈" title="No blocked attempts recorded" hint="This log covers instrumented processes. Absence of events is not proof of zero network traffic; verify the host firewall and collect packet evidence." />
      ) : (
        <Card pad={false}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Process</th>
                  <th>Destination</th>
                  <th>Kind</th>
                  <th>Origin</th>
                </tr>
              </thead>
              <tbody>
                {status.last_attempts.map((a, i) => (
                  <tr key={i}>
                    <td className="mono">{a.ts.slice(11, 19)}</td>
                    <td>{a.process}</td>
                    <td className="mono">{a.dest}{a.port ? `:${a.port}` : ""}</td>
                    <td><Pill tone="warn" dot={false}>{a.kind.replace("blocked_", "")}</Pill></td>
                    <td className="mono muted">{(a.stack ?? [])[0] ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {verdict && (
        <>
          <Section>Verification result</Section>
          <Card pad={false}>
            <div className="table-wrap">
              <table>
                <tbody>
                  {(verdict.outcomes ?? []).map((o: any, i: number) => (
                    <tr key={i}>
                      <td style={{ width: 90 }}>
                        <Pill tone={o.skipped ? "neutral" : o.passed ? "ok" : "bad"}>
                          {o.skipped ? "skipped" : o.passed ? "pass" : "fail"}
                        </Pill>
                      </td>
                      <td style={{ width: 190, fontWeight: 550 }}>{o.name.replace(/_/g, " ")}</td>
                      <td className="muted">{o.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
          <p className="muted" style={{ marginTop: 10 }}>
            The signed Seal Certificate is printed by <span className="cmd">yantra seal verify</span>; the live egress
            demo is <span className="cmd">yantra seal demo</span>.
          </p>
        </>
      )}
    </Page>
  );
}
