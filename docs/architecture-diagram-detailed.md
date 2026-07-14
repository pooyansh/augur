# Architecture — Detailed Diagram

Zoomed-in view of the internal flows the high-level diagram (`architecture-diagram.md`) collapses into single boxes. Three parts: the bot tick loop, the manager's control surface (including the new stop-bot path), and the offline `ml/` pipeline.

## 1. One bot tick — `BaseBot.run()`

```mermaid
flowchart TD
    start(["Tick fires<br/>(per bot.schedule)"]) --> s1["1. signals = await deps.signals.snapshot_for(config)"]
    s1 --> s2["2. await deps.heartbeat.beat()<br/>→ one-way JSONL over unix socket to manager"]
    s2 --> ks{"3. kill_switch.is_tripped()?"}
    ks -->|yes, first tripped tick| cascade["cancel_all(market_id) once<br/>(KillSwitchCascade)"]
    cascade --> nextTick(["sleep until next tick"])
    ks -->|yes, already cascaded| nextTick
    ks -->|no| wr{"winning_rule configured<br/>AND current_position() != None?"}
    wr -->|yes| evalRule["evaluate WinningRule.evaluate(ctx)<br/>pure, sync, no I/O<br/>→ cache provisional_ruling()"]
    wr -->|no| onTick
    evalRule --> auditRuling{"ruling changed<br/>since last tick?"}
    auditRuling -->|yes| writeAudit1["audit.write(KIND_PROVISIONAL_RULING)"]
    auditRuling -->|no| onTick
    writeAudit1 --> onTick["4. decision = await self.on_tick(signals)<br/>strategy may call self.provisional_ruling() here"]
    onTick --> cancels["5. for coid in decision.cancels:<br/>await adapter.cancel(coid)"]
    cancels --> intents["6. for tmpl in decision.intents:<br/>await self.place(tmpl)"]
    intents --> place1["dedup check (_inflight cache)"]
    place1 --> place2["check_caps() — RiskCapExceeded?"]
    place2 --> place3["audit.write(order_submitted)"]
    place3 --> place4["adapter.place(intent)"]
    place4 --> place5["audit.write(order_accepted/rejected)"]
    place5 --> snap["7. await self._persist_snapshot()<br/>best-effort, warn on failure"]
    snap --> metrics["8. push tick metrics<br/>(Prometheus)"]
    metrics --> nextTick
```

**What's new here (this session):** step 3.5 — the optional winning-rule evaluation. It sits between the kill-switch check and `on_tick`, is entirely skippable (no config, no `current_position()` override → always `None`, zero cost), and never influences steps 5/6 on its own — the strategy has to read `self.provisional_ruling()` from inside its own `on_tick` and decide what to do.

## 2. Manager control surface

```mermaid
flowchart LR
    subgraph manager["Manager process"]
        sup["Supervisor"]
        hb["HeartbeatServer<br/>(one-way: bot → manager only)"]
        dash["DashboardServer"]

        subgraph dashapi["Dashboard API — two routers"]
            readRouter["router / ops_router<br/>ALL GET<br/>/api/bots, /api/strategies,<br/>/api/markets, /api/audit,<br/>/api/failures, /api/capital<br/>← reads via dashboard_reader<br/>(SELECT-only Postgres role)"]
            controlRouter["control_router<br/>ONE route:<br/>POST /api/control/bots/{id}/stop<br/>← in-process call, zero DB writes"]
        end
    end

    cli["Operator CLI<br/>start · reload · drain · status"] --> sup
    browser["Browser<br/>(127.0.0.1, or via SSH tunnel)"] --> readRouter
    browser -->|"Stop bot button<br/>(confirm-guarded)"| controlRouter

    controlRouter -->|"supervisor.stop_bot(id)"| sup
    controlRouter -->|"audit.write(KIND_BOT_STOP_REQUESTED)"| auditlog

    sup -->|"asyncio.create_subprocess_exec"| botproc["bot subprocess"]
    botproc -.->|heartbeat JSONL| hb
    sup -->|"SIGTERM → grace_s → SIGKILL"| botproc

    readRouter -->|SELECT only| pg[("Postgres")]
    botproc -->|"snapshot + audit writes<br/>(write role)"| pg
    auditlog[("audit_log<br/>append-only")] -.-> pg

    classDef control fill:#fee,stroke:#c33
    class controlRouter,botproc control
```

**Key distinction, drawn explicitly because it's easy to conflate:**

| Mechanism | Scope | Effect | Reversible? |
|---|---|---|---|
| Global kill switch (`src/risk/`) | All bots | Freezes new order placement; process keeps running | Yes — `KillSwitchWriter.reset()` |
| `Supervisor.stop_bot(id)` (new) | One bot | Terminates the subprocess (SIGTERM → grace → SIGKILL) | No — bot must be respawned via `reload` |
| Provisional winning rule (new) | One bot's own decision | Purely informational; strategy decides whether to act | N/A — never touches control flow itself |

## 3. Offline `ml/` pipeline (Tier 1 + Tier 2 collectors)

```mermaid
flowchart LR
    gamma["Gamma API<br/>gamma-api.polymarket.com<br/>/events/keyset (after_cursor pagination)"]
    dataapi["Data API<br/>data-api.polymarket.com/trades<br/>(offset-paginated, caps at 3000)"]

    subgraph mlpkg["ml/data_collection/ (sibling package)"]
        t1["events.py — Tier 1<br/>collect_events(series_id, venue)<br/>one row per window"]
        t2["trades.py — Tier 2<br/>paginate + bucket into<br/>1-second bars per token"]
        ckpt[("checkpoint.jsonl<br/>resumable")]
    end

    gamma --> t1
    dataapi --> t2
    t2 <-.->|"resume on restart"| ckpt

    t1 --> events_pq[("events/<br/>&lt;venue&gt;/&lt;series_slug&gt;/<br/>yyyy=/mm=/dd=/*.parquet")]
    t2 --> seconds_pq[("seconds/<br/>&lt;venue&gt;/&lt;series_slug&gt;/<br/>yyyy=/mm=/dd=/*.parquet")]

    events_pq -.-> manifest1["manifest.json<br/>sha256(params) + row count"]
    seconds_pq -.-> manifest2["manifest.json"]

    events_pq -.->|"not built yet"| features["ml/features/"]
    seconds_pq -.->|"not built yet"| features
    features -.-> datasets["ml/datasets/<br/>bandit view / sequential view"]
    datasets -.-> training["ml/training/<br/>(not built yet)"]

    classDef notbuilt fill:#f5f5f5,stroke:#999,stroke-dasharray: 5 5
    class features,datasets,training notbuilt
```

Both collectors are fully generic — `series_id`/`venue` are always explicit arguments, never hardcoded. Today's only populated dataset is `polymarket/btc-up-or-down-5m/` (6 months of Tier 1, a 2-week pilot of Tier 2), but the same code works for any other Polymarket recurring-market series by passing a different `series_id`.
