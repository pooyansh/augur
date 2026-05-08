# M4 — Model serving

**Goal:** Make a trained artifact reachable from a bot via the existing `Signal` interface, with hot-reload, fallback, and drift detection.

## Deliverables

- **`MLSignal(Signal)`** in `src/signals/ml_signal.py`:
  - Constructor takes a `model_id` (logical name) and a `version` (digest or "latest").
  - On startup: pulls the artifact from object storage by digest, verifies the digest, loads it.
  - On each `sample()` call: computes features via `ml.features` (the **same** module used in training — invariant), runs forward pass, emits a typed `MLSignalOutput` (action distribution + scalar score + model version + feature hash).
  - Inference latency tracked per `Phase 6a`'s `model_inference_latency_seconds`.
- **Model registry** (`src/ml_registry/` — small read-only client over Postgres):
  - Resolve `(model_id, "latest")` → digest → object-storage URI → bytes.
  - Resolve a specific digest for pinning.
  - Never mutated from production. Promotion writes happen offline (M5).
- **Hot-reload**:
  - `MLSignal` checks the registry on a configured cadence. New `latest` for its `model_id` → fetch, verify, swap.
  - Swap is atomic per call: an in-flight `sample()` finishes against the old model; the next call uses the new model.
  - Swap emits an `info` alert with the old → new digest.
- **Fallback chain**:
  - Each `MLSignal` is configured with an ordered fallback list (e.g. `[ml_v3, ml_v2, supervised_baseline_v1, none]`).
  - If artifact load fails, inference raises, or drift detection trips → the next fallback is loaded and used. Emits `warn`.
  - The supervised baseline from M2 is the typical penultimate fallback; `none` (signal returns "no opinion") is the final one. The strategy must handle "no opinion" gracefully — its `on_tick` already does this by design.
- **Drift detection** (`src/signals/drift.py`):
  - Per-feature distribution compared against the training-snapshot distribution recorded in the model metadata. Population Stability Index (PSI) or KS test, evaluated on a rolling window.
  - Threshold per feature documented in the model's metadata (set during training, not at runtime). Crossing it → fallback + `warn`. Crossing a higher "severe" threshold → fallback + `critical`.
  - Drift state is observable as `model_drift_score` (Phase 6a metric).

## Hard constraints

- The production image installs the **serving subset** of `ml/` only — no training deps, no GPU libraries. Verified by an image-size budget in CI.
- `MLSignal` never writes to the model registry, never logs the artifact bytes, never sends artifact bytes to alerts.
- Inference must complete within the schedule's tick budget. A model that exceeds it falls back automatically and emits `warn`.
- Audit-log integration: when a strategy uses an `MLSignal` to decide an order, the resulting `audit_log` row records `(model_id, model_version, feature_hash, model_output)`. This is added to the Phase 6 audit-log schema as optional columns.

## Exit criteria

- [ ] A bot subscribed to an `MLSignal` runs through paper for ≥7 days using a model from M2 (the safer choice for first integration).
- [ ] Forced artifact-load failure → fallback chain activates, bot keeps running, alert fires.
- [ ] Forced drift (synthetic input perturbation) → drift score crosses threshold, fallback activates, alert fires.
- [ ] Hot-reload from one digest to another succeeds without missing a tick on a running paper bot.
- [ ] Image size with the serving subset is below the documented budget; training deps are not present in the image.
