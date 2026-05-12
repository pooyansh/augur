# Rule 04 — Alerting

## Severity routing

| Severity   | Sinks                    | Examples                                                      |
|------------|--------------------------|---------------------------------------------------------------|
| `info`     | Discord                  | Bot started, snapshot written                                 |
| `warn`     | Discord + Slack          | Signal stale, retryable order error, drawdown 50%             |
| `critical` | Slack + Telegram (pages) | Order rejected, kill switch tripped, bot crash-loop           |

Implemented in `src/alerts/router.py` — `_DEFAULT_ROUTING`.

## Dedup-key construction

```
key = blake2s(f"{bot_id}|{severity}|{template_key}|{dedup_extra}".encode(), digest_size=8).hexdigest()
```

- `bot_id`: stable bot identifier
- `severity`: `info` / `warn` / `critical`
- `template_key`: short stable string for the alert type (e.g. `"signal_stale"`)
- `dedup_extra`: optional extra discriminator (default `""`)

Suppression window: **300 seconds** (5 minutes).  After that, the same key is
re-emitted.  See `src/alerts/dedup.py`.

## Redaction-on-send mandate (invariant 4)

**Every** message MUST be passed through `RedactionFilter._redact` before
being handed to any sink.  This is enforced in `AlertRouter.send()`:

```python
safe_message = self._redactor(message)
```

The `_redactor` callable comes from `make_redactor(secret_values)` in
`src/alerts/redact.py`, backed by the same `RedactionFilter` class used by
the log handler.

**Tests must assert** that outbound HTTP bodies do not contain any plaintext
secret.  See `tests/unit/test_alert_redaction.py`.

## Sink failure semantics

Sink failures (network errors, HTTP 4xx/5xx) are:
1. Logged at `warning` level with the sink name and error.
2. **Never raised to the caller** — alerting is best-effort.
3. Other sinks continue to be attempted (failures are isolated per sink).

Implementation in `AlertRouter._send_to_sink()`.

## Per-bot alert override (forward-looking)

`BotEntry.alerts` in `config/bots.yaml` is a reserved field for per-bot
routing overrides (e.g. directing a specific bot's `critical` alerts to a
different Slack channel).  Implementation is deferred until a second strategy
needs it.  The config field already exists in the schema.

## Sink configuration

Webhooks are loaded from `secrets/alerts.enc.yaml` (decrypted at entrypoint).
Schema:

```yaml
slack:
  webhook_url: "https://hooks.slack.com/..."
discord:
  webhook_url: "https://discord.com/api/webhooks/..."
telegram:
  bot_token: "..."
  chat_id: "-100..."
```

Missing or incomplete config for a sink: `make_default_router` logs `warning`
and disables that sink (never raises).
