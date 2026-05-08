# M3 — Offline reinforcement learning

**Goal:** Train an RL policy from logged trading data without exploring in the live market. Beat the M2 supervised baseline on the same backtest before anything reaches paper.

## Why offline only

Online RL in a real market means the bot intentionally takes suboptimal actions to learn. The exploration cost is paid in real money against an adversarial environment. We don't take it. Offline RL on logged data, with conservative algorithms, is the only acceptable starting point. Online fine-tuning is a future phase, gated by a long live track record of the offline policy.

## Deliverables

- **Environment definition** (`ml/env/`):
  - State: features from `ml/features/` + position state + recent signal history.
  - Action space: **discrete and small** — e.g. `{do_nothing, buy_small, buy_large, sell_small, sell_large, close}`. Continuous order sizing is a later optimization, not a starting point.
  - Reward: from `ml/rewards/` (the function defined in M1). Versioned. The reward fn used to train a model is part of its metadata.
  - Termination: market resolution, time horizon, or position close.
- **Behavioral cloning baseline** (`ml/training/bc.py`). Imitates the logged behavior policy. This is the floor: an offline RL algorithm that doesn't beat BC didn't learn anything beyond what's already in the logs.
- **Offline RL training** (`ml/training/offline_rl.py`):
  - Algorithm: start with **CQL** or **IQL** — both are conservative, well-studied, and tolerate medium-sized datasets. Library: `d3rlpy` or equivalent.
  - Trains against a `data_snapshot_id` containing real logged transitions.
  - Same metadata write as M2: code git sha, snapshot id, feature hash, **reward fn name + version**, hyperparams, artifact uri.
- **Off-policy evaluation harness** (`ml/training/ope.py`) — non-negotiable:
  - Importance-weighted return + a doubly-robust estimator.
  - Bootstrapped confidence intervals.
  - **Promotion gate:** OPE return CI must be (a) entirely above zero, (b) entirely above the BC baseline, (c) entirely above the M2 supervised baseline's realized return on the same window. Any of those failing → no paper.
  - Naive backtest replay is **not** a substitute for OPE. Document this in `.claude/rules/07-testing.md`.

## Risk-specific design constraints

- The action space cannot exceed what `BaseBot.place` will let through. A policy that wants to take 20 positions per minute is constrained by `max_orders_per_minute` regardless — train the policy under that constraint, don't let it learn behavior the platform will then block silently.
- Reward shaping should explicitly penalize drawdown and turnover. Reward = pure realized PnL teaches the policy to take maximum risk near the terminal state. Documented in `ml/rewards/`.
- The policy outputs a discrete action; the strategy translates it into an `OrderIntent` deterministically. The ML→risk boundary is in the strategy, not in the model.

## Out of scope

- Online fine-tuning.
- Multi-agent / multi-market policies. Single market, single bot.
- Continuous-action methods. Revisit only after discrete-action offline RL has been live and stable.

## Exit criteria

- [ ] BC baseline trained, evaluated, archived. Used as a floor.
- [ ] At least one offline RL policy whose OPE CI clears all three promotion-gate conditions above.
- [ ] Backtest-to-paper consistency check (as in M2) passes for the chosen policy.
- [ ] A second engineer (or appropriate subagent) can read the training run metadata and reproduce the model bit-for-bit.
