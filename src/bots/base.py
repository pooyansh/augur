"""BaseBot abstract class and associated data contracts.

Every strategy subclasses :class:`BaseBot` and implements exactly three methods:
``on_tick``, ``snapshot``, and ``rehydrate``.  Everything else — client_order_id
generation, risk enforcement, audit writing, heartbeat, snapshot persistence — is
provided by the base and MUST NOT be overridden.

See .claude/rules/01-bot-contract.md for the full lifecycle contract.
"""

from __future__ import annotations

__all__ = [
    "BaseBot",
    "BotConfig",
    "BotDeps",
    "Clock",
    "Decision",
    "Heartbeat",
    "KillSwitchTripped",
    "LocalHeartbeat",
    "RiskCapExceeded",
    "RiskCaps",
    "Schedule",
    "SignalSnapshot",
]

import asyncio
import logging
import random as random_module
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import blake2s
from random import Random
from typing import Any, ClassVar

from src.exchanges.base import (
    ExchangeAdapter,
    Mode,
    OrderIntent,
    OrderResult,
    OrderTemplate,
)
from src.risk.audit import AuditLogger
from src.risk.caps import RiskCapExceeded, RiskCaps, check_caps
from src.risk.kill_switch import KillSwitchCascade, KillSwitchReader
from src.signals.base import SignalSnapshot
from src.state.repository import StateRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

# RiskCapExceeded is imported from src.risk.caps; re-exported here for
# backward compatibility with existing tests that import from src.bots.base.
# RiskCaps is also imported from src.risk.caps and re-exported below.


class KillSwitchTripped(RuntimeError):  # noqa: N818 — name prescribed by CLAUDE.md Phase 3 spec
    """Raised when ``place()`` is called and the kill switch is active.

    Signals to the caller that no order was submitted and they should not
    retry until the switch is reset.
    """


# ---------------------------------------------------------------------------
# Clock — injectable for deterministic tests
# ---------------------------------------------------------------------------


class Clock:
    """Thin ``datetime.now`` wrapper that tests can replace.

    Production code uses the default, which returns real UTC time.
    Tests inject a :class:`~tests.fixtures.clocks.ManualClock` to control
    time without sleeping.
    """

    def now(self) -> datetime:
        """Return the current UTC datetime."""
        return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class Heartbeat(ABC):
    """Abstract heartbeat interface.

    ``BaseBot.run`` calls ``await self._heartbeat.beat()`` each tick.
    Phase 5 will swap the default :class:`LocalHeartbeat` for a unix-socket
    implementation that the manager process monitors.
    """

    @abstractmethod
    async def beat(self) -> None:
        """Record that this bot is alive."""


class LocalHeartbeat(Heartbeat):
    """Phase 3 / v1 heartbeat — records last-beat time in process memory only.

    The manager's socket-based health check is a Phase 5 concern.  This impl
    satisfies the interface so ``BaseBot`` compiles and tests run.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or Clock()
        self._last_beat: datetime | None = None

    @property
    def last_beat(self) -> datetime | None:
        """UTC datetime of the most recent beat, or ``None`` if never called."""
        return self._last_beat

    async def beat(self) -> None:
        self._last_beat = self._clock.now()


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


class Schedule:
    """Simple fixed-interval schedule.

    v1 supports only "every N seconds".  A cron-style extension can be
    added later without changing the ``BaseBot.run`` contract.

    Args:
        every_seconds: Positive integer tick interval in seconds.
    """

    def __init__(self, every_seconds: int) -> None:
        if every_seconds <= 0:
            raise ValueError(f"every_seconds must be positive, got {every_seconds}")
        self.every_seconds = every_seconds

    def next_tick(self, now: datetime) -> datetime:
        """Return the next scheduled tick time at or after ``now``.

        Args:
            now: Reference UTC datetime.

        Returns:
            UTC datetime of the next tick, aligned to the interval boundary.
        """
        import math

        epoch = now.timestamp()
        boundary = math.ceil(epoch / self.every_seconds) * self.every_seconds
        # If now lands exactly on a boundary, schedule the *next* one.
        if boundary == epoch:
            boundary += self.every_seconds
        return datetime.fromtimestamp(boundary, tz=UTC)


# ---------------------------------------------------------------------------
# Config and dependency container
# ---------------------------------------------------------------------------


@dataclass
class BotConfig:
    """Static configuration for a single bot instance.

    Loaded from ``config/bots.yaml`` at startup.  Secret values are referenced
    by name; the secrets loader resolves them at runtime.

    Args:
        bot_id: Stable, unique identifier used as the ``client_order_id`` seed.
            Must never change after first deployment of this bot.
        strategy_name: Registry key used to look up the :class:`BaseBot` subclass.
        market_id: Canonical market identifier the bot trades.
        mode: Execution mode — ``paper`` (default) or ``live``.
        live: Per-bot live lock (invariant 1 / three-lock rule).  Must be
            ``True`` AND ``mode=live`` AND the bot id on the manager allow-list
            for live orders to transmit.
        schedule: Tick schedule.
        risk: Risk cap configuration.
        signal_subscriptions: List of signal names the bot subscribes to.
            The signals runner (Phase 3a) uses this to pre-fetch only the
            required feeds.
    """

    bot_id: str
    strategy_name: str
    market_id: str
    mode: Mode
    live: bool
    schedule: Schedule
    risk: RiskCaps
    signal_subscriptions: list[str]


@dataclass
class BotDeps:
    """Runtime dependency container injected at bot construction.

    Separates infrastructure concerns from strategy logic, making the bot
    fully testable by swapping in in-memory fakes.

    Args:
        adapter: Exchange adapter (or paper-mode simulator).
        state: Snapshot read/write repository.
        kill_switch: Read-only kill-switch query interface.
        heartbeat: Heartbeat emitter.
        audit: Append-only audit log writer.
        clock: Injectable clock for deterministic tests.
        rng: Injectable RNG for deterministic tests.
        signals: Signal snapshot provider.  Defaults to
            :class:`~src.signals.base.InMemorySignals` stub.
        secrets_slice: Per-bot slice of the loaded secrets, resolved from
            ``entry.secrets.exchange_credentials`` at subprocess start.
            ``None`` in unit tests that do not load secrets.
    """

    adapter: ExchangeAdapter
    state: StateRepository
    kill_switch: KillSwitchReader
    heartbeat: Heartbeat
    audit: AuditLogger
    clock: Clock = field(default_factory=Clock)
    rng: Random = field(default_factory=random_module.Random)
    signals: Any = None  # InMemorySignals; typed as Any to avoid circular import
    secrets_slice: Any = None  # Mapping[str, Any] | None — per-bot secret slice


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    """Value returned by ``on_tick``.

    A strategy returns zero or more order intents and zero or more cancellation
    requests.  ``BaseBot.run`` processes them in order: cancels first, then
    placements.

    Args:
        intents: Order templates to place.  Use :class:`~src.exchanges.base.OrderTemplate`
            so strategies never fabricate ``client_order_id`` values.
        cancels: ``client_order_id`` values the strategy wants cancelled.
        note: Free-form annotation written to the audit log.  Useful for
            strategy reasoning (e.g. "signal crossed threshold").
    """

    intents: list[OrderTemplate] = field(default_factory=list)
    cancels: list[str] = field(default_factory=list)
    note: str = ""


# ---------------------------------------------------------------------------
# BaseBot
# ---------------------------------------------------------------------------


class BaseBot(ABC):
    """Abstract base for all trading bots.

    Subclasses implement :meth:`on_tick`, :meth:`snapshot`, and
    :meth:`rehydrate`.  They MUST NOT override ``run``, ``place``,
    ``_next_client_order_id``, ``_check_risk_caps``, or ``_persist_snapshot``.

    Class attributes:
        name: Registry key for auto-discovery.  Must be unique across all
            registered strategies.
        schedule: Default tick schedule for this strategy class.

    Args:
        market: The market this bot instance trades.
        config: Static bot configuration.
        deps: Injected runtime dependencies.
    """

    name: ClassVar[str]
    schedule: ClassVar[Schedule]

    def __init__(self, market: Any, config: BotConfig, deps: BotDeps) -> None:
        self._market = market
        self._config = config
        self._deps = deps

        # Monotonic per-bot counter persisted in every snapshot.
        # Seeded from 0; rehydrate() sets it from the snapshot.
        self._intent_seq: int = 0

        # Track position (as notional) and daily loss for risk checks.
        self._position_notional: Decimal = Decimal(0)
        self._daily_loss: Decimal = Decimal(0)

        # Rolling window for order-rate cap: list of placement timestamps.
        self._recent_order_times: list[datetime] = []

        # In-flight dedup cache: client_order_id → OrderResult.
        # Populated by place(); allows idempotent retries without double-sending.
        self._inflight: dict[str, OrderResult] = {}

        # Warn callback — defaults to logger.warning; Phase 6 wires Slack/Discord.
        self._on_warn: Callable[[str], None] = lambda msg: logger.warning(msg)

        # Phase 6a — per-tick metric accumulators (reset after each push).
        self._tick_latency_obs: list[float] = []
        self._tick_overrun_delta: int = 0
        self._order_intent_delta: dict[str, int] = {
            "accepted": 0,
            "rejected": 0,
            "risk_blocked": 0,
            "kill_switch": 0,
        }
        self._fill_latency_obs: list[float] = []

        # Kill-switch cascade: issues cancel_all exactly once per trip period.
        self._ks_cascade = KillSwitchCascade(
            audit=deps.audit,
            bot_id=config.bot_id,
        )

        if deps.signals is None:
            from src.signals.base import InMemorySignals

            deps.signals = InMemorySignals()

    # ------------------------------------------------------------------
    # Abstract interface — strategies implement these three methods only
    # ------------------------------------------------------------------

    @abstractmethod
    async def on_tick(self, signals: SignalSnapshot) -> Decision:
        """Execute one strategy tick.

        Called by ``run()`` after signals and kill-switch checks pass.  The
        implementation must be pure relative to I/O: it may read from
        ``signals`` and internal state, but must not call the adapter directly.
        Orders are expressed as :class:`~src.exchanges.base.OrderTemplate`
        values inside the returned :class:`Decision`.

        Args:
            signals: Fresh signal snapshot for this tick.

        Returns:
            A :class:`Decision` describing the desired actions.
        """

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Serialise in-memory state to a JSONB-safe dict.

        Called by ``_persist_snapshot()`` after every successful tick.
        The returned dict MUST include the keys that
        ``rehydrate`` depends on, including at minimum:
        - ``intent_seq`` (int)
        - ``position`` (str — Decimal serialised as string)
        - ``last_decision_at`` (str — ISO-8601 UTC)

        Returns:
            JSONB-serialisable dict.
        """

    @abstractmethod
    def rehydrate(self, snapshot: dict[str, Any]) -> None:
        """Restore in-memory state from a previously written snapshot.

        Called by the manager after spawning a replacement bot following a
        crash.  Must be idempotent: calling it twice with the same snapshot
        must leave the bot in the same state as calling it once.

        Args:
            snapshot: Dict from the most recent ``bot_state.state`` row,
                as produced by ``snapshot()``.
        """

    # ------------------------------------------------------------------
    # Provided — DO NOT override
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main bot loop.  Ticks on the configured schedule until cancelled.

        Loop steps each tick:
        1. Fetch signals snapshot.
        2. Beat heartbeat.
        3. Check kill switch — if tripped, cancel-all and skip ``on_tick``.
        4. Call ``on_tick``.
        5. Process cancels from the decision.
        6. Process order intents via ``place()``.
        7. Persist snapshot (best-effort — failure warns but does NOT abort).
        8. Sleep until next scheduled tick.
        """
        logger.info(
            "Bot starting: bot_id=%s strategy=%s market=%s mode=%s",
            self._config.bot_id,
            self._config.strategy_name,
            self._config.market_id,
            self._config.mode,
        )

        while True:
            tick_start = self._deps.clock.now()
            # Generate a deterministic tick correlation id.
            tick_id = blake2s(
                f"{self._config.bot_id}:{self._intent_seq}:{tick_start.isoformat()}".encode(),
                digest_size=8,
            ).hexdigest()

            from contextvars import Token

            _tick_token: Token[str | None] | None = None
            try:
                from src.observability.context import tick_id_var

                _tick_token = tick_id_var.set(tick_id)
            except Exception:
                pass

            try:
                # 1. Signals
                signals = await self._deps.signals.snapshot_for(self._config)

                # 2. Heartbeat
                await self._deps.heartbeat.beat()

                # 3. Kill switch
                if await self._deps.kill_switch.is_tripped():
                    logger.warning(
                        "Kill switch tripped — skipping on_tick for bot %s",
                        self._config.bot_id,
                    )
                    await self._ks_cascade.on_trip(self._deps.adapter, self._config.market_id)
                    await self._sleep_until_next_tick(tick_start)
                    continue
                else:
                    # Switch is clear — reset cascade so next trip re-issues cancel_all.
                    self._ks_cascade.reset()

                # 4. Strategy tick — measure latency
                on_tick_start = time.perf_counter()
                decision = await self.on_tick(signals)
                tick_duration = time.perf_counter() - on_tick_start

                # Record latency and check for overrun.
                self._tick_latency_obs.append(tick_duration)
                if tick_duration > self._config.schedule.every_seconds:
                    self._tick_overrun_delta += 1

                # Update Prometheus directly on the manager side (if available).
                try:
                    from src.observability.metrics import BOT_TICK_LATENCY, BOT_TICK_OVERRUN_TOTAL

                    BOT_TICK_LATENCY.labels(
                        bot_id=self._config.bot_id,
                        strategy=self._config.strategy_name,
                    ).observe(tick_duration)
                    if tick_duration > self._config.schedule.every_seconds:
                        BOT_TICK_OVERRUN_TOTAL.labels(
                            bot_id=self._config.bot_id,
                            strategy=self._config.strategy_name,
                        ).inc()
                except Exception:
                    pass  # metrics import optional — bot may run without observability pkg

                # 5. Cancels
                for coid in decision.cancels:
                    try:
                        await self._deps.adapter.cancel(coid)
                    except Exception as exc:
                        logger.warning("Cancel failed for %s: %s", coid, exc)

                # 6. Place intents
                for template in decision.intents:
                    try:
                        await self.place(template)
                    except RiskCapExceeded as exc:
                        logger.warning("Risk cap exceeded: %s", exc)
                    except KillSwitchTripped:
                        logger.warning("Kill switch tripped mid-tick; aborting placements")
                        break
                    except Exception as exc:
                        logger.error("Unexpected error placing order: %s", exc)

                # 7. Snapshot (best-effort — invariant 6)
                await self._persist_snapshot()

                # 8. Push metric deltas via heartbeat (best-effort).
                await self._push_tick_metrics()

            except asyncio.CancelledError:
                logger.info("Bot %s cancelled, exiting run loop.", self._config.bot_id)
                raise
            except Exception as exc:
                logger.error("Unhandled error in bot %s run loop: %s", self._config.bot_id, exc)
            finally:
                if _tick_token is not None:
                    import contextlib

                    with contextlib.suppress(Exception):
                        tick_id_var.reset(_tick_token)

            # 9. Sleep until next tick
            await self._sleep_until_next_tick(tick_start)

    async def place(self, intent_or_template: OrderIntent | OrderTemplate) -> OrderResult:
        """Place an order with risk checks, audit logging, and dedup.

        Accepts either an :class:`~src.exchanges.base.OrderIntent` (full, with
        ``client_order_id``) or an :class:`~src.exchanges.base.OrderTemplate`
        (strategy-facing, without id).  Templates are wrapped into intents using
        the next deterministic ``client_order_id``.

        Steps:
        1. Assign ``client_order_id`` if given a template.
        2. Check kill switch — raises :exc:`KillSwitchTripped` if active.
        3. Check dedup cache — return cached result if id seen before.
        4. Check risk caps — raises :exc:`RiskCapExceeded` if any cap breached.
        5. Write ``order_submitted`` audit entry.
        6. Call adapter.place().
        7. Write ``order_accepted`` or ``order_rejected`` audit entry.
        8. Store result in dedup cache.
        9. Return result.

        Args:
            intent_or_template: The order to place.

        Returns:
            :class:`~src.exchanges.base.OrderResult` from the adapter.

        Raises:
            KillSwitchTripped: If the kill switch is active.
            RiskCapExceeded: If any risk cap would be breached.
        """
        # 1. Resolve intent
        if isinstance(intent_or_template, OrderTemplate):
            intent = OrderIntent(
                client_order_id=self._next_client_order_id(),
                market=intent_or_template.market,
                side=intent_or_template.side,
                price=intent_or_template.price,
                size=intent_or_template.size,
            )
        else:
            intent = intent_or_template

        # 2. Kill switch (fast path — checked again before every order)
        if await self._deps.kill_switch.is_tripped():
            self._order_intent_delta["kill_switch"] += 1
            try:
                from src.observability.metrics import ORDER_INTENT_TOTAL

                ORDER_INTENT_TOTAL.labels(
                    bot_id=self._config.bot_id,
                    strategy=self._config.strategy_name,
                    result="kill_switch",
                ).inc()
            except Exception:
                pass
            raise KillSwitchTripped("Kill switch is active; order blocked")

        # 3. Dedup — same client_order_id on retry returns cached result
        if intent.client_order_id in self._inflight:
            logger.debug(
                "Dedup hit for client_order_id=%s; returning cached result",
                intent.client_order_id,
            )
            return self._inflight[intent.client_order_id]

        # 4. Risk caps
        try:
            await self._check_risk_caps(intent)
        except RiskCapExceeded:
            self._order_intent_delta["risk_blocked"] += 1
            try:
                from src.observability.metrics import ORDER_INTENT_TOTAL

                ORDER_INTENT_TOTAL.labels(
                    bot_id=self._config.bot_id,
                    strategy=self._config.strategy_name,
                    result="risk_blocked",
                ).inc()
            except Exception:
                pass
            raise

        # Track recent order times for rate cap
        now = self._deps.clock.now()
        self._recent_order_times.append(now)

        # 5. Audit: submitted
        await self._deps.audit.write(
            bot_id=self._config.bot_id,
            kind="order_submitted",
            payload={
                "market_id": intent.market.market_id,
                "side": intent.side,
                "price": str(intent.price),
                "size": str(intent.size),
                "note": "",
            },
            client_order_id=intent.client_order_id,
        )

        # 6. Adapter call
        result = await self._deps.adapter.place(intent)

        # 7. Audit: result
        await self._deps.audit.write(
            bot_id=self._config.bot_id,
            kind="order_accepted" if result.accepted else "order_rejected",
            payload={
                "accepted": result.accepted,
                "reason": result.reason,
                "raw": dict(result.raw),
            },
            client_order_id=intent.client_order_id,
            exchange_order_id=result.exchange_order_id,
        )

        # 8. Cache result for idempotent retries
        self._inflight[intent.client_order_id] = result

        # Track intent outcome for metrics.
        result_label = "accepted" if result.accepted else "rejected"
        self._order_intent_delta[result_label] += 1
        try:
            from src.observability.metrics import ORDER_INTENT_TOTAL

            ORDER_INTENT_TOTAL.labels(
                bot_id=self._config.bot_id,
                strategy=self._config.strategy_name,
                result=result_label,
            ).inc()
        except Exception:
            pass

        return result

    # ------------------------------------------------------------------
    # Internal helpers — strategies must not call these directly
    # ------------------------------------------------------------------

    def _next_client_order_id(self) -> str:
        """Generate the next deterministic ``client_order_id``.

        Determinism is guaranteed by blake2s over ``"{bot_id}:{intent_seq}"``.
        The ``intent_seq`` counter is persisted in every snapshot so retries
        after a crash replay the same id sequence (invariant 2).

        Returns:
            8-byte hex string (16 characters).
        """
        self._intent_seq += 1
        raw = f"{self._config.bot_id}:{self._intent_seq}".encode()
        return blake2s(raw, digest_size=8).hexdigest()

    async def _check_risk_caps(self, intent: OrderIntent) -> None:
        """Validate an intent against configured risk caps.

        Delegates to :func:`~src.risk.caps.check_caps`.  Prunes stale entries
        from ``_recent_order_times`` before delegating so the rolling window is
        always current.

        Args:
            intent: The order intent to validate.

        Raises:
            RiskCapExceeded: If any cap is breached.  The intent must NOT be
                forwarded to the adapter.
        """
        now = self._deps.clock.now()
        # Prune stale entries from the rolling window before checking.
        window_start = now.timestamp() - 60.0
        self._recent_order_times = [
            t for t in self._recent_order_times if t.timestamp() >= window_start
        ]
        check_caps(
            intent.price,
            intent.size,
            position_notional=self._position_notional,
            daily_loss=self._daily_loss,
            recent_order_times=self._recent_order_times,
            caps=self._config.risk,
            now=now,
        )

    async def _persist_snapshot(self) -> None:
        """Write a snapshot to the state repository.

        Best-effort per invariant 6: failures emit a warn and do NOT propagate.
        The tick continues normally after a snapshot failure.
        """
        try:
            snap = self.snapshot()
            snap.setdefault("intent_seq", self._intent_seq)
            snap.setdefault("position", str(self._position_notional))
            snap.setdefault("last_decision_at", self._deps.clock.now().isoformat())

            # Read current version from snap or default
            version = int(snap.get("_version", 0)) + 1
            snap["_version"] = version

            await self._deps.state.write_snapshot(
                bot_id=self._config.bot_id,
                market_id=self._config.market_id,
                version=version,
                state=snap,
            )
            # After a successful snapshot, reset the lag gauge to 0.
            try:
                from src.observability.metrics import BOT_SNAPSHOT_LAG_SECONDS

                BOT_SNAPSHOT_LAG_SECONDS.labels(bot_id=self._config.bot_id).set(0.0)
            except Exception:
                pass
        except Exception as exc:
            self._on_warn(f"Snapshot write failed for bot {self._config.bot_id}: {exc}")

    async def _push_tick_metrics(self) -> None:
        """Push accumulated metric deltas to the manager via the heartbeat client.

        Best-effort: failures are logged at debug level and do not abort the tick.
        Resets per-tick accumulators after a successful push.
        """
        from src.manager.heartbeat import HeartbeatClient, SocketHeartbeat

        # Resolve the underlying HeartbeatClient if the heartbeat is a SocketHeartbeat.
        hb = self._deps.heartbeat
        client: HeartbeatClient | None = None
        if isinstance(hb, SocketHeartbeat):
            client = hb.client

        if client is None:
            # Not a socket heartbeat (unit tests use LocalHeartbeat) — skip push.
            return

        payload: dict[str, object] = {
            "strategy": self._config.strategy_name,
            "tick_latency_seconds_obs": list(self._tick_latency_obs),
            "tick_overrun_total": self._tick_overrun_delta,
            "order_intent_total": dict(self._order_intent_delta),
            "order_fill_latency_seconds_obs": list(self._fill_latency_obs),
            "position_notional_usd": float(self._position_notional),
            "pnl_realized_usd": 0.0,
            "pnl_unrealized_usd": 0.0,
        }

        try:
            await client.push_metrics(payload)
            # Reset accumulators after successful push.
            self._tick_latency_obs.clear()
            self._tick_overrun_delta = 0
            self._order_intent_delta = {
                "accepted": 0,
                "rejected": 0,
                "risk_blocked": 0,
                "kill_switch": 0,
            }
            self._fill_latency_obs.clear()
        except Exception as exc:
            logger.debug("_push_tick_metrics failed (non-fatal): %s", exc)

    async def _sleep_until_next_tick(self, tick_start: datetime) -> None:
        """Sleep until the next scheduled tick boundary.

        Args:
            tick_start: The datetime when the current tick began.
        """
        next_tick = self._config.schedule.next_tick(tick_start)
        now = self._deps.clock.now()
        sleep_secs = (next_tick - now).total_seconds()
        if sleep_secs > 0:
            await asyncio.sleep(sleep_secs)
