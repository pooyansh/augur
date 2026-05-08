# M2 — Supervised baseline

**Goal:** Get a *boring* model end-to-end through training, registry, paper, and into a strategy's decision loop. Prove the pipeline works on something cheap before spending compute on RL.

If a logistic regression can't beat random in paper, the failure is in features, labels, or data — not in model capacity. M3 (RL) will not fix it.

## Deliverables

- **Task definition.** One concrete, narrow predictive task: e.g. "given current features at time `t`, predict whether the mid-price 15 minutes ahead is above or below the current mid by more than the round-trip cost." Binary classification. Documented in `ml/tasks/`.
- **Training pipeline** (`ml/training/supervised.py`):
  - Reads a `data_snapshot_id` (M1).
  - Splits time-respecting (no leakage from future to past).
  - Trains a baseline (logistic regression) and a stronger baseline (LightGBM).
  - Evaluates: AUC, calibration, profit-aware metric (e.g. expected return after costs over the test window).
  - Writes a **training-run metadata row** to Postgres: `code_git_sha`, `data_snapshot_id`, `feature_module_hash`, `task_name`, `hyperparams`, `metrics`, `artifact_uri`, `artifact_digest`.
  - Uploads the model artifact (joblib / ONNX) to object storage by digest.
- **No live serving yet.** This phase's exit is a model that scores well in backtest and **passes a paper-mode validation that matches the backtest**, not a model running in a bot.
- **Backtest harness** (`ml/training/backtest.py`):
  - Replays the model's predictions through a paper-mode strategy variant against the same `data_snapshot_id`.
  - Reports realized return, max drawdown, trade count, hit rate.
  - **Confidence:** bootstrap the test window into N folds; report the realized-return CI. A model whose CI straddles zero is not promotable, regardless of its mean.

## What this phase exposes (and why it matters)

- The first end-to-end run will reveal label leakage, feature lookahead, or train/serve drift. Fix here, where the cost is low — not later when an RL policy is misbehaving.
- The supervised baseline becomes the **fallback signal** that future ML signals fall back to under drift (M4).
- A "best supervised baseline" gives M3's RL track a concrete bar to clear: if offline RL doesn't beat LightGBM, RL doesn't ship.

## Out of scope

- Any RL.
- Hyperparameter sweeps beyond a sensible default grid.
- Deep models. If the baseline doesn't work, deeper won't either.

## Exit criteria

- [ ] One trained model artifact in object storage, registered in Postgres, with full reproducibility metadata.
- [ ] Backtest realized-return CI is positive after costs on a held-out window of ≥30 days.
- [ ] **Backtest-to-paper consistency:** running the same model in paper mode for ≥7 days produces a realized return inside the backtest CI. If they disagree, either the paper-mode slippage model (Phase 4) is wrong or train/serve symmetry is broken — investigate before proceeding.
- [ ] Re-running training with the same `data_snapshot_id` + git sha + hyperparams produces a model with the same digest.
