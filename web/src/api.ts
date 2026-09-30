/** Server API + live RPC subscription for the dashboard. */

export async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) { const body = await res.json().catch(() => ({})); throw new Error(body.detail ?? body.error ?? `HTTP ${res.status}`); }
  return (await res.json()) as T;
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) { const body = await res.json().catch(() => ({})); throw new Error(body.detail ?? body.error ?? `HTTP ${res.status}`); }
  return (await res.json()) as T;
}

export interface SealStatus {
  network_isolation?: { verified: boolean; kernel?: string; scope?: string; probes?: { name: string; blocked: boolean; errno: number }[]; child?: { verified: boolean }; packet_capture?: string };
  sealed: boolean;
  allowlist: string[];
  blocked_attempts_total: number;
  last_attempts: { ts: string; process: string; dest: string; port: number | null; kind: string; stack: string[] }[];
  layers: Record<string, boolean>;
  nftables_counters?: Record<string, number>;
}

export interface ModelInfo {
  id: string;
  engine: string;
  params_b: number;
  quant: string | null;
  roles: string[];
  capabilities: string[];
  available: boolean;
  vram_gb: number;
  local?: boolean;
  path?: string | null;
  probes_passed?: number | null;
  probes_total?: number | null;
}

export interface RoleAssignment {
  role: string;
  model_id: string | null;
  source: "policy" | "fallback" | "none";
  reason: string;
  probes_passed: number | null;
  probes_total: number | null;
}

export interface EngineStatus {
  engine_id: string;
  kind: string;
  replica: number;
  status: string;
  port: number | null;
  error: string | null;
}

export interface SpanRow {
  span_id: string;
  parent_id: string | null;
  name: string;
  kind: string;
  start_ns: number;
  end_ns: number;
  status: string;
  attrs: Record<string, any>;
  task_id: string | null;
}

/** Minimal WS client that mirrors run notifications to a callback. */
export class LiveFeed {
  private ws: WebSocket | null = null;
  constructor(private readonly onNote: (method: string, params: any) => void) {}

  connect(): void {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/rpc`);
    this.ws.onmessage = (ev) => {
      try {
        const frame = JSON.parse(ev.data);
        if (typeof frame.method === "string") this.onNote(frame.method, frame.params ?? {});
      } catch {
        /* ignore */
      }
    };
    this.ws.onclose = () => setTimeout(() => this.connect(), 2000);
  }

  close(): void {
    this.ws?.close();
  }
}
