"""NullStrategy — a no-op BaseBot subclass for tests.

Returns an empty :class:`~src.bots.base.Decision` every tick.  Exposes a
``tick_count`` counter so tests can assert on how many ticks were processed.
"""

from __future__ import annotations

__all__ = ["NullStrategy", "make_null_bot"]

from decimal import Decimal
from typing import Any

from src.bots.base import BaseBot, BotConfig, BotDeps, Decision, RiskCaps, Schedule
from src.exchanges.base import Market, Mode
from src.signals.base import SignalSnapshot


class NullStrategy(BaseBot):
    """No-op strategy that returns an empty :class:`~src.bots.base.Decision` each tick.

    Useful as a test harness for the :class:`~src.bots.base.BaseBot` lifecycle
    without any order activity.

    Attributes:
        tick_count: Number of completed ``on_tick`` calls.
    """

    name: str = "null_strategy"
    schedule: Schedule = Schedule(every_seconds=1)

    def __init__(self, market: Market, config: BotConfig, deps: BotDeps) -> None:
        super().__init__(market, config, deps)
        self.tick_count: int = 0

    async def on_tick(self, signals: SignalSnapshot) -> Decision:
        """Increment ``tick_count`` and return an empty decision.

        Args:
            signals: Current signal snapshot (ignored).

        Returns:
            Empty :class:`~src.bots.base.Decision`.
        """
        self.tick_count += 1
        return Decision()

    def snapshot(self) -> dict[str, Any]:
        """Serialise the minimal required state plus ``tick_count``.

        Returns:
            Dict with ``intent_seq``, ``position``, ``last_decision_at``,
            and ``tick_count``.
        """
        return {
            "intent_seq": self._intent_seq,
            "position": str(self._position_notional),
            "last_decision_at": self._deps.clock.now().isoformat(),
            "tick_count": self.tick_count,
        }

    def rehydrate(self, snapshot: dict[str, Any]) -> None:
        """Restore state from a snapshot.

        Args:
            snapshot: Dict produced by :meth:`snapshot`.
        """
        self._intent_seq = int(snapshot.get("intent_seq", 0))
        self._position_notional = Decimal(str(snapshot.get("position", "0")))
        self.tick_count = int(snapshot.get("tick_count", 0))


def make_null_bot(
    *,
    bot_id: str = "test-bot-001",
    market_id: str = "test-market-001",
    mode: Mode = Mode.PAPER,
    max_position_notional: Decimal = Decimal("10000"),
    max_daily_loss: Decimal = Decimal("1000"),
    max_orders_per_minute: int = 60,
    adapter: Any = None,
    state: Any = None,
    kill_switch: Any = None,
    heartbeat: Any = None,
    audit: Any = None,
    clock: Any = None,
) -> NullStrategy:
    """Factory that wires up a :class:`NullStrategy` with sensible test defaults.

    All infrastructure arguments default to in-memory fakes, making it trivial
    to spin up a bot in a test without boilerplate.

    Args:
        bot_id: Stable bot identifier.
        market_id: Market the bot is assigned to.
        mode: Execution mode.
        max_position_notional: Position cap.
        max_daily_loss: Daily loss cap.
        max_orders_per_minute: Rate cap.
        adapter: Override the :class:`~tests.fixtures.echo_exchange.EchoExchange`.
        state: Override the :class:`~tests.fixtures.state.InMemoryStateRepository`.
        kill_switch: Override the :class:`~tests.fixtures.state.InMemoryKillSwitch`.
        heartbeat: Override the :class:`~src.bots.base.LocalHeartbeat`.
        audit: Override the :class:`~tests.fixtures.state.InMemoryAuditLogger`.
        clock: Override the :class:`~tests.fixtures.clocks.ManualClock`.

    Returns:
        A fully-wired :class:`NullStrategy` instance.
    """
    from src.bots.base import LocalHeartbeat

    from tests.fixtures.clocks import ManualClock
    from tests.fixtures.echo_exchange import EchoExchange
    from tests.fixtures.state import (
        InMemoryAuditLogger,
        InMemoryKillSwitch,
        InMemoryStateRepository,
    )

    if adapter is None:
        adapter = EchoExchange()
    if state is None:
        state = InMemoryStateRepository()
    if kill_switch is None:
        kill_switch = InMemoryKillSwitch()
    if heartbeat is None:
        heartbeat = LocalHeartbeat()
    if audit is None:
        audit = InMemoryAuditLogger()
    if clock is None:
        clock = ManualClock()

    market = Market(
        market_id=market_id,
        token_id="token-0",
        tick_size=Decimal("0.01"),
        min_size=Decimal("1"),
        venue="echo",
    )
    config = BotConfig(
        bot_id=bot_id,
        strategy_name="null_strategy",
        market_id=market_id,
        mode=mode,
        live=False,
        schedule=Schedule(every_seconds=1),
        risk=RiskCaps(
            max_position_notional=max_position_notional,
            max_daily_loss=max_daily_loss,
            max_orders_per_minute=max_orders_per_minute,
        ),
        signal_subscriptions=[],
    )
    deps = BotDeps(
        adapter=adapter,
        state=state,
        kill_switch=kill_switch,
        heartbeat=heartbeat,
        audit=audit,
        clock=clock,
    )
    return NullStrategy(market=market, config=config, deps=deps)
