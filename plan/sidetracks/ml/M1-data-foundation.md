# M1 — Data foundation

**Goal:** Make historical data trainable. Establish the train/serve feature boundary that every later phase depends on.

## Deliverables

- `ml/pyproject.toml` — separate package, polars + pyarrow + scikit-learn + lightgbm. Optional pytorch under an extra. **Never imported by `src/`.**
- **Snapshot store.** A nightly job exports the canonical training corpus to versioned parquet files in object storage:
  - `signal_samples/yyyy=/mm=/dd=` — all signal samples (Phase 3a's table).
  - `book_snapshots/...` — periodic orderbook captures from the Polymarket adapter (Phase 4).
  - `audit_log/...` — every order intent + result.
  - `bot_state/...` — daily snapshot of latest `bot_state` rows for replay context.
  - Each export is content-addressed: a `data_snapshot_id` (sha256 of the manifest) names a fully reproducible training input.
- **Manifest registry.** A `data_snapshots` table in Postgres records each snapshot's id, time range, row counts, and storage URI. Training runs reference a snapshot id, never raw paths.
- **`ml/features/` module.**
  - Pure functions: `(market_state, signal_history, position) -> FeatureRow`. Deterministic. Hash of the module's source code is part of the model artifact metadata.
  - Lives in `ml/features/` and is imported by both training (`ml/training/`) and serving (`src/signals/ml_signal.py`).
  - **Property test:** for any `(t, market)` tuple in the historical corpus, computing features at training time from the snapshot equals computing them at serving time from a synthesized live state. This is the train/serve symmetry guarantee; if the test breaks, the divergence between training and serving has reopened.
- **Backtest data slicer.** Given a `(market, time_range, data_snapshot_id)`, produce a deterministic iterable of `(features, label)` tuples for supervised training, or `(state, action, reward, next_state, done)` tuples for offline RL. Re-uses Phase 3a's replay where possible.

## Reward function (forward-looking, lives here)

Reward design is hard, opinionated, and easy to get wrong. Define it now so M2 and M3 share a single reward module:

- `ml/rewards/` — versioned reward functions. Each is a pure function with a stable name (e.g. `pnl_minus_drawdown_v1`).
- Default reward: realized PnL minus a drawdown penalty minus a turnover penalty. Parameters are explicit, not tuned silently.
- The reward function name + version is recorded in every training-run metadata row; comparing two RL models trained against different rewards is meaningless without it.

## Out of scope

- Any model training. M1 ends when the data is queryable and features are reproducible.
- Any deep-learning infrastructure. CPU-friendly tabular workflows only.

## Exit criteria

- [ ] One full `data_snapshot_id` covers ≥30 days of paper-mode operation.
- [ ] Train/serve symmetry test passes on at least 1000 randomly sampled `(t, market)` tuples.
- [ ] A backtest data slicer call is reproducible: same `data_snapshot_id` + same code git sha → bit-identical output.
- [ ] No `ml/` import appears anywhere in `src/` outside `src/signals/ml_signal.py` (enforced by an import-linter rule).
