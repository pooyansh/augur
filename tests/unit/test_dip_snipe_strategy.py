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

import httpx
import pytest
import respx
from src.bots.base import BotConfig, BotDeps, LocalHeartbeat, RiskCaps, Schedule
from src.bots.dip_snipe.strategy import DipSnipe3Round, _check_clob_settlement
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


# ---------------------------------------------------------------------------
# Fast-rule continuation (opt-in; .claude/rules/10-winning-rules.md)
#
# Explicit, accepted tradeoff: advance past a round on FAST_LOSS_STREAK
# consecutive provisional LOST readings, without waiting for authoritative
# settlement — the ruling can flip-flop, so a wrong fast read must be
# corrected once the real result is known rather than compounding silently.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_rule_does_not_advance_before_streak_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.bots.dip_snipe.strategy as mod
    from src.rules.base import ProvisionalRuling

    bot = _make_bot()

    async def never_settled(client: object, condition_id: str, token_id: str) -> Decimal | None:
        return None

    monkeypatch.setattr(mod, "_check_clob_settlement", never_settled)
    await _run_round_to_open(bot, monkeypatch, up_ask=Decimal("0.25"), dn_ask=Decimal("0.90"))

    bot._provisional_ruling_cache = ProvisionalRuling.LOST
    for _ in range(DipSnipe3Round.FAST_LOSS_STREAK - 1):
        d = await bot._tick_awaiting_settlement()
        assert d.note == "awaiting_settlement"
        assert bot._phase == "awaiting_settlement"

    assert bot._pending_validations == []


@pytest.mark.asyncio
async def test_fast_rule_advances_after_loss_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.bots.dip_snipe.strategy as mod
    from src.rules.base import ProvisionalRuling

    bot = _make_bot()

    async def never_settled(client: object, condition_id: str, token_id: str) -> Decimal | None:
        return None

    monkeypatch.setattr(mod, "_check_clob_settlement", never_settled)
    await _run_round_to_open(bot, monkeypatch, up_ask=Decimal("0.25"), dn_ask=Decimal("0.90"))
    condition_id = bot._condition_id

    bot._provisional_ruling_cache = ProvisionalRuling.LOST
    d = None
    for _ in range(DipSnipe3Round.FAST_LOSS_STREAK):
        d = await bot._tick_awaiting_settlement()

    assert d is not None
    assert d.note == "round_lost_continuing_fast"
    assert bot._finished is False
    assert bot._phase == "resolving"
    assert len(bot._pending_validations) == 1
    assert bot._pending_validations[0]["condition_id"] == condition_id
    assert bot._pending_validations[0]["round_index"] == 1


@pytest.mark.asyncio
async def test_fast_rule_correction_stops_on_actual_win(monkeypatch: pytest.MonkeyPatch) -> None:
    """A round fast-advanced as a loss that authoritative settlement later
    shows was a WIN must stop the strategy — honoring "stop on win" even
    though it already moved on, rather than silently placing more rounds."""
    import src.bots.dip_snipe.strategy as mod
    from src.rules.base import ProvisionalRuling

    bot = _make_bot()

    async def never_settled(client: object, condition_id: str, token_id: str) -> Decimal | None:
        return None

    monkeypatch.setattr(mod, "_check_clob_settlement", never_settled)
    await _run_round_to_open(bot, monkeypatch, up_ask=Decimal("0.25"), dn_ask=Decimal("0.90"))

    bot._provisional_ruling_cache = ProvisionalRuling.LOST
    for _ in range(DipSnipe3Round.FAST_LOSS_STREAK):
        await bot._tick_awaiting_settlement()

    assert bot._finished is False
    assert len(bot._pending_validations) == 1

    async def actually_won(client: object, condition_id: str, token_id: str) -> Decimal | None:
        return Decimal("1.0")

    monkeypatch.setattr(mod, "_check_clob_settlement", actually_won)
    d = await bot._check_pending_validations()

    assert d is not None
    assert d.note == "fast_rule_correction_actually_won_stopping"
    assert bot._finished is True
    assert bot._won is True
    assert bot._pending_validations == []


@pytest.mark.asyncio
async def test_fast_rule_correction_confirms_actual_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.bots.dip_snipe.strategy as mod
    from src.rules.base import ProvisionalRuling

    bot = _make_bot()

    async def never_settled(client: object, condition_id: str, token_id: str) -> Decimal | None:
        return None

    monkeypatch.setattr(mod, "_check_clob_settlement", never_settled)
    await _run_round_to_open(bot, monkeypatch, up_ask=Decimal("0.25"), dn_ask=Decimal("0.90"))

    bot._provisional_ruling_cache = ProvisionalRuling.LOST
    for _ in range(DipSnipe3Round.FAST_LOSS_STREAK):
        await bot._tick_awaiting_settlement()

    assert len(bot._pending_validations) == 1

    async def actually_lost(client: object, condition_id: str, token_id: str) -> Decimal | None:
        return Decimal("0.0")

    monkeypatch.setattr(mod, "_check_clob_settlement", actually_lost)
    d = await bot._check_pending_validations()

    assert d is None
    assert bot._finished is False
    assert bot._pending_validations == []


@pytest.mark.asyncio
async def test_current_position_none_outside_awaiting_settlement() -> None:
    bot = _make_bot()
    assert bot._phase == "resolving"
    assert bot.current_position() is None


@pytest.mark.asyncio
async def test_current_position_reports_when_awaiting_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    import src.bots.dip_snipe.strategy as mod

    bot = _make_bot()

    async def never_settled(client: object, condition_id: str, token_id: str) -> Decimal | None:
        return None

    monkeypatch.setattr(mod, "_check_clob_settlement", never_settled)
    await _run_round_to_open(bot, monkeypatch, up_ask=Decimal("0.25"), dn_ask=Decimal("0.90"))

    # _run_round_to_open drives ticks with signals=None, so entry_btc_price
    # is never captured — set it directly to exercise current_position().
    bot._entry_btc_price = Decimal("65000")
    bot._entry_at = datetime.now(tz=UTC)

    pos = bot.current_position()
    assert pos is not None
    assert pos.side == "Up"
    assert pos.entry_reference == Decimal("65000")


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


# ---------------------------------------------------------------------------
# _check_clob_settlement — race-condition hardening
#
# Regression coverage for a live-observed bug: closed=true can appear on a
# CLOB /markets response before winner/price have finished settling to
# their terminal 0/1 values (or via a stale cached response, given the
# CLOB sits behind Cloudflare). The old check trusted any "winner" key
# being *present* (even False) as proof of finality — this confirms the
# fix requires an explicit winner=True on some token first.
# ---------------------------------------------------------------------------

_CONDITION_ID = "0xabc123"
_UP_TOKEN = "up-token-id"
_DOWN_TOKEN = "down-token-id"


@pytest.mark.asyncio
async def test_settlement_not_closed_returns_none() -> None:
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_mocked=True) as mock:
            mock.get(f"https://clob.polymarket.com/markets/{_CONDITION_ID}").mock(
                return_value=httpx.Response(200, json={"closed": False, "tokens": []})
            )
            payout = await _check_clob_settlement(client, _CONDITION_ID, _UP_TOKEN)
    assert payout is None


@pytest.mark.asyncio
async def test_settlement_closed_but_no_winner_true_returns_none() -> None:
    """closed=true with only winner=False keys present must NOT be trusted —
    this is exactly the race window that produced a live misread."""
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_mocked=True) as mock:
            mock.get(f"https://clob.polymarket.com/markets/{_CONDITION_ID}").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "closed": True,
                        "tokens": [
                            {"token_id": _UP_TOKEN, "price": 0.4, "winner": False},
                            {"token_id": _DOWN_TOKEN, "price": 0.6, "winner": False},
                        ],
                    },
                )
            )
            payout = await _check_clob_settlement(client, _CONDITION_ID, _UP_TOKEN)
    assert payout is None


@pytest.mark.asyncio
async def test_settlement_closed_with_winner_returns_correct_payout() -> None:
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_mocked=True) as mock:
            mock.get(f"https://clob.polymarket.com/markets/{_CONDITION_ID}").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "closed": True,
                        "tokens": [
                            {"token_id": _UP_TOKEN, "price": 1, "winner": True},
                            {"token_id": _DOWN_TOKEN, "price": 0, "winner": False},
                        ],
                    },
                )
            )
            payout = await _check_clob_settlement(client, _CONDITION_ID, _UP_TOKEN)
    assert payout == Decimal("1")


@pytest.mark.asyncio
async def test_settlement_returns_losing_tokens_own_payout() -> None:
    """Payout returned is for the *fired* token specifically, not whichever won."""
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_mocked=True) as mock:
            mock.get(f"https://clob.polymarket.com/markets/{_CONDITION_ID}").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "closed": True,
                        "tokens": [
                            {"token_id": _UP_TOKEN, "price": 1, "winner": True},
                            {"token_id": _DOWN_TOKEN, "price": 0, "winner": False},
                        ],
                    },
                )
            )
            payout = await _check_clob_settlement(client, _CONDITION_ID, _DOWN_TOKEN)
    assert payout == Decimal("0")
