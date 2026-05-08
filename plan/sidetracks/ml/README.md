# Sidetrack — ML / RL signals

A parallel track to the main plan. Trains models offline on historical data and exposes them to bots through the existing `Signal` interface — so the rest of the platform doesn't know a signal is "an ML model" vs. "a Coingecko price."

## Why this is a sidetrack, not a phase

- It depends on the main plan being far enough along that historical data exists (Phase 3a's `signal_samples`, Phase 6's `audit_log`, Phase 4's market data captures).
- Training is **off-host**. The production VPS does inference only; training runs on a workstation or rented GPU box. Mixing those concerns into the main phases bloats them.
- It is optional. The platform must work with hand-coded signals first; ML signals layer on top.

## Hard architectural commitments

These are non-negotiable; they keep the ML track from undermining the main plan's invariants.

1. **Models are signals, not strategies.** A model emits a typed observation; a strategy decides what to do with it. Risk caps, idempotency, kill switch — all enforced exactly as before, in `BaseBot.place`. A bad model cannot bypass any control.
2. **Train off-host.** No GPU deps, no PyTorch, no large parquet files in the production image. The serving subset of code is a thin package that loads a versioned artifact and runs forward passes.
3. **Train/serve symmetry.** Feature transforms live in **one** module imported by both training and serving. A divergence between training-time and inference-time features is the most common silent failure mode in ML systems; we close that door at the package boundary.
4. **Models are immutable, versioned artifacts.** Stored in object storage by digest, registered in Postgres with the training-run metadata that produced them. A model version names: code git sha, data snapshot id, hyperparams, reward fn (RL), training duration.
5. **Boring baselines first.** A logistic regression or LightGBM gets the pipeline end-to-end before any deep model — let alone RL — is attempted. If a baseline can't beat random in paper, the problem isn't the model.
6. **Offline RL only, initially.** No on-policy exploration in real markets. The exploration cost is paid in real money and is unacceptable until the offline track has demonstrated stable performance live for an extended period.
7. **Off-policy evaluation is mandatory** before any RL policy reaches paper. Naive backtests on logged behavior data are biased; use importance-weighted return + a doubly-robust estimator. If OPE confidence intervals straddle zero, the policy doesn't ship.
8. **Drift detection auto-demotes.** Input-distribution drift past documented thresholds switches the bot to a fallback signal (typically the prior baseline) and emits `warn`. The strategy keeps running; the model gets investigated.
9. **Audit trail.** Every order influenced by an ML signal logs `(model_version, input_features_hash, model_output)` to `audit_log`. Required for replay when a bad trade lands.

## Phases

| # | File | Output |
|---|---|---|
| M0 | this file | Architectural commitments + integration points |
| M1 | [M1-data-foundation.md](M1-data-foundation.md) | Historical data collection, snapshot store, train/serve feature module |
| M2 | [M2-supervised-baseline.md](M2-supervised-baseline.md) | First end-to-end loop with a boring supervised model |
| M3 | [M3-offline-rl.md](M3-offline-rl.md) | Offline RL (CQL / IQL) + off-policy evaluation |
| M4 | [M4-model-serving.md](M4-model-serving.md) | `MLSignal`, model registry, hot-reload, fallback, drift detection |
| M5 | [M5-promotion-and-ops.md](M5-promotion-and-ops.md) | Promotion gate (backtest → OPE → paper → live), retraining cadence, monitoring |

## Integration points with the main plan

| Hooks into | What changes there |
|---|---|
| Phase 3a — Signals platform | `MLSignal(Signal)` is just another signal class. No change to the `Signal` ABC. |
| Phase 4 — Polymarket adapter | Adds nothing. Adapter unaware of ML. |
| Phase 6 — Risk + alerting | Adds nothing in the risk path. Alerting gains a new severity event: `ModelDrift`. |
| Phase 6a — Observability | Adds metrics: `model_inference_latency_seconds`, `model_drift_score`, `model_fallback_active`, `model_version_running`. |
| Phase 7 — First strategy | A second strategy variant `momentum_v1_ml` may consume an `MLSignal` alongside the BTC signal. The original `momentum_v1` is unchanged and stays the control. |
| Phase 9 — Promotion + ops | Promotion gate gains the OPE check; rotation calendar gains "model retraining cadence." |

## Prerequisites before starting M1

- Phase 3a complete: `signal_samples` is being written.
- Phase 4 complete: at least one market is being paper-traded so book-state captures exist.
- Phase 6a complete: structured logs with `tick_id` so feature lookup at training time is straightforward.

Starting M1 before these exist means inventing data sources, which leads to training/serving divergence.

## What lives where

- `ml/` — top-level sibling to `src/`. Its own `pyproject.toml`. Heavy deps (polars, pyarrow, scikit-learn, lightgbm, pytorch optional, d3rlpy or similar for offline RL).
- `ml/features/` — **the only module imported by both training and serving.** Pure functions, deterministic, versioned.
- `ml/training/` — pipelines, sweeps, OPE harness. Never imported in production.
- `ml/artifacts/` — local cache of downloaded models for inference; populated from object storage at startup.
- `src/signals/ml_signal.py` — the production `Signal` subclass that loads an artifact and emits typed outputs. Imports `ml.features` (read-only) and the artifact loader. Nothing else.

This split is the single most important structural decision in the sidetrack. Get it wrong and training/serving will silently diverge.

## Hardware

- Local workstation with a single GPU is sufficient for the supervised baseline and for behavioral cloning.
- Offline RL benefits from more compute. Rent rather than own; spot/preemptible GPUs are fine because training is restartable.
- Production VPS does **not** get a GPU. Models that don't fit CPU-inference latency budgets do not ship.
