"""Unit tests for dip_snipe_3round — the manager-supervised, round-capped
dip-trigger strategy for recurring BTC Up/Down windows.

All external I/O (market resolution, CLOB book, CLOB settlement) is
monkeypatched at the module level — no live network per
``.claude/rules/07-testing.md``. Order placement goes through the real
``BaseBot.place()`` against ``EchoExchange`` so ``_inflight``/audit/risk-cap
plumbing is exercised the same way the manager would drive it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from src.bots.base import BotConfig, BotDeps, LocalHeartbeat, RiskCaps, Schedule
from src.bots.dip_snipe.strategy import DipSnipe3Round
from src.exchanges.base import Market, Mode
from src.exchanges.market_resolver import ResolvedMarket

from tests.fixtures.clocks import ManualClock
from tests.fixtures.echo_exchange import EchoExchange
from tests.fixtures.state import InMemoryAuditLogger, InMemoryKillSwitch, InMemoryStateRepository


def _make_bot(*, adapter: EchoExchange | None = None) -> DipSnipe3Round:
    market = Market(
        market_id="",
        token_id="",
        tick_size=Decimal("0.01"),
        min_size=Decimal("5"),
        venue="polymarket",
    )
    config = BotConfig(
        bot_id="dipsnipe-test-1",
        strategy_name="dip_snipe_3round",
        market_id="",
        mode=Mode.PAPER,
        live=False,
        schedule=Schedule(every_seconds=5),
        risk=RiskCaps(
            max_position_notional=Decimal("100"),
            max_daily_loss=Decimal("50"),
            max_orders_per_minute=60,
        ),
        signal_subscriptions=[],
        strategy_params={
            "slug": "btc-updown-5m-test",
            "outcome": "UP",
            "exchange": "polymarket",
            "trigger": "0.30",
            "size": "5",
            "max_rounds": 3,
            "min_window_secs": 120,
        },
    )
    deps = BotDeps(
        adapter=adapter or EchoExchange(),
        state=InMemoryStateRepository(),
        kill_switch=InMemoryKillSwitch(),
        heartbeat=LocalHeartbeat(),
        audit=InMemoryAuditLogger(),
        clock=ManualClock(),
    )
    return DipSnipe3Round(market=market, config=config, deps=deps)


def _resolved(window_end: float) -> ResolvedMarket:
    return ResolvedMarket(
        condition_id="0xcond",
        slug="btc-updown-5m-test-123",
        tokens={"Up": "up-token", "Down": "dn-token"},
        window_end=window_end,
        tick_size=Decimal("0.01"),
        min_size=Decimal("5"),
    )


async def _run_round_to_open(
    bot: DipSnipe3Round, monkeypatch: pytest.MonkeyPatch, *, up_ask: Decimal, dn_ask: Decimal
) -> None:
    """Drive resolving -> watching -> trigger -> accepted fill -> awaiting_settlement."""
    import time

    import src.bots.dip_snipe.strategy as mod

    monkeypatch.setattr(mod, "resolve_all_outcomes", lambda ref: _resolved(time.time() + 300))
    d = await bot.on_tick(signals=None)  # type: ignore[arg-type]
    assert bot._phase == "watching"
    assert "market_resolved" in d.note

    async def fake_best_ask(client: object, token_id: str) -> Decimal:
        return up_ask if token_id == "up-token" else dn_ask

    monkeypatch.setattr(mod, "_best_ask", fake_best_ask)
    d = await bot.on_tick(signals=None)  # type: ignore[arg-type]
    assert bot._phase == "awaiting_fill"
    assert len(d.intents) == 1

    result = await bot.place(d.intents[0])
    assert result.accepted

    d = bot._tick_awaiting_fill()
    assert bot._phase == "awaiting_settlement"
    assert d.note == "order_accepted_holding"


@pytest.mark.asyncio
async def test_win_stops_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.bots.dip_snipe.strategy as mod

    bot = _make_bot()
    await _run_round_to_open(bot, monkeypatch, up_ask=Decimal("0.25"), dn_ask=Decimal("0.90"))
    assert bot._rounds_played == 1

    async def fake_settlement(client: object, condition_id: str, token_id: str) -> Decimal:
        return Decimal("1.0")

    monkeypatch.setattr(mod, "_check_clob_settlement", fake_settlement)
    d = await bot._tick_awaiting_settlement()

    assert bot._finished is True
    assert bot._won is True
    assert d.note == "round_won_stopping"
    assert bot._position_notional == Decimal("0")

    # Finished bots stay inert forever — no further intents, no phase change.
    d2 = await bot.on_tick(signals=None)  # type: ignore[arg-type]
    assert d2.intents == []
    assert d2.note == "finished_won"


@pytest.mark.asyncio
async def test_loss_continues_to_next_round(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.bots.dip_snipe.strategy as mod

    bot = _make_bot()
    await _run_round_to_open(bot, monkeypatch, up_ask=Decimal("0.25"), dn_ask=Decimal("0.90"))
    assert bot._rounds_played == 1

    async def fake_settlement(client: object, condition_id: str, token_id: str) -> Decimal:
        return Decimal("0.0")

    monkeypatch.setattr(mod, "_check_clob_settlement", fake_settlement)
    d = await bot._tick_awaiting_settlement()

    assert bot._finished is False
    assert bot._phase == "resolving"
    assert d.note == "round_lost_continuing"
    assert bot._condition_id is None  # round-scoped fields reset


@pytest.mark.asyncio
async def test_third_loss_stops_at_max_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.bots.dip_snipe.strategy as mod

    bot = _make_bot()

    async def fake_settlement(client: object, condition_id: str, token_id: str) -> Decimal:
        return Decimal("0.0")

    monkeypatch.setattr(mod, "_check_clob_settlement", fake_settlement)

    for expected_round in (1, 2, 3):
        await _run_round_to_open(bot, monkeypatch, up_ask=Decimal("0.25"), dn_ask=Decimal("0.90"))
        assert bot._rounds_played == expected_round
        d = await bot._tick_awaiting_settlement()

    assert bot._finished is True
    assert bot._won is False
    assert d.note == "round_lost_max_rounds_stopping"


@pytest.mark.asyncio
async def test_rejected_order_does_not_consume_a_round(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    import src.bots.dip_snipe.strategy as mod

    bot = _make_bot(adapter=EchoExchange(reject=True))
    monkeypatch.setattr(mod, "resolve_all_outcomes", lambda ref: _resolved(time.time() + 300))
    await bot.on_tick(signals=None)  # type: ignore[arg-type]

    async def fake_best_ask(client: object, token_id: str) -> Decimal:
        return Decimal("0.25") if token_id == "up-token" else Decimal("0.90")

    monkeypatch.setattr(mod, "_best_ask", fake_best_ask)
    d = await bot.on_tick(signals=None)  # type: ignore[arg-type]
    assert bot._phase == "awaiting_fill"

    result = await bot.place(d.intents[0])
    assert not result.accepted

    d2 = bot._tick_awaiting_fill()
    assert bot._rounds_played == 0
    assert bot._phase in ("watching", "resolving")
    assert d2.note.startswith("order_rejected")


@pytest.mark.asyncio
async def test_window_closes_with_no_fill_does_not_consume_a_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    import src.bots.dip_snipe.strategy as mod

    bot = _make_bot()
    # Window closes in 10s — less than min_window_secs (120) remaining.
    monkeypatch.setattr(mod, "resolve_all_outcomes", lambda ref: _resolved(time.time() + 10))
    d = await bot.on_tick(signals=None)  # type: ignore[arg-type]
    # secs_left (~10) < min_window_secs (120) -> stays in resolving.
    assert bot._phase == "resolving"
    assert d.note.startswith("waiting_next_window")


def test_awaiting_fill_times_out_when_never_placed() -> None:
    """A RiskCapExceeded/KillSwitchTripped-blocked order never reaches
    _inflight — awaiting_fill must give up after the timeout instead of
    stalling forever waiting on an id that will never arrive."""
    import time

    bot = _make_bot()
    bot._phase = "awaiting_fill"
    bot._pending_coid = "never-arrives"
    bot._fired_token = "up-token"
    bot._fired_outcome = "Up"
    bot._entry_price = Decimal("0.25")
    bot._awaiting_fill_since = time.time() - (DipSnipe3Round.AWAITING_FILL_TIMEOUT_SECS + 1)

    d = bot._tick_awaiting_fill()

    assert d.note == "awaiting_fill_timed_out"
    assert bot._rounds_played == 0
    assert bot._pending_coid is None
    assert bot._phase in ("watching", "resolving")


def test_awaiting_fill_gives_up_immediately_when_since_missing() -> None:
    """A snapshot rehydrated into awaiting_fill with no awaiting_fill_since
    (e.g. written by a process that predates this one — _inflight never
    survives a restart) must give up on the first tick, not restart the
    timeout window every tick forever."""
    bot = _make_bot()
    bot._phase = "awaiting_fill"
    bot._pending_coid = "orphaned-from-restart"
    bot._fired_token = "up-token"
    bot._fired_outcome = "Up"
    bot._entry_price = Decimal("0.25")
    bot._awaiting_fill_since = None

    d = bot._tick_awaiting_fill()

    assert d.note == "awaiting_fill_timed_out"
    assert bot._rounds_played == 0
    assert bot._pending_coid is None
    assert bot._phase in ("watching", "resolving")


def test_rehydrate_is_idempotent() -> None:
    bot = _make_bot()
    bot._phase = "awaiting_settlement"
    bot._rounds_played = 2
    bot._condition_id = "0xabc"
    bot._fired_token = "up-token"
    bot._fired_outcome = "Up"
    bot._entry_price = Decimal("0.27")
    bot._pending_coid = "deadbeefcafef00d"
    bot._position_notional = Decimal("1.35")

    snap = bot.snapshot()

    fresh = _make_bot()
    fresh.rehydrate(snap)
    fresh.rehydrate(snap)  # idempotency check

    assert fresh._phase == "awaiting_settlement"
    assert fresh._rounds_played == 2
    assert fresh._condition_id == "0xabc"
    assert fresh._fired_token == "up-token"
    assert fresh._entry_price == Decimal("0.27")
    assert fresh._pending_coid == "deadbeefcafef00d"
    assert fresh._position_notional == Decimal("1.35")


@pytest.mark.asyncio
async def test_finished_bot_never_places_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _make_bot()
    bot._finished = True
    bot._won = True
    for _ in range(5):
        d = await bot.on_tick(signals=None)  # type: ignore[arg-type]
        assert d.intents == []
