/**
 * Vitest unit tests for client-side aggregation helpers in chartTransforms.ts.
 */

import { describe, it, expect } from "vitest";
import {
  strategyPnlData,
  marketExposureData,
  failureCountByKind,
  strategyWinRates,
} from "../lib/chartTransforms.ts";
import type { StrategyRollup, MarketExposure, FailureEvent } from "../lib/api.ts";

// ---------------------------------------------------------------------------
// strategyPnlData
// ---------------------------------------------------------------------------

describe("strategyPnlData", () => {
  const strats: StrategyRollup[] = [
    {
      strategy: "momentum_v1",
      wins: 3,
      losses: 1,
      gross_pnl: 15.0,
      realized_pnl: 12.0,
      unrealized_pnl: 3.0,
      n_orders: 10,
      n_bots: 1,
      n_markets: 1,
      last_fill_at: null,
    },
    {
      strategy: "mean_reversion_v1",
      wins: 1,
      losses: 5,
      gross_pnl: -8.0,
      realized_pnl: -8.0,
      unrealized_pnl: 0,
      n_orders: 6,
      n_bots: 1,
      n_markets: 2,
      last_fill_at: null,
    },
  ];

  it("returns one datum per strategy", () => {
    const result = strategyPnlData(strats);
    expect(result).toHaveLength(2);
  });

  it("sorts by gross_pnl descending", () => {
    const result = strategyPnlData(strats);
    expect(result[0].name).toBe("momentum_v1");
    expect(result[1].name).toBe("mean_reversion_v1");
  });

  it("maps pnl correctly", () => {
    const result = strategyPnlData(strats);
    expect(result[0].pnl).toBe(15.0);
    expect(result[1].pnl).toBe(-8.0);
  });

  it("returns empty array for empty input", () => {
    expect(strategyPnlData([])).toEqual([]);
  });

  it("does not mutate the input array", () => {
    const input = [...strats];
    strategyPnlData(strats);
    expect(strats).toEqual(input);
  });
});

// ---------------------------------------------------------------------------
// marketExposureData
// ---------------------------------------------------------------------------

describe("marketExposureData", () => {
  const markets: MarketExposure[] = [
    {
      market_id: "market-b",
      gross_pnl: 5.0,
      realized_pnl: 5.0,
      unrealized_pnl: 0,
      n_bots: 1,
      n_orders: 4,
      last_fill_at: null,
    },
    {
      market_id: "market-a",
      gross_pnl: 20.0,
      realized_pnl: 18.0,
      unrealized_pnl: 2.0,
      n_bots: 2,
      n_orders: 12,
      last_fill_at: "2026-05-09T12:00:00Z",
    },
  ];

  it("sorts by gross_pnl descending", () => {
    const result = marketExposureData(markets);
    expect(result[0].name).toBe("market-a");
    expect(result[1].name).toBe("market-b");
  });

  it("maps n_bots correctly", () => {
    const result = marketExposureData(markets);
    expect(result[0].n_bots).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// failureCountByKind
// ---------------------------------------------------------------------------

describe("failureCountByKind", () => {
  const events: FailureEvent[] = [
    { ts: "2026-05-09T10:00:00Z", bot_id: "bot-1", kind: "order_rejected", detail: null },
    { ts: "2026-05-09T10:01:00Z", bot_id: "bot-1", kind: "order_rejected", detail: null },
    { ts: "2026-05-09T10:02:00Z", bot_id: "bot-2", kind: "bot_crash", detail: "OOM" },
    { ts: "2026-05-09T10:03:00Z", bot_id: "bot-1", kind: "order_rejected", detail: null },
  ];

  it("counts correctly", () => {
    const result = failureCountByKind(events);
    const rejectedRow = result.find((r) => r.kind === "order_rejected");
    const crashRow = result.find((r) => r.kind === "bot_crash");
    expect(rejectedRow?.count).toBe(3);
    expect(crashRow?.count).toBe(1);
  });

  it("sorts by count descending", () => {
    const result = failureCountByKind(events);
    expect(result[0].kind).toBe("order_rejected");
  });

  it("returns empty for empty input", () => {
    expect(failureCountByKind([])).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// strategyWinRates
// ---------------------------------------------------------------------------

describe("strategyWinRates", () => {
  it("computes win rate as wins/(wins+losses)", () => {
    const strats: StrategyRollup[] = [
      {
        strategy: "s1",
        wins: 3,
        losses: 1,
        gross_pnl: 0,
        realized_pnl: 0,
        unrealized_pnl: 0,
        n_orders: 4,
        n_bots: 1,
        n_markets: 1,
        last_fill_at: null,
      },
    ];
    const result = strategyWinRates(strats);
    expect(result[0].win_rate).toBeCloseTo(0.75);
    expect(result[0].total).toBe(4);
  });

  it("returns 0 win_rate when wins and losses are both 0", () => {
    const strats: StrategyRollup[] = [
      {
        strategy: "s1",
        wins: 0,
        losses: 0,
        gross_pnl: 0,
        realized_pnl: 0,
        unrealized_pnl: 0,
        n_orders: 0,
        n_bots: 1,
        n_markets: 1,
        last_fill_at: null,
      },
    ];
    expect(strategyWinRates(strats)[0].win_rate).toBe(0);
  });
});
