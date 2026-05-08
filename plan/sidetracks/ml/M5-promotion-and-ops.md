# M5 — Promotion + ops

**Goal:** Define the gate a model must pass to reach live, the cadence at which it gets retrained, and the monitoring that catches it going wrong.

## Promotion gate (in order; each must pass)

1. **Reproducibility.** Re-running training with the same `data_snapshot_id`, code git sha, and hyperparams produces a model with the same digest. If not, the model is discarded.
2. **Backtest.** Realized-return CI on a held-out window is positive after costs.
3. **OPE (RL only).** Doubly-robust estimator CI clears the conditions in M3.
4. **Beats fallback.** Backtest return CI is strictly above the M2 supervised baseline's CI on the same window.
5. **Paper.** Runs in paper for the platform-minimum window (per `.claude/rules/07-testing.md`) and the realized return falls inside the backtest CI. A paper run that disagrees with the backtest is a stop, not a near-miss.
6. **Drift profile recorded.** PSI thresholds per feature are documented in the model metadata.
7. **Live promotion.** Same three locks as any other strategy (invariant 1) — `--mode live` flag, per-bot `live: true`, manager-level live allow-list. Adds: ML-specific operator sign-off recorded in `audit_log`. The model digest is pinned in `config/bots.yaml`; `latest` resolution is forbidden in live mode (drift toward an unintended model is unacceptable).

A failure at any step demotes the model to "evaluated, not promoted" and the gate restarts at step 1 after fixes.

## Retraining cadence

- **Time-based:** retrain weekly for fast-moving markets, monthly otherwise. Documented per `model_id`.
- **Drift-triggered:** PSI crossing the warn threshold for >24h triggers a retrain even if the time-based cadence hasn't elapsed.
- **Performance-triggered:** rolling realized return below a configured floor for >48h triggers a retrain.
- A retrained model goes through the **full promotion gate** every time. There is no "small fix" path that skips paper.

## Monitoring (extends Phase 6a)

- `model_inference_latency_seconds` — per `model_id`.
- `model_drift_score` — per `(model_id, feature)`.
- `model_fallback_active{model_id}` — gauge.
- `model_version_running{model_id}` — labeled by digest.
- `model_realized_return_window` — rolling realized return attributable to the model's signals.

## Runbooks (added to Phase 9's set)

- "ML signal degrading — when to retrain, when to stop the bot."
- "Fallback chain exhausted — strategy receiving 'no opinion' indefinitely."
- "Forced rollback to a prior model digest."

## Exit criteria

- [ ] One model has gone through the full promotion gate end-to-end (M2 model is the natural candidate).
- [ ] A scheduled retrain has produced a new model that re-passes the gate; the live bot has been hot-reloaded onto it.
- [ ] A simulated drift trip has demoted a live model to fallback, recovered, and been investigated; the runbook documented the actual diagnosis.
- [ ] No model has run live with `latest`-resolution; every live `MLSignal` is pinned to a digest in `config/bots.yaml`.
