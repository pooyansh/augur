# SLOs — Prediction Market Bot Platform

These are **budgets, not contracts**. Missing them prompts investigation; they do not trigger a page. Review each window after a paper run and calibrate before promoting to live.

## 1. Tick On-Time Rate

**Target:** ≥ 99% of ticks complete within their scheduled interval (over a 7-day rolling window).

**Measurement:** `1 - (rate(bot_tick_overrun_total[7d]) / rate(bot_tick_latency_seconds_count[7d]))`

**Error budget:** 1% of ticks may overrun per week — for a 15-minute cadence bot running 7 days, that is ~100 overrun ticks before the budget is exhausted.

---

## 2. Snapshot Lag p99

**Target:** p99 snapshot lag < 60 seconds (over a 7-day rolling window).

**Measurement:** `histogram_quantile(0.99, sum by (le) (rate(bot_snapshot_lag_seconds[7d])))`

> Note: `bot_snapshot_lag_seconds` is a gauge, not a histogram, in v1. The SLO is checked operationally by inspecting the time-series. A histogram variant may be added in Phase 9 if alerting on this directly becomes necessary.

---

## 3. Order Placement Success Rate

**Target:** ≥ 99% of eligible order intents are accepted (over a 7-day rolling window).

**Denominator:** `result=accepted` + `result=rejected` only.
**Excluded from denominator:** `result=risk_blocked` and `result=kill_switch` — these are deliberate policy decisions, not failures.

**Measurement:**
```promql
sum(rate(order_intent_total{result="accepted"}[7d]))
/
(
  sum(rate(order_intent_total{result="accepted"}[7d]))
  + sum(rate(order_intent_total{result="rejected"}[7d]))
)
```

---

*Budgets, not contracts. Missing them prompts investigation; they don't page.*
