"""Tests for BaseBot's optional provisional-winning-rule integration.

Uses only the repo's established fakes (never mocks), per
``.claude/rules/07-testing.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

from src.bots.base import BaseBot, BotConfig, BotDeps, Decision, LocalHeartbeat, RiskCaps, Schedule
from src.exchanges.base import Market, Mode
from src.risk.audit import KIND_PROVISIONAL_RULING
from src.rules.base import PositionState, ProvisionalRuling, WinningRule, WinningRuleContext
from src.signals.base import SignalSnapshot

from tests.fixtures.clocks import ManualClock
from tests.fixtures.echo_exchange import EchoExchange
from tests.fixtures.state import InMemoryAuditLogger, InMemoryKillSwitch, InMemoryStateRepository

_MARKET_ID = "test-market-001"
_BOT_ID = "test-bot-001"


class _AlwaysWonRule(WinningRule):
    """Deterministic test rule — always returns WON."""

    name: ClassVar[str] = "test.fixture.always_won"

    def evaluate(self, ctx: WinningRuleContext) -> ProvisionalRuling:
        return ProvisionalRuling.WON


class _SequenceRule(WinningRule):
    """Deterministic test rule that returns rulings from a fixed queue.

    Once exhausted, keeps returning the last value.
    """

    name: ClassVar[str] = "test.fixture.sequence"

    def __init__(self, sequence: list[ProvisionalRuling]) -> None:
        self._sequence = list(sequence)
        self._calls = 0

    def evaluate(self, ctx: WinningRuleContext) -> ProvisionalRuling:
        ruling = self._sequence[min(self._calls, len(self._sequence) - 1)]
        self._calls += 1
        return ruling


class _RaisingRule(WinningRule):
    """Deterministic test rule that always raises inside evaluate()."""

    name: ClassVar[str] = "test.fixture.raising"

    def evaluate(self, ctx: WinningRuleContext) -> ProvisionalRuling:
        raise RuntimeError("boom")


class _PositionAwareStrategy(BaseBot):
    """No-op strategy that optionally exposes a fake current_position()."""

    name: str = "provisional_ruling_test_strategy"
    schedule: Schedule = Schedule(every_seconds=1)

    def __init__(
        self,
        market: Market,
        config: BotConfig,
        deps: BotDeps,
        *,
        position: PositionState | None = None,
        expose_position: bool = True,
    ) -> None:
        super().__init__(market, config, deps)
        self._fake_position = position
        self._expose_position = expose_position

    async def on_tick(self, signals: SignalSnapshot) -> Decision:
        return Decision()

    def current_position(self) -> PositionState | None:
        if not self._expose_position:
            return super().current_position()
        return self._fake_position

    def snapshot(self) -> dict[str, Any]:
        return {
            "intent_seq": self._intent_seq,
            "position": str(self._position_notional),
            "last_decision_at": self._deps.clock.now().isoformat(),
        }

    def rehydrate(self, snapshot: dict[str, Any]) -> None:
        self._intent_seq = int(snapshot.get("intent_seq", 0))
        self._position_notional = Decimal(str(snapshot.get("position", "0")))


def _empty_signals(clock: ManualClock) -> SignalSnapshot:
    return SignalSnapshot(samples={}, received_at=clock.now(), stale=frozenset())


def _position() -> PositionState:
    return PositionState(
        market_id=_MARKET_ID,
        side="UP",
        entry_reference=Decimal("100000"),
        entry_at=datetime(2026, 1, 1, tzinfo=UTC),
        size=Decimal("10"),
    )


def _build_bot(
    *,
    winning_rule: WinningRule | None = None,
    winning_rule_params: dict[str, Any] | None = None,
    position: PositionState | None = None,
    expose_position: bool = True,
) -> tuple[_PositionAwareStrategy, InMemoryAuditLogger, ManualClock]:
    """Build a fully-wired _PositionAwareStrategy with in-memory fakes."""
    market = Market(
        market_id=_MARKET_ID,
        token_id="token-0",
        tick_size=Decimal("0.01"),
        min_size=Decimal("1"),
        venue="echo",
    )
    config = BotConfig(
        bot_id=_BOT_ID,
        strategy_name="provisional_ruling_test_strategy",
        market_id=_MARKET_ID,
        mode=Mode.PAPER,
        live=False,
        schedule=Schedule(every_seconds=1),
        risk=RiskCaps(
            max_position_notional=Decimal("10000"),
            max_daily_loss=Decimal("1000"),
            max_orders_per_minute=60,
        ),
        signal_subscriptions=[],
        winning_rule=winning_rule,
        winning_rule_params=winning_rule_params or {},
    )
    clock = ManualClock()
    audit = InMemoryAuditLogger()
    deps = BotDeps(
        adapter=EchoExchange(),
        state=InMemoryStateRepository(),
        kill_switch=InMemoryKillSwitch(),
        heartbeat=LocalHeartbeat(),
        audit=audit,
        clock=clock,
    )
    bot = _PositionAwareStrategy(
        market=market,
        config=config,
        deps=deps,
        position=position,
        expose_position=expose_position,
    )
    return bot, audit, clock


def _ruling_audit_rows(audit: InMemoryAuditLogger) -> list[dict[str, Any]]:
    return [r for r in audit.rows if r["kind"] == KIND_PROVISIONAL_RULING]


async def test_no_winning_rule_configured_ruling_always_none() -> None:
    """No winning_rule configured -> provisional_ruling() always None, no new audit rows."""
    bot, audit, clock = _build_bot(winning_rule=None, position=_position())
    assert bot.provisional_ruling() is None

    await bot._evaluate_winning_rule(_empty_signals(clock))

    assert bot.provisional_ruling() is None
    assert _ruling_audit_rows(audit) == []


async def test_winning_rule_configured_but_no_position_override_ruling_never_raises() -> None:
    """Rule configured, current_position() not overridden (default None) -> always None."""
    bot, audit, clock = _build_bot(
        winning_rule=_AlwaysWonRule(), position=_position(), expose_position=False
    )
    assert bot.current_position() is None

    await bot._evaluate_winning_rule(_empty_signals(clock))

    assert bot.provisional_ruling() is None
    assert _ruling_audit_rows(audit) == []


async def test_ruling_computed_and_audited_only_on_change() -> None:
    """A rule + position combo computes a ruling; audit row written only when it changes."""
    rule = _SequenceRule(
        [
            ProvisionalRuling.WON,
            ProvisionalRuling.WON,  # unchanged — no new audit row
            ProvisionalRuling.LOST,  # changed — new audit row
        ]
    )
    bot, audit, clock = _build_bot(winning_rule=rule, position=_position())

    for _ in range(3):
        await bot._evaluate_winning_rule(_empty_signals(clock))

    rows = _ruling_audit_rows(audit)
    assert len(rows) == 2  # WON (first time) + LOST (change) — not the repeated WON
    assert rows[0]["payload"]["ruling"] == ProvisionalRuling.WON.value
    assert rows[1]["payload"]["ruling"] == ProvisionalRuling.LOST.value
    assert bot.provisional_ruling() == ProvisionalRuling.LOST


async def test_audit_payload_carries_expected_fields() -> None:
    """Audit payload includes rule name, ruling, side, entry_reference, market_id."""
    bot, audit, clock = _build_bot(winning_rule=_AlwaysWonRule(), position=_position())

    await bot._evaluate_winning_rule(_empty_signals(clock))

    row = _ruling_audit_rows(audit)[0]
    payload = row["payload"]
    assert payload["rule_name"] == "test.fixture.always_won"
    assert payload["ruling"] == ProvisionalRuling.WON.value
    assert payload["position_side"] == "UP"
    assert payload["entry_reference"] == "100000"
    assert payload["market_id"] == _MARKET_ID


async def test_rule_that_raises_does_not_propagate_and_degrades_to_none() -> None:
    """A WinningRule.evaluate() that raises never propagates; ruling degrades to None."""
    bot, audit, clock = _build_bot(winning_rule=_RaisingRule(), position=_position())

    await bot._evaluate_winning_rule(_empty_signals(clock))  # must not raise

    assert bot.provisional_ruling() is None
    assert _ruling_audit_rows(audit) == []


async def test_run_wires_winning_rule_evaluation_into_the_tick_loop() -> None:
    """A single real run() tick evaluates the rule and writes the expected audit row."""
    import asyncio
    import contextlib

    bot, audit, _clock = _build_bot(winning_rule=_AlwaysWonRule(), position=_position())

    task = asyncio.create_task(bot.run())
    # Let the event loop advance through the synchronous portion of the first
    # tick; run() suspends on a real asyncio.sleep() only at the very end of
    # the tick (step: sleep until next tick), so a couple of loop yields is
    # enough for the winning-rule evaluation (and its audit write) to land.
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert bot.provisional_ruling() == ProvisionalRuling.WON
    assert len(_ruling_audit_rows(audit)) == 1
