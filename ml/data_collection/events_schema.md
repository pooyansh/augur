# `events/` — Tier 1 dataset

**Grain:** one row per recurring-market settlement window (one closed Gamma event) for a given
`(venue, series_id)`.

| Column | Meaning |
|---|---|
| `venue` | Exchange/venue this window was collected from (e.g. `"polymarket"`). Present so a future non-Polymarket collector's output is distinguishable in the same dataset shape. |
| `series_id` | The venue's series id this window belongs to — the collector's actual partitioning key. |
| `window_slug` | Unique id for the window (Gamma event slug). |
| `market_name` | The constant series name (e.g. `"BTC Up or Down 5m"`) — the same for every row in one collection. Not a training feature. |
| `window_name` | The specific per-window human-readable title. Not a training feature. |
| `condition_id` | The venue's market id for this window. Joins to any future per-second/trade datasets for the same window. |
| `token_id_up`, `token_id_down` | Outcome token ids for the "up" and "down" sides of this window, when the series uses up/down-style binary outcomes. |
| `start_ts`, `end_ts` | Window open/close time (UTC). |
| `price_to_beat` | Reference price at window open — the threshold the window resolves against. Decimal-compatible string, may be null if not yet populated by the venue at collection time. |
| `final_price` | Reference price at window close. Decimal-compatible string, may be null (observed: the venue sometimes has not populated this yet even on a "closed" event). |
| `outcome` | Which side actually won (`"UP"` / `"DOWN"` for this series' outcome labels), derived from the settlement prices. Null if not resolvable. |
| `lifetime_volume` | Total volume traded in this window (venue's own rollup), Decimal-compatible string. |

Money/price fields round-trip through Parquet as strings, never native floats, per
`.claude/rules/00-stack.md` rule 6.
