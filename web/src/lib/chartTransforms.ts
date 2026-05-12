/**
 * Pure client-side aggregation helpers for dashboard charts.
 *
 * All functions take raw API data and return chart-ready arrays.
 * No side effects; fully unit-testable.
 */

import type { StrategyRollup, MarketExposure, FailureEvent } from "./api.ts";

export interface PnlBarDatum {
  name: string;
  pnl: number;
  wins: number;
  losses: number;
}

/** Build a bar-chart dataset from strategies, sorted by gross_pnl descending. */
export function strategyPnlData(strategies: StrategyRollup[]): PnlBarDatum[] {
  return [...strategies]
    .sort((a, b) => b.gross_pnl - a.gross_pnl)
    .map((s) => ({
      name: s.strategy,
      pnl: s.gross_pnl,
      wins: s.wins,
      losses: s.losses,
    }));
}

export interface MarketBarDatum {
  name: string;
  pnl: number;
  n_bots: number;
}

/** Build a bar-chart dataset from markets, sorted by gross_pnl descending. */
export function marketExposureData(markets: MarketExposure[]): MarketBarDatum[] {
  return [...markets]
    .sort((a, b) => b.gross_pnl - a.gross_pnl)
    .map((m) => ({
      name: m.market_id,
      pnl: m.gross_pnl,
      n_bots: m.n_bots,
    }));
}

export interface FailureCountByKind {
  kind: string;
  count: number;
}

/** Count failure events by kind for a summary chart. */
export function failureCountByKind(events: FailureEvent[]): FailureCountByKind[] {
  const counts = new Map<string, number>();
  for (const ev of events) {
    counts.set(ev.kind, (counts.get(ev.kind) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([kind, count]) => ({ kind, count }))
    .sort((a, b) => b.count - a.count);
}

export interface WinRateDatum {
  strategy: string;
  win_rate: number;
  total: number;
}

/** Compute win rate per strategy. Returns 0 when no wins/losses are recorded. */
export function strategyWinRates(strategies: StrategyRollup[]): WinRateDatum[] {
  return strategies.map((s) => {
    const total = s.wins + s.losses;
    return {
      strategy: s.strategy,
      win_rate: total > 0 ? s.wins / total : 0,
      total,
    };
  });
}
