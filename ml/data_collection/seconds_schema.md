# `seconds/` — Tier 2 dataset

**Grain:** one row per `(window, token, second-of-window)` — i.e. one row per outcome token
per elapsed second within a settlement window, reconstructed from the venue's raw trade log
(there is no native per-second feed; see `ml/data_collection/trades.py` module docstring).

| Column | Meaning |
|---|---|
| `window_slug` | Joins back to `events/`. |
| `market_name` | Denormalized from `events/` — constant per series (e.g. `"BTC Up or Down 5m"`). |
| `window_name` | Denormalized from `events/` — the specific window's human-readable title. |
| `token_id` | Which outcome token (e.g. UP or DOWN side) this row's price/volume belongs to. |
| `outcome` | Which side actually won the window (denormalized from `events/`, same value on every row of a window regardless of `token_id`), so a human/model can read a row without a join. |
| `second_offset` | `0`–`window_seconds - 1` — seconds elapsed since window start. The natural "time-within-episode" index for a bandit/RL state. |
| `ts` | Absolute UTC timestamp (`start_ts + second_offset`). |
| `price` | Last-trade price for this token as of this second, Decimal-compatible string. **Forward-filled** from the prior second when no trade occurred in this exact second. **Null** for leading seconds before the first trade of the window for this token — this is intentionally *not* fabricated; a pre-window price anchor to seed these leading seconds is Tier 3 (`price_anchor/`), out of scope for this dataset. |
| `volume` | Sum of trade sizes (shares) for this token in this exact second, Decimal-compatible string. `"0"` (not null) if no trade occurred. |
| `trade_count` | Number of individual trades in this second — `0` if none. A cheap liquidity/activity signal distinct from volume (many small trades vs. one big one). |

Money/size fields round-trip through Parquet as strings, never native floats, per
`.claude/rules/00-stack.md` rule 6.

## Known gap (documented, not a bug)

Trade timestamps from `data-api.polymarket.com/trades` have one-second resolution only (no
sub-second precision). When multiple trades for the same token land in the same second, the
"last" trade used for that second's `price` is determined by the order trades were returned by
the API, not true sub-second chronology — the API does not expose finer ordering. This is a
known limitation of the source data, not a collector bug.
