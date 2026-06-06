"""momentum_v1 — BTC momentum binary prediction-market strategy.

Subscribes to the ``btc_15min`` signal and trades a binary BTC price-prediction
market (e.g. "Will BTC close above $X on date Y?").  The market's ``token_id``
is the YES outcome token — the strategy always trades YES shares.

Decision logic (per tick):
1. If ``btc_15min`` is stale → skip, no order.
2. Compute momentum: ``(current_price - prev_price) / prev_price * 100``.
3. No position + momentum ≥ ``MOMENTUM_THRESHOLD_PCT`` → BUY YES.
4. In position + ticks held ≥ ``MAX_TICKS_IN_POSITION`` → SELL to exit.
5. In position + momentum ≤ ``-MOMENTUM_THRESHOLD_PCT`` → SELL to exit early.
6. First tick after startup/rehydrate has no prior price → no-op, record price.

Snapshot keys (beyond BaseBot required):
- ``prev_btc_price`` (str | null) — last seen BTC/USD price
- ``position_size`` (str) — YES shares currently held
- ``ticks_in_position`` (int) — ticks elapsed since last entry
"""

from __future__ import annotations

__all__ = ["MomentumV1"]

import logging
from decimal import Decimal
from typing import Any, ClassVar

from src.bots.base import BaseBot, BotConfig, BotDeps, Decision, Schedule
from src.exchanges.base import Market, OrderTemplate, Side
from src.manager.registry import registry
from src.signals.base import SignalSnapshot

logger = logging.getLogger(__name__)

_SIGNAL = "btc_15min"


@registry.strategy
class MomentumV1(BaseBot):
    """BTC momentum strategy — enters YES when BTC price jumps up.

    Class-level parameters can be overridden in a subclass for experimentation;
    the live/paper config in ``bots.yaml`` selects which class to run.

    Attributes:
        MOMENTUM_THRESHOLD_PCT: Minimum % BTC move to trigger an entry.
        TARGET_SIZE: Desired position size in YES shares.
        MAX_TICKS_IN_POSITION: Forced exit after this many held ticks.
        BUY_PRICE: Limit price for YES buys (probability in [0, 1]).
        SELL_PRICE: Limit price for YES sells when exiting.
    """

    name: ClassVar[str] = "momentum_v1"
    schedule: ClassVar[Schedule] = Schedule(every_seconds=900)

    MOMENTUM_THRESHOLD_PCT: ClassVar[Decimal] = Decimal("0.5")
    TARGET_SIZE: ClassVar[Decimal] = Decimal("10")
    MAX_TICKS_IN_POSITION: ClassVar[int] = 4
    BUY_PRICE: ClassVar[Decimal] = Decimal("0.55")
    SELL_PRICE: ClassVar[Decimal] = Decimal("0.45")

    def __init__(self, market: Market, config: BotConfig, deps: BotDeps) -> None:
        super().__init__(market, config, deps)
        p = config.strategy_params
        self._momentum_threshold = Decimal(str(p.get("momentum_threshold_pct", self.MOMENTUM_THRESHOLD_PCT)))
        self._target_size_cfg    = Decimal(str(p.get("target_size",             self.TARGET_SIZE)))
        self._max_ticks          = int(p.get("max_ticks_in_position",           self.MAX_TICKS_IN_POSITION))
        self._buy_price          = Decimal(str(p.get("buy_price",               self.BUY_PRICE)))
        self._sell_price         = Decimal(str(p.get("sell_price",              self.SELL_PRICE)))
        self._prev_btc_price: Decimal | None = None
        self._position_size: Decimal = Decimal("0")
        self._ticks_in_position: int = 0

    async def on_tick(self, signals: SignalSnapshot) -> Decision:
        """Execute one strategy tick.

        Args:
            signals: Fresh signal snapshot — must include ``btc_15min``.

        Returns:
            Decision with zero or one OrderTemplate.
        """
        if _SIGNAL in signals.stale:
            logger.warning(
                "btc_15min stale — skipping tick bot=%s", self._config.bot_id
            )
            return Decision(note="btc_15min_stale")

        sample = signals.samples.get(_SIGNAL)
        if sample is None:
            logger.warning(
                "btc_15min absent from snapshot bot=%s", self._config.bot_id
            )
            return Decision(note="btc_15min_absent")

        current_price = Decimal(str(sample["price_usd"]))
        intents: list[OrderTemplate] = []
        note = "hold"

        if self._prev_btc_price is not None:
            momentum_pct = (
                (current_price - self._prev_btc_price) / self._prev_btc_price * 100
            )

            if self._position_size > 0:
                self._ticks_in_position += 1
                timeout = self._ticks_in_position >= self._max_ticks
                reversal = momentum_pct <= -self._momentum_threshold
                if timeout or reversal:
                    intents.append(
                        OrderTemplate(
                            market=self._market,
                            side=Side.SELL,
                            price=self._sell_price,
                            size=self._position_size,
                        )
                    )
                    reason = "timeout" if timeout else "reversal"
                    note = (
                        f"exit reason={reason} ticks={self._ticks_in_position}"
                        f" momentum={momentum_pct:.3f}%"
                    )
                    self._position_size = Decimal("0")
                    self._ticks_in_position = 0
                else:
                    note = f"hold ticks={self._ticks_in_position} momentum={momentum_pct:.3f}%"

            elif momentum_pct >= self._momentum_threshold:
                buy_size = max(self._target_size_cfg, self._market.min_size)
                intents.append(
                    OrderTemplate(
                        market=self._market,
                        side=Side.BUY,
                        price=self._buy_price,
                        size=buy_size,
                    )
                )
                self._position_size = buy_size
                self._ticks_in_position = 0
                note = f"enter momentum={momentum_pct:.3f}%"

        else:
            note = "first_tick"

        self._prev_btc_price = current_price
        logger.info(
            "momentum_v1 tick bot=%s btc=%s note=%s",
            self._config.bot_id,
            current_price,
            note,
        )
        return Decision(intents=intents, note=note)

    def snapshot(self) -> dict[str, Any]:
        """Serialise strategy state.

        Returns:
            Dict including required BaseBot keys plus strategy-specific fields.
        """
        return {
            "intent_seq": self._intent_seq,
            "position": str(self._position_notional),
            "last_decision_at": self._deps.clock.now().isoformat(),
            "prev_btc_price": (
                str(self._prev_btc_price) if self._prev_btc_price is not None else None
            ),
            "position_size": str(self._position_size),
            "ticks_in_position": self._ticks_in_position,
        }

    def rehydrate(self, snapshot: dict[str, Any]) -> None:
        """Restore state from a previously written snapshot.

        Idempotent: calling twice with the same snapshot produces the same
        state as calling once.

        Args:
            snapshot: Dict from ``bot_state.state``, produced by :meth:`snapshot`.
        """
        self._intent_seq = int(snapshot.get("intent_seq", 0))
        self._position_notional = Decimal(str(snapshot.get("position", "0")))
        prev = snapshot.get("prev_btc_price")
        self._prev_btc_price = Decimal(str(prev)) if prev is not None else None
        self._position_size = Decimal(str(snapshot.get("position_size", "0")))
        self._ticks_in_position = int(snapshot.get("ticks_in_position", 0))
