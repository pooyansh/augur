/**
 * Playwright smoke test — checks each route renders without console errors.
 *
 * Uses route interception to return recorded JSON fixtures instead of a
 * real backend. No live manager or Postgres required.
 */

import { test, expect } from "@playwright/test";

// Minimal fixture data matching the API response shapes.
const FIXTURES: Record<string, unknown> = {
  "/api/health": { healthy: true, postgres_ok: true, all_bots_alive: true },
  "/api/status": {
    bots: [
      {
        bot_id: "smoke-bot",
        strategy: "momentum_v1",
        mode: "paper",
        pid: 12345,
        restart_count: 0,
        heartbeat_age_s: 5.0,
        snapshot_lag_s: 10.0,
        last_error: null,
        spawned_at: "2026-05-09T12:00:00Z",
      },
    ],
    total_bots: 1,
    alive_bots: 1,
    paper_bots: 1,
    live_bots: 0,
  },
  "/api/bots": [
    {
      bot_id: "smoke-bot",
      strategy: "momentum_v1",
      market_id: "market-abc",
      mode: "paper",
      snapshot_at: "2026-05-09T12:00:00Z",
      version: 1,
      state: { mode: "paper" },
    },
  ],
  "/api/bots/smoke-bot": {
    bot_id: "smoke-bot",
    strategy: "momentum_v1",
    market_id: "market-abc",
    mode: "paper",
    snapshot_at: "2026-05-09T12:00:00Z",
    version: 1,
    state: { mode: "paper" },
    recent_audit: [],
  },
  "/api/strategies": {
    strategies: [
      {
        strategy: "momentum_v1",
        wins: 3,
        losses: 1,
        gross_pnl: 12.5,
        realized_pnl: 10.0,
        unrealized_pnl: 2.5,
        n_orders: 10,
        n_bots: 1,
        n_markets: 1,
        last_fill_at: null,
      },
    ],
  },
  "/api/strategies/momentum_v1": {
    strategy: "momentum_v1",
    summary: {
      strategy: "momentum_v1",
      wins: 3,
      losses: 1,
      gross_pnl: 12.5,
      realized_pnl: 10.0,
      unrealized_pnl: 2.5,
      n_orders: 10,
      n_bots: 1,
      n_markets: 1,
      last_fill_at: null,
    },
    bots: [],
  },
  "/api/markets": {
    markets: [
      {
        market_id: "market-abc",
        gross_pnl: 12.5,
        realized_pnl: 10.0,
        unrealized_pnl: 2.5,
        n_bots: 1,
        n_orders: 10,
        last_fill_at: null,
      },
    ],
  },
  "/api/audit": [],
  "/api/failures": { events: [], total: 0 },
  "/api/capital": {
    total_usd: 500.0,
    per_exchange: [{ exchange: "polymarket", balance: 500.0, currency: "USD" }],
    sourced_from: "bot_state.state.balance",
  },
};

test.beforeEach(async ({ page }) => {
  // Intercept all /api/* requests and return fixture JSON.
  await page.route(/\/api\//, async (route) => {
    const url = new URL(route.request().url());
    // Strip query params for fixture lookup.
    const path = url.pathname;
    const body = FIXTURES[path];
    if (body !== undefined) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    } else {
      await route.fulfill({ status: 404, body: '{"detail":"not found"}' });
    }
  });

  // Collect console errors.
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      console.error(`[browser console error] ${msg.text()}`);
    }
  });
});

const ROUTES = [
  { path: "/", name: "Overview" },
  { path: "/bots", name: "Bots" },
  { path: "/strategies", name: "Strategies" },
  { path: "/markets", name: "Markets" },
  { path: "/audit", name: "Audit" },
  { path: "/failures", name: "Failures" },
];

for (const { path, name } of ROUTES) {
  test(`${name} page renders without errors`, async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") errors.push(msg.text());
    });
    page.on("pageerror", (err) => errors.push(err.message));

    await page.goto(path);
    // Wait for any async rendering to settle.
    await page.waitForLoadState("networkidle");

    // Basic DOM check: the page must have rendered something beyond the root div.
    await expect(page.locator("body")).not.toBeEmpty();

    // Assert no console errors.
    expect(errors).toEqual([]);
  });
}

test("Bot detail page renders for smoke-bot", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));

  await page.goto("/bots/smoke-bot");
  await page.waitForLoadState("networkidle");

  await expect(page.locator("body")).not.toBeEmpty();
  expect(errors).toEqual([]);
});
