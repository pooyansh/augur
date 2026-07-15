"""dip_snipe_3round — multi-window dip-buyer with a bounded win/loss cap.

Watches a recurring Polymarket BTC Up/Down market (e.g. the 5-minute
cadence) window by window.  Each window it waits for either outcome's ask to
drop to or below a trigger price, buys the minimum size, and holds to
settlement.  A win stops the strategy immediately; a loss moves on to the
next window, up to ``max_rounds`` actually-placed bets.

This is the manager-supervised, dashboard-visible equivalent of the one-off
``scripts/snipe_bot.py`` CLI tool — same dip-trigger entry and CLOB-direct
settlement check, but expressed as a proper ``BaseBot`` strategy so it goes
through the roster, risk caps, the three-lock live gate, and the dashboard
instead of being invoked ad hoc.

A "round" is an actually placed *and accepted* order. A window that closes
without the trigger firing, or an order that gets rejected, does not consume
a round — the strategy simply moves to (or stays in) watching the next
opportunity.

Snapshot keys (beyond the BaseBot-mandatory three):
- ``phase`` (str) — one of "resolving", "watching", "awaiting_fill",
  "awaiting_settlement", "finished"
- ``rounds_played`` (int)
- ``won`` (bool | null)
- ``finished`` (bool)
- ``condition_id``, ``full_slug``, ``up_token``, ``dn_token`` (str | null)
- ``window_end`` (float | null)
- ``fired_token``, ``fired_outcome``, ``entry_price`` (str | null)
- ``pending_coid`` (str | null)
"""

from __future__ import annotations

__all__ = ["DipSnipe3Round"]

import asyncio
import logging
import time
from decimal import ROUND_UP, Decimal
from typing import Any, ClassVar, Literal, cast

import httpx

from src.bots.base import BaseBot, BotConfig, BotDeps, Decision, Schedule
from src.exchanges.base import Market, OrderTemplate, Side
from src.exchanges.market_resolver import resolve_all_outcomes
from src.manager.config import MarketRef
from src.manager.registry import registry
from src.signals.base import SignalSnapshot

logger = logging.getLogger(__name__)

_CLOB_HOST = "https://clob.polymarket.com"
_MIN_NOTIONAL = Decimal("1.00")


def _effective_size(size: Decimal, ask: Decimal) -> Decimal:
    """Bump size up if needed to meet the $1.00 CLOB minimum notional."""
    if ask <= 0:
        return size
    if ask * size < _MIN_NOTIONAL:
        return (_MIN_NOTIONAL / ask).to_integral_value(rounding=ROUND_UP)
    return size


async def _best_ask(client: httpx.AsyncClient, token_id: str) -> Decimal:
    """Fetch the current best ask for ``token_id`` from the CLOB order book.

    Returns ``Decimal("1")`` (i.e. "no ask, never trigger") if the book is
    empty or the request fails — a transient/empty read must never look like
    a false trigger.
    """
    try:
        r = await client.get(f"{_CLOB_HOST}/book", params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        asks = r.json().get("asks", [])
    except Exception as exc:
        logger.warning("dip_snipe best-ask fetch failed for %s: %s", token_id, exc)
        return Decimal("1")
    prices = [Decimal(str(a["price"])) for a in asks if Decimal(str(a.get("size", "0"))) > 0]
    return min(prices) if prices else Decimal("1")


async def _check_clob_settlement(
    client: httpx.AsyncClient, condition_id: str, token_id: str
) -> Decimal | None:
    """Query the CLOB for settlement; return the token's payout if resolved.

    Mirrors ``scripts/snipe_bot.py::_check_clob_settlement`` (proven,
    live-verified pattern) — CLOB, not Gamma, per
    ``.claude/rules/05-exchanges.md``.  Returns ``None`` if not yet settled
    or the request fails.
    """
    try:
        r = await client.get(f"{_CLOB_HOST}/markets/{condition_id}", timeout=10)
        r.raise_for_status()
        data: dict[str, Any] = r.json()
    except Exception as exc:
        logger.warning("dip_snipe settlement check failed for %s: %s", condition_id, exc)
        return None
    if not data.get("closed"):
        return None
    for tok in data.get("tokens", []):
        if tok.get("token_id") == token_id and "winner" in tok:
            return Decimal(str(tok["price"]))
    return None


@registry.strategy
class DipSnipe3Round(BaseBot):
    """Dip-trigger buyer with a bounded win/loss round cap across windows.

    Attributes:
        TRIGGER: Ask price at or below which to fire.
        SIZE: Shares to buy per round.
        MAX_ROUNDS: Maximum number of actually-placed bets before stopping.
        MIN_WINDOW_SECS: Skip a window with fewer seconds remaining than this.
    """

    name: ClassVar[str] = "dip_snipe_3round"
    schedule: ClassVar[Schedule] = Schedule(every_seconds=5)

    TRIGGER: ClassVar[Decimal] = Decimal("0.30")
    SIZE: ClassVar[Decimal] = Decimal("5")
    MAX_ROUNDS: ClassVar[int] = 3
    MIN_WINDOW_SECS: ClassVar[int] = 120
    AWAITING_FILL_TIMEOUT_SECS: ClassVar[float] = 15.0

    def __init__(self, market: Market, config: BotConfig, deps: BotDeps) -> None:
        super().__init__(market, config, deps)
        p = config.strategy_params
        self._trigger = Decimal(str(p.get("trigger", self.TRIGGER)))
        self._size = Decimal(str(p.get("size", self.SIZE)))
        self._max_rounds = int(p.get("max_rounds", self.MAX_ROUNDS))
        self._min_window_secs = int(p.get("min_window_secs", self.MIN_WINDOW_SECS))
        self._ref = MarketRef(
            exchange=cast(
                Literal["polymarket", "kalshi", "echo"], str(p.get("exchange", market.venue))
            ),
            slug=str(p["slug"]),
            outcome=str(p.get("outcome", "UP")),
        )

        self._http = httpx.AsyncClient()

        self._phase: str = "resolving"
        self._rounds_played = 0
        self._won: bool | None = None
        self._finished = False
        self._condition_id: str | None = None
        self._full_slug: str | None = None
        self._up_token: str | None = None
        self._dn_token: str | None = None
        self._window_end: float | None = None
        self._fired_token: str | None = None
        self._fired_outcome: str | None = None
        self._entry_price: Decimal | None = None
        self._pending_coid: str | None = None
        self._awaiting_fill_since: float | None = None

    async def on_tick(self, signals: SignalSnapshot) -> Decision:
        if self._finished:
            return Decision(note=f"finished_{'won' if self._won else 'max_rounds'}")

        if self._phase == "resolving":
            return await self._tick_resolving()
        if self._phase == "watching":
            return await self._tick_watching()
        if self._phase == "awaiting_fill":
            return self._tick_awaiting_fill()
        if self._phase == "awaiting_settlement":
            return await self._tick_awaiting_settlement()

        logger.error("dip_snipe unknown phase %r — resetting to resolving", self._phase)
        self._phase = "resolving"
        return Decision(note="unknown_phase_reset")

    async def _tick_resolving(self) -> Decision:
        try:
            resolved = await asyncio.to_thread(resolve_all_outcomes, self._ref)
        except Exception as exc:
            logger.warning("dip_snipe market resolve failed: %s", exc)
            return Decision(note="resolve_failed")

        window_end = resolved.window_end if resolved.window_end is not None else (time.time() + 900)
        secs_left = window_end - time.time()
        if secs_left < self._min_window_secs:
            return Decision(note=f"waiting_next_window secs_left={secs_left:.0f}")

        outcome_map = {k.lower(): (k, v) for k, v in resolved.tokens.items()}
        up_label, up_token = outcome_map.get("up", outcome_map.get("yes", ("Up", "")))
        dn_label, dn_token = outcome_map.get("down", outcome_map.get("no", ("Down", "")))
        if not up_token or not dn_token:
            logger.error("dip_snipe unexpected outcomes: %s", list(resolved.tokens.keys()))
            return Decision(note="unexpected_outcomes")

        self._condition_id = resolved.condition_id
        self._full_slug = resolved.slug
        self._up_token = up_token
        self._dn_token = dn_token
        self._window_end = window_end
        self._phase = "watching"
        logger.info(
            "dip_snipe bot=%s window resolved slug=%s round=%d/%d",
            self._config.bot_id,
            resolved.slug,
            self._rounds_played,
            self._max_rounds,
        )
        return Decision(note=f"market_resolved slug={resolved.slug} up={up_label} dn={dn_label}")

    async def _tick_watching(self) -> Decision:
        assert self._window_end is not None
        assert self._up_token is not None
        assert self._dn_token is not None

        if time.time() >= self._window_end:
            self._reset_round_fields()
            self._phase = "resolving"
            return Decision(note="window_closed_no_fill")

        up_ask = await _best_ask(self._http, self._up_token)
        dn_ask = await _best_ask(self._http, self._dn_token)

        candidates = [("Up", self._up_token, up_ask), ("Down", self._dn_token, dn_ask)]
        for outcome_name, token_id, ask in candidates:
            if Decimal("0") < ask <= self._trigger:
                effective = _effective_size(self._size, ask)
                template = OrderTemplate(
                    market=Market(
                        market_id=self._condition_id or "",
                        token_id=token_id,
                        tick_size=Decimal("0.01"),
                        min_size=Decimal("5"),
                        venue="polymarket",
                    ),
                    side=Side.BUY,
                    price=ask,
                    size=effective,
                )
                self._fired_token = token_id
                self._fired_outcome = outcome_name
                self._entry_price = ask
                self._pending_coid = self._peek_next_client_order_id()
                self._awaiting_fill_since = time.time()
                self._phase = "awaiting_fill"
                logger.info(
                    "dip_snipe bot=%s TRIGGER %s ask=%s size=%s",
                    self._config.bot_id,
                    outcome_name,
                    ask,
                    effective,
                )
                return Decision(
                    intents=[template],
                    note=f"trigger_fired outcome={outcome_name} ask={ask} size={effective}",
                )

        return Decision(note=f"watching up_ask={up_ask} dn_ask={dn_ask}")

    def _tick_awaiting_fill(self) -> Decision:
        if self._pending_coid is None:
            self._phase = "watching"
            return Decision(note="awaiting_fill_no_pending_id")

        result = self._inflight.get(self._pending_coid)
        if result is None:
            # `_awaiting_fill_since` can only be unset here via `rehydrate` —
            # `_tick_watching` always sets a real timestamp together with
            # `pending_coid`. A missing value means this phase was restored
            # from a snapshot written before this process existed, so the
            # order predates this process's `_inflight` cache entirely and
            # can never resolve. `... or time.time()` would restart the
            # timeout window on every tick forever (`time.time() - time.time()`
            # is always ~0) — treat a missing timestamp as already-expired
            # instead of freshly-started.
            if self._awaiting_fill_since is not None and (
                time.time() - self._awaiting_fill_since < self.AWAITING_FILL_TIMEOUT_SECS
            ):
                return Decision(note="awaiting_fill_result")
            # place() never populated _inflight for this id — the intent was
            # blocked before reaching the adapter (RiskCapExceeded or
            # KillSwitchTripped bypass BaseBot's normal accepted/rejected
            # path entirely). Give up waiting on an id that will never
            # arrive rather than stalling in this phase forever.
            logger.warning(
                "dip_snipe bot=%s pending order %s never resolved after %.0fs "
                "(likely blocked by a risk cap or kill switch) — giving up, not counting a round",
                self._config.bot_id,
                self._pending_coid,
                self.AWAITING_FILL_TIMEOUT_SECS,
            )
            self._clear_pending_order()
            return Decision(note="awaiting_fill_timed_out")

        if result.accepted:
            self._rounds_played += 1
            assert self._entry_price is not None
            self._position_notional = self._entry_price * self._size
            self._phase = "awaiting_settlement"
            return Decision(note="order_accepted_holding")

        logger.warning(
            "dip_snipe bot=%s order rejected: %s — not counting a round",
            self._config.bot_id,
            result.reason,
        )
        self._clear_pending_order()
        return Decision(note=f"order_rejected reason={result.reason}")

    def _clear_pending_order(self) -> None:
        self._fired_token = None
        self._fired_outcome = None
        self._entry_price = None
        self._pending_coid = None
        self._awaiting_fill_since = None
        self._phase = "watching" if (self._window_end or 0) > time.time() else "resolving"

    async def _tick_awaiting_settlement(self) -> Decision:
        assert self._condition_id is not None
        assert self._fired_token is not None

        payout = await _check_clob_settlement(self._http, self._condition_id, self._fired_token)
        if payout is None:
            return Decision(note="awaiting_settlement")

        self._position_notional = Decimal("0")
        won_this_round = payout >= Decimal("0.5")

        if won_this_round:
            self._won = True
            self._finished = True
            logger.info(
                "dip_snipe bot=%s WON round=%d — stopping.",
                self._config.bot_id,
                self._rounds_played,
            )
            return Decision(note="round_won_stopping")

        if self._rounds_played >= self._max_rounds:
            self._won = False
            self._finished = True
            logger.info(
                "dip_snipe bot=%s max rounds (%d) reached, last round lost — stopping.",
                self._config.bot_id,
                self._max_rounds,
            )
            return Decision(note="round_lost_max_rounds_stopping")

        self._reset_round_fields()
        self._phase = "resolving"
        logger.info(
            "dip_snipe bot=%s round lost (%d/%d) — continuing to next window.",
            self._config.bot_id,
            self._rounds_played,
            self._max_rounds,
        )
        return Decision(note="round_lost_continuing")

    def _reset_round_fields(self) -> None:
        self._condition_id = None
        self._full_slug = None
        self._up_token = None
        self._dn_token = None
        self._window_end = None
        self._fired_token = None
        self._fired_outcome = None
        self._entry_price = None
        self._pending_coid = None

    def _peek_next_client_order_id(self) -> str:
        """Predict the ``client_order_id`` the base will assign to this
        tick's (sole) order template.

        ``on_tick`` returns exactly one intent when triggering, and
        ``BaseBot.place()`` assigns ids by incrementing ``_intent_seq`` in
        the same deterministic scheme used everywhere else
        (``blake2s(f"{bot_id}:{intent_seq}")``) — safe to predict here since
        no other intent can be placed between this tick and the next.
        """
        from hashlib import blake2s

        next_seq = self._intent_seq + 1
        raw = f"{self._config.bot_id}:{next_seq}".encode()
        return blake2s(raw, digest_size=8).hexdigest()

    def snapshot(self) -> dict[str, Any]:
        return {
            "intent_seq": self._intent_seq,
            "position": str(self._position_notional),
            "last_decision_at": self._deps.clock.now().isoformat(),
            "phase": self._phase,
            "rounds_played": self._rounds_played,
            "won": self._won,
            "finished": self._finished,
            "condition_id": self._condition_id,
            "full_slug": self._full_slug,
            "up_token": self._up_token,
            "dn_token": self._dn_token,
            "window_end": self._window_end,
            "fired_token": self._fired_token,
            "fired_outcome": self._fired_outcome,
            "entry_price": str(self._entry_price) if self._entry_price is not None else None,
            "pending_coid": self._pending_coid,
            "awaiting_fill_since": self._awaiting_fill_since,
        }

    def rehydrate(self, snapshot: dict[str, Any]) -> None:
        self._intent_seq = int(snapshot.get("intent_seq", 0))
        self._position_notional = Decimal(str(snapshot.get("position", "0")))
        self._phase = str(snapshot.get("phase", "resolving"))
        self._rounds_played = int(snapshot.get("rounds_played", 0))
        self._won = snapshot.get("won")
        self._finished = bool(snapshot.get("finished", False))
        self._condition_id = snapshot.get("condition_id")
        self._full_slug = snapshot.get("full_slug")
        self._up_token = snapshot.get("up_token")
        self._dn_token = snapshot.get("dn_token")
        self._window_end = snapshot.get("window_end")
        self._fired_token = snapshot.get("fired_token")
        self._fired_outcome = snapshot.get("fired_outcome")
        entry_price = snapshot.get("entry_price")
        self._entry_price = Decimal(str(entry_price)) if entry_price is not None else None
        self._pending_coid = snapshot.get("pending_coid")
        self._awaiting_fill_since = snapshot.get("awaiting_fill_since")
