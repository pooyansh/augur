/**
 * Typed fetch helpers for the dashboard API.
 *
 * ETag / 304 caching:
 *   - Each helper stores the last ETag in a module-level Map.
 *   - On subsequent calls it sends If-None-Match; if the server returns 304
 *     the previous cached response is returned unchanged.
 *
 * Base URL:
 *   Reads import.meta.env.VITE_API_BASE (default "" = same-origin).
 *   In dev, Vite proxies /api/* to localhost:8090.
 */

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

/** ETag cache: path → { etag, data } */
const _cache = new Map<string, { etag: string; data: unknown }>();

async function fetchApi<T>(path: string): Promise<T> {
  const url = `${BASE}${path}`;
  const cached = _cache.get(path);
  const headers: Record<string, string> = {};
  if (cached) {
    headers["If-None-Match"] = cached.etag;
  }

  const resp = await fetch(url, { headers });

  if (resp.status === 304 && cached) {
    return cached.data as T;
  }

  if (!resp.ok) {
    throw new Error(`API ${path} returned ${resp.status}`);
  }

  const data = (await resp.json()) as T;
  const etag = resp.headers.get("etag");
  if (etag) {
    _cache.set(path, { etag, data });
  }
  return data;
}

// ---------------------------------------------------------------------------
// Response types (mirrors src/manager/dashboard/models.py)
// ---------------------------------------------------------------------------

export interface HealthResponse {
  healthy: boolean;
  postgres_ok: boolean;
  all_bots_alive: boolean;
}

export interface StatusBot {
  bot_id: string;
  strategy: string;
  mode: string;
  pid: number | null;
  restart_count: number;
  heartbeat_age_s: number;
  snapshot_lag_s: number;
  last_error: string | null;
  spawned_at: string;
}

export interface StatusResponse {
  bots: StatusBot[];
  total_bots: number;
  alive_bots: number;
  paper_bots: number;
  live_bots: number;
}

export interface BotSummary {
  bot_id: string;
  strategy: string;
  market_id: string;
  mode: string;
  snapshot_at: string;
  version: number;
  state: Record<string, unknown>;
}

export interface AuditRow {
  id: number;
  ts: string;
  bot_id: string;
  kind: string;
  client_order_id: string | null;
  exchange_order_id: string | null;
  payload: Record<string, unknown>;
}

export interface BotDetail extends BotSummary {
  recent_audit: AuditRow[];
}

export interface StrategyRollup {
  strategy: string;
  wins: number;
  losses: number;
  gross_pnl: number;
  realized_pnl: number;
  unrealized_pnl: number;
  n_orders: number;
  n_bots: number;
  n_markets: number;
  last_fill_at: string | null;
}

export interface StrategiesResponse {
  strategies: StrategyRollup[];
}

export interface StrategyBotBreakdown {
  bot_id: string;
  market_id: string;
  wins: number;
  losses: number;
  gross_pnl: number;
  realized_pnl: number;
  unrealized_pnl: number;
  n_orders: number;
  last_fill_at: string | null;
}

export interface StrategyDetail {
  strategy: string;
  summary: StrategyRollup;
  bots: StrategyBotBreakdown[];
}

export interface MarketExposure {
  market_id: string;
  gross_pnl: number;
  realized_pnl: number;
  unrealized_pnl: number;
  n_bots: number;
  n_orders: number;
  last_fill_at: string | null;
}

export interface MarketsResponse {
  markets: MarketExposure[];
}

export interface FailureEvent {
  ts: string;
  bot_id: string;
  kind: string;
  detail: string | null;
}

export interface FailuresResponse {
  events: FailureEvent[];
  total: number;
}

export interface ExchangeBalance {
  exchange: string;
  balance: number;
  currency: string;
}

export interface CapitalResponse {
  total_usd: number;
  per_exchange: ExchangeBalance[];
  sourced_from: string;
}

// ---------------------------------------------------------------------------
// API functions
// ---------------------------------------------------------------------------

export const api = {
  health: () => fetchApi<HealthResponse>("/api/health"),
  status: () => fetchApi<StatusResponse>("/api/status"),
  bots: () => fetchApi<BotSummary[]>("/api/bots"),
  bot: (id: string) => fetchApi<BotDetail>(`/api/bots/${encodeURIComponent(id)}`),
  strategies: () => fetchApi<StrategiesResponse>("/api/strategies"),
  strategy: (name: string) =>
    fetchApi<StrategyDetail>(`/api/strategies/${encodeURIComponent(name)}`),
  markets: () => fetchApi<MarketsResponse>("/api/markets"),
  audit: (params?: {
    limit?: number;
    before?: string;
    bot_id?: string;
    kind?: string;
    since?: string;
  }) => {
    const qs = new URLSearchParams();
    if (params?.limit) qs.set("limit", String(params.limit));
    if (params?.before) qs.set("before", params.before);
    if (params?.bot_id) qs.set("bot_id", params.bot_id);
    if (params?.kind) qs.set("kind", params.kind);
    if (params?.since) qs.set("since", params.since);
    const q = qs.toString();
    return fetchApi<AuditRow[]>(`/api/audit${q ? `?${q}` : ""}`);
  },
  failures: () => fetchApi<FailuresResponse>("/api/failures"),
  capital: () => fetchApi<CapitalResponse>("/api/capital"),
};

// ---------------------------------------------------------------------------
// Control API — write-capable calls, kept structurally distinct from the
// read-only `api` object above. `/api/control/*` is a separate, narrow
// surface (see .claude/rules/06b-dashboard.md) that performs process control
// actions, not database writes.
// ---------------------------------------------------------------------------

export interface StopBotResponse {
  bot_id: string;
  stopped: boolean;
}

export const controlApi = {
  stopBot: async (botId: string): Promise<StopBotResponse> => {
    const url = `${BASE}/api/control/bots/${encodeURIComponent(botId)}/stop`;
    const resp = await fetch(url, { method: "POST" });
    if (!resp.ok) {
      throw new Error(`API /api/control/bots/${botId}/stop returned ${resp.status}`);
    }
    return (await resp.json()) as StopBotResponse;
  },
};
