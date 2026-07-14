# 10 — Provisional winning rules

## What a winning rule is

Official settlement on some recurring markets (e.g. BTC Up/Down 5-min on
Polymarket) lags the window's actual close by seconds to minutes — the
exchange's own `closed`/`winner` fields simply aren't available yet. A
**provisional winning rule** (`WinningRule`, `src/rules/base.py`) is a pure,
synchronous, deterministic heuristic that lets a strategy make an early,
independent judgment call — WON / LOST / UNDECIDED — about a position it
currently holds, using signals already fetched that tick. Its sole purpose is
to inform a strategy's own **continuation decision** (e.g. stop placing
further orders, cancel resting orders for this window) while waiting for real
settlement.

This is **optional per bot** — most bots never configure one. It's a
strategy-author opt-in, not new mandatory infrastructure. A bot with no
`winning_rule` configured behaves byte-identical to a bot built before this
feature existed.

## Hard invariant — never feeds P&L, position, or real settlement

**A provisional ruling must never feed P&L bookkeeping, the `position` field
in `snapshot()`, or any audit kind that implies real settlement.** It exists
solely to inform a strategy's own continuation decision, consulted from
inside the strategy's own `on_tick`. This is the same spirit as the existing
CLAUDE.md invariant "risk checks live in `BaseBot.place`, not strategies" —
the framework computes the judgment, strategies decide what (if anything) to
do with it, and neither `perf_rollup` nor the dashboard nor any P&L query may
ever read `KIND_PROVISIONAL_RULING` audit rows as if they were authoritative.

Real settlement detection (`PolymarketAdapter.poll_settlement`,
`scripts/snipe_bot.py`'s `_poll_settlement`) is unchanged by this feature and
remains the sole source of authoritative outcome/P&L.

## Market-scoped naming and storage

Winning rules are stored **market-scoped**, mirroring the same
`<venue>/<series_slug>/` partitioning principle used elsewhere in the
codebase — rules must not pile up as an undifferentiated flat list as more
market families are added:

```
src/rules/<venue>/<series_slug>/<rule_module>.py
```

e.g. `src/rules/polymarket/btc_up_or_down_5m/price_compare.py`.

The registry key is a **fully-qualified dotted name**, not a bare short
name:

```
"<venue>.<series_slug>.<rule_name>"
```

e.g. `"polymarket.btc_up_or_down_5m.price_compare"`. This makes collisions
across market families structurally impossible — two different series can
each register a rule literally called `price_compare` without conflict — and
makes the registry self-documenting about which market a rule targets.
`BotEntry.winning_rule.name` in `config/bots.yaml` is always this
fully-qualified dotted name.

`validate_bot_winning_rule` (`src/manager/supervisor.py`) additionally
sanity-checks that the referenced rule's `<venue>` prefix matches the bot
entry's own `market.exchange`, catching a copy-paste config mistake (e.g. a
BTC bot accidentally referencing an ETH-series rule) at spawn time.

## Expected behavior caveat — flip-flopping is normal

Unlike final settlement (a one-time authoritative event), a provisional
ruling is recomputed every tick from live data and **can flip between
WON/LOST/UNDECIDED** multiple times before the window actually closes (e.g.
BTC price oscillating right around the entry reference). This is expected,
not a bug. Any strategy consuming `provisional_ruling()` should be written
defensively — e.g. only act after N consecutive ticks of the same ruling,
never assume a single WON tick means money has been received. Rules may
expose a `tolerance_pct`-style param (see `PriceCompare`) to narrow how often
this happens near the boundary, but flip-flopping near the entry price is
still expected/normal even with a tolerance configured.

## How `BaseBot` integration works

- `WinningRule.evaluate(ctx: WinningRuleContext) -> ProvisionalRuling` is
  pure and synchronous: no I/O, no mutation beyond the single call, same
  inputs always produce the same output.
- `BaseBot.current_position(self) -> PositionState | None` is an **optional**
  override (default returns `None`) — strategies that want the feature
  implement it to expose their already-tracked position.
- `BaseBot.provisional_ruling(self) -> ProvisionalRuling | None` is
  **provided by base — do not override**. It returns the cached result of
  the most recent evaluation.
- `run()` evaluates the configured rule (if any) once per tick, between the
  kill-switch check and `on_tick`, whenever `current_position()` returns
  non-`None`. A `KIND_PROVISIONAL_RULING` audit row (`src/risk/audit.py`) is
  written only when the ruling **changes** from the previous tick — this
  avoids flooding `audit_log` every tick while still capturing every
  transition.
- A rule that raises inside `evaluate()` never crashes the tick loop: the
  error is logged and the cached ruling degrades to `None` rather than
  serving a stale value.
- **The ruling never triggers action on its own.** A strategy consults
  `self.provisional_ruling()` inside its own `on_tick` and decides what to
  do (e.g. add a cancel to `Decision.cancels`, or skip placing new intents
  this tick).

## Config — `BotEntry.winning_rule`

```yaml
bots:
  - id: btc-updown-5m-1
    strategy: momentum_v1
    market: {exchange: polymarket, slug: btc-updown-5m, outcome: UP}
    winning_rule:
      name: polymarket.btc_up_or_down_5m.price_compare
      params:
        tolerance_pct: "0.05"
    # ...
```

`winning_rule` is absent by default. Setting it is the only thing required
to opt a bot into provisional rulings being computed — the strategy itself
must still implement `current_position()` to actually get a non-`None`
ruling.

## Adding a new winning rule

1. Create `src/rules/<venue>/<series_slug>/<rule_name>.py` (create the
   `<venue>`/`<series_slug>` packages if they don't exist yet).
2. Subclass `WinningRule` (`src/rules/base.py`); set
   `name: ClassVar[str] = "<venue>.<series_slug>.<rule_name>"`.
3. Implement `evaluate(self, ctx: WinningRuleContext) -> ProvisionalRuling`
   — pure, synchronous, deterministic. Return `UNDECIDED` whenever there
   isn't enough information; never raise for "don't know yet."
4. Decorate the class with `@winning_rule`
   (`from src.rules.registry import winning_rule`).
5. `autodiscover()` picks it up automatically (recursive walk of
   `src/rules/`) — no manual registration needed.
6. Write unit tests against the fakes-only convention
   (`.claude/rules/07-testing.md`) — `evaluate()` never needs I/O or mocks.

## See also

- `src/rules/base.py` — `ProvisionalRuling`, `PositionState`,
  `WinningRuleContext`, `WinningRule`
- `src/rules/registry.py` — `WinningRuleRegistry` (recursive auto-discovery)
- `src/rules/polymarket/btc_up_or_down_5m/price_compare.py` — reference
  implementation
- `src/manager/config.py` — `WinningRuleRef`, `BotEntry.winning_rule`
- `src/manager/supervisor.py` — `validate_bot_winning_rule`
- `src/bots/base.py` — `BaseBot.current_position`,
  `BaseBot.provisional_ruling`, `BaseBot._evaluate_winning_rule`
