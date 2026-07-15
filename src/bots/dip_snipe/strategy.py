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
- ``entry_btc_price``, ``entry_at`` (str | null) — captured at trigger time
  for the optional provisional-winning-rule fast path (below)
- ``pending_coid`` (str | null)
- ``provisional_loss_streak`` (int), ``pending_validations`` (list) — fast
  provisional-rule continuation state, see class docstring

Optional fast-rule continuation (opt-in, see .claude/rules/10-winning-rules.md):
when a winning_rule is configured in config/bots.yaml and its configured
BTC price signal is subscribed (btc_fast for 5-minute-or-shorter windows —
see src/signals/btc_fast.py; btc_15min's 900s cadence is too slow to
detect a move within one), ``_tick_awaiting_settlement`` no longer blocks
indefinitely on Polymarket's official closed+winner fields (which can lag
minutes). After ``FAST_LOSS_STREAK`` consecutive LOST provisional rulings,
it advances to the next round as if lost, real capital included. This is a
deliberate, accepted tradeoff: the provisional rule can flip-flop, so a
wrong fast read can cause an extra real order before the true result is
known. Every fast-advanced round is tracked in ``_pending_validations`` and
re-checked against authoritative settlement every tick thereafter — if it
turns out to have actually been a win, the strategy stops immediately
(honors "stop on win" even late) rather than silently compounding the
error with further rounds.
"""

from __future__ import annotations

__all__ = ["DipSnipe3Round"]

import asyncio
import logging
import time
from datetime import datetime
from decimal import ROUND_UP, Decimal, InvalidOperation
from typing import Any, ClassVar, Literal, cast

import httpx

from src.bots.base import BaseBot, BotConfig, BotDeps, Decision, Schedule
from src.exchanges.base import Market, OrderTemplate, Side
from src.exchanges.market_resolver import resolve_all_outcomes
from src.manager.config import MarketRef
from src.manager.registry import registry
from src.rules.base import PositionState, ProvisionalRuling
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


def _btc_price_from_signals(signals: SignalSnapshot | None, signal_name: str) -> Decimal | None:
    """Read the live BTC price from ``signal_name``, if fresh and present.

    Mirrors the same signal/field access as
    ``src/rules/polymarket/btc_up_or_down_5m/price_compare.py`` — needed
    here too so ``current_position()`` can report an ``entry_reference``
    for that rule to compare against; ``signal_name`` must match the
    ``winning_rule.params.signal_name`` configured for this bot (see that
    rule's docstring — ``btc_15min``'s 900s cadence is too slow to detect
    a move within a 5-minute window; use ``btc_fast`` for 5-min bots).
    Returns ``None`` if the signal isn't configured, is stale, or doesn't
    parse — the fast-rule feature is inert in that case, not an error.
    """
    if signals is None or signal_name in signals.stale or signal_name not in signals.samples:
        return None
    try:
        return Decimal(str(signals.samples[signal_name]["price_usd"]))
    except (KeyError, TypeError, InvalidOperation):
        return None


async def _check_clob_settlement(
    client: httpx.AsyncClient, condition_id: str, token_id: str
) -> Decimal | None:
    """Query the CLOB for settlement; return the token's payout if resolved.

    Based on ``scripts/snipe_bot.py::_check_clob_settlement`` — CLOB, not
    Gamma, per ``.claude/rules/05-exchanges.md``. Returns ``None`` if not
    yet settled or the request fails.

    Two hardening changes vs. the original pattern, found live: this
    strategy polls every 5s (vs. snipe_bot.py's 30s), which is frequent
    enough to catch a real race window right as a market resolves —
    observed directly: the same condition_id returned ``closed: false``
    and, seconds later, ``closed: true`` with final winner/price data.
    ``"winner" in tok`` only checks the key is *present*, not that it's
    ``True`` — if the CLOB (behind Cloudflare, per its response headers)
    ever serves a cached or transitional response where `closed` has
    flipped but price/winner haven't finished settling to their terminal
    0/1 values, this would misread an interim price as the final payout.
    Fixed by requiring at least one token to be explicitly winner=True
    before trusting any token's price, and by cache-busting the request.
    """
    try:
        r = await client.get(
            f"{_CLOB_HOST}/markets/{condition_id}",
            timeout=10,
            headers={"Cache-Control": "no-cache"},
        )
        r.raise_for_status()
        data: dict[str, Any] = r.json()
    except Exception as exc:
        logger.warning("dip_snipe settlement check failed for %s: %s", condition_id, exc)
        return None
    if not data.get("closed"):
        return None
    tokens = data.get("tokens", [])
    if not any(tok.get("winner") is True for tok in tokens):
        # closed=true but resolution hasn't finished settling winner/price
        # on this response — not an error, just not final yet. Try again
        # next tick rather than trusting an interim price.
        return None
    for tok in tokens:
        if tok.get("token_id") == token_id:
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

    # Consecutive same-direction provisional rulings required before the
    # fast path acts on a LOST reading — the rule is expected to flip-flop
    # (.claude/rules/10-winning-rules.md), so a single tick is not enough.
    FAST_LOSS_STREAK: ClassVar[int] = 3

    def __init__(self, market: Market, config: BotConfig, deps: BotDeps) -> None:
        super().__init__(market, config, deps)
        p = config.strategy_params
        self._trigger = Decimal(str(p.get("trigger", self.TRIGGER)))
        self._size = Decimal(str(p.get("size", self.SIZE)))
        self._max_rounds = int(p.get("max_rounds", self.MAX_ROUNDS))
        self._min_window_secs = int(p.get("min_window_secs", self.MIN_WINDOW_SECS))
        # Must match the winning_rule's own signal_name param (see
        # src/rules/polymarket/btc_up_or_down_5m/price_compare.py) — read
        # from the same config source so the two can never drift apart.
        self._btc_signal_name = str(config.winning_rule_params.get("signal_name", "btc_15min"))
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

        # Fast-rule continuation state (opt-in via config/bots.yaml
        # winning_rule + signals — inert when neither is configured, since
        # current_position() only reports a position while genuinely
        # awaiting settlement and provisional_ruling() is None without a
        # configured rule).
        self._entry_btc_price: Decimal | None = None
        self._entry_at: datetime | None = None
        self._provisional_loss_streak: int = 0
        # Rounds advanced past on the fast rule's word, not yet confirmed by
        # authoritative settlement — checked every tick regardless of phase
        # so a wrong fast read still gets corrected once the real result is
        # known (see _check_pending_validations).
        self._pending_validations: list[dict[str, Any]] = []

    def current_position(self) -> PositionState | None:
        """Expose the held position while awaiting settlement, for the
        optional provisional-winning-rule feature
        (.claude/rules/10-winning-rules.md). Returns None whenever there's
        nothing held — inert unless a winning_rule is also configured."""
        if self._phase != "awaiting_settlement":
            return None
        if (
            self._condition_id is None
            or self._fired_outcome is None
            or self._entry_btc_price is None
            or self._entry_at is None
        ):
            return None
        return PositionState(
            market_id=self._condition_id,
            side=self._fired_outcome,
            entry_reference=self._entry_btc_price,
            entry_at=self._entry_at,
            size=self._size,
        )

    async def on_tick(self, signals: SignalSnapshot) -> Decision:
        if self._finished:
            return Decision(note=f"finished_{'won' if self._won else 'max_rounds'}")

        validation_decision = await self._check_pending_validations()
        if validation_decision is not None:
            return validation_decision

        if self._phase == "resolving":
            return await self._tick_resolving()
        if self._phase == "watching":
            return await self._tick_watching(signals)
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

    async def _tick_watching(self, signals: SignalSnapshot) -> Decision:
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
                self._entry_btc_price = _btc_price_from_signals(signals, self._btc_signal_name)
                self._entry_at = self._deps.clock.now()
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
        self._entry_btc_price = None
        self._entry_at = None
        self._pending_coid = None
        self._awaiting_fill_since = None
        self._phase = "watching" if (self._window_end or 0) > time.time() else "resolving"

    async def _tick_awaiting_settlement(self) -> Decision:
        assert self._condition_id is not None
        assert self._fired_token is not None

        payout = await _check_clob_settlement(self._http, self._condition_id, self._fired_token)
        if payout is not None:
            self._provisional_loss_streak = 0
            return self._resolve_round(payout)

        # Authoritative settlement not yet available — consult the fast
        # provisional rule (if configured) so the bot doesn't block on
        # Polymarket's official closed+winner fields, which can lag.
        # Explicit, accepted tradeoff (.claude/rules/10-winning-rules.md):
        # the ruling can flip-flop, so this can advance past a round that
        # authoritative settlement later reveals was actually a win —
        # tracked in _pending_validations and corrected by
        # _check_pending_validations once the real result is known.
        ruling = self.provisional_ruling()
        if ruling == ProvisionalRuling.LOST:
            self._provisional_loss_streak += 1
        else:
            self._provisional_loss_streak = 0

        if self._provisional_loss_streak < self.FAST_LOSS_STREAK:
            return Decision(note="awaiting_settlement")

        logger.warning(
            "dip_snipe bot=%s fast rule: %d consecutive LOST readings, advancing "
            "without authoritative settlement (round=%d) — pending validation",
            self._config.bot_id,
            self._provisional_loss_streak,
            self._rounds_played,
        )
        self._pending_validations.append(
            {
                "condition_id": self._condition_id,
                "fired_token": self._fired_token,
                "round_index": self._rounds_played,
            }
        )
        self._provisional_loss_streak = 0
        self._position_notional = Decimal("0")
        return self._advance_after_loss(fast=True)

    def _resolve_round(self, payout: Decimal) -> Decision:
        """Apply an authoritative settlement payout: win stops, loss advances."""
        self._position_notional = Decimal("0")
        if payout >= Decimal("0.5"):
            self._won = True
            self._finished = True
            logger.info(
                "dip_snipe bot=%s WON round=%d — stopping.",
                self._config.bot_id,
                self._rounds_played,
            )
            return Decision(note="round_won_stopping")
        return self._advance_after_loss(fast=False)

    def _advance_after_loss(self, *, fast: bool) -> Decision:
        """Stop if max_rounds reached, else reset and move to the next window.

        Args:
            fast: True if this loss was decided by the fast provisional
                rule rather than authoritative settlement (logged/noted
                distinctly; see _pending_validations for the correction path).
        """
        suffix = "_fast" if fast else ""
        if self._rounds_played >= self._max_rounds:
            self._won = False
            self._finished = True
            logger.info(
                "dip_snipe bot=%s max rounds (%d) reached, last round lost%s — stopping.",
                self._config.bot_id,
                self._max_rounds,
                " (fast rule, pending validation)" if fast else "",
            )
            return Decision(note=f"round_lost_max_rounds_stopping{suffix}")

        self._reset_round_fields()
        self._phase = "resolving"
        logger.info(
            "dip_snipe bot=%s round lost (%d/%d)%s — continuing to next window.",
            self._config.bot_id,
            self._rounds_played,
            self._max_rounds,
            " (fast rule, pending validation)" if fast else "",
        )
        return Decision(note=f"round_lost_continuing{suffix}")

    async def _check_pending_validations(self) -> Decision | None:
        """Reconcile any rounds advanced past via the fast rule.

        Runs every tick regardless of phase. If authoritative settlement
        later shows a fast-advanced round was actually a WIN, honors
        "stop on win" immediately even though the strategy already moved
        on — better a late correction than silently compounding the error
        with further real rounds. A confirmed loss just gets dropped
        (nothing to correct).

        Returns:
            A stopping Decision if a correction fired this tick, else None
            (meaning: proceed with the normal phase-dispatch tick as usual).
        """
        if not self._pending_validations or self._finished:
            return None

        still_pending: list[dict[str, Any]] = []
        for entry in self._pending_validations:
            payout = await _check_clob_settlement(
                self._http, entry["condition_id"], entry["fired_token"]
            )
            if payout is None:
                still_pending.append(entry)
                continue

            if payout >= Decimal("0.5"):
                logger.error(
                    "dip_snipe bot=%s fast-rule correction: round=%d was actually a "
                    "WIN (condition_id=%s) — stopping now despite having already "
                    "continued past it.",
                    self._config.bot_id,
                    entry["round_index"],
                    entry["condition_id"],
                )
                self._won = True
                self._finished = True
                self._pending_validations = still_pending
                return Decision(note="fast_rule_correction_actually_won_stopping")

            logger.info(
                "dip_snipe bot=%s fast-rule validation confirmed: round=%d was correctly a loss.",
                self._config.bot_id,
                entry["round_index"],
            )

        self._pending_validations = still_pending
        return None

    def _reset_round_fields(self) -> None:
        self._condition_id = None
        self._full_slug = None
        self._up_token = None
        self._dn_token = None
        self._window_end = None
        self._fired_token = None
        self._fired_outcome = None
        self._entry_price = None
        self._entry_btc_price = None
        self._entry_at = None
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
            "entry_btc_price": (
                str(self._entry_btc_price) if self._entry_btc_price is not None else None
            ),
            "entry_at": self._entry_at.isoformat() if self._entry_at is not None else None,
            "pending_coid": self._pending_coid,
            "awaiting_fill_since": self._awaiting_fill_since,
            "provisional_loss_streak": self._provisional_loss_streak,
            "pending_validations": list(self._pending_validations),
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
        entry_btc_price = snapshot.get("entry_btc_price")
        self._entry_btc_price = (
            Decimal(str(entry_btc_price)) if entry_btc_price is not None else None
        )
        entry_at = snapshot.get("entry_at")
        self._entry_at = datetime.fromisoformat(entry_at) if entry_at else None
        self._pending_coid = snapshot.get("pending_coid")
        self._awaiting_fill_since = snapshot.get("awaiting_fill_since")
        self._provisional_loss_streak = int(snapshot.get("provisional_loss_streak", 0))
        self._pending_validations = list(snapshot.get("pending_validations", []))
