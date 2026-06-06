"""Pydantic schema for config/bots.yaml — the bot roster.

The manager loads and validates the whole roster at startup.  A single
invalid entry fails the whole load: no partial roster, no silent surprises.

See CLAUDE.md § Bot model and plan/05-manager-supervisor.md for the schema
narrative.
"""

from __future__ import annotations

__all__ = [
    "BotEntry",
    "BotsRoster",
    "MarketRef",
    "RiskOverride",
    "SecretRef",
    "SignalSubscription",
    "load_roster",
]

import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, Field, model_validator


class MarketRef(BaseModel):
    """Reference to a specific market on a specific exchange.

    Two forms are supported:

    **Static** — the market IDs are known ahead of time::

        exchange: polymarket
        market_id: "0xabc..."   # condition_id
        token_id: "123..."      # ERC-1155 outcome token (polymarket only)

    **Dynamic** — the concrete IDs are resolved at runtime via the venue API::

        exchange: polymarket
        slug: "btc-updown-15m"  # slug prefix; resolver appends the current window timestamp
        outcome: "UP"           # which token to trade (UP/DOWN/YES/NO)

    Args:
        exchange: Venue name — one of ``"polymarket"``, ``"kalshi"``, ``"echo"``.
        market_id: Canonical market identifier (static markets only).
        token_id: ERC-1155 outcome token ID (polymarket static markets only).
        slug: Slug prefix for dynamic/recurring markets resolved at runtime.
        outcome: Outcome name for dynamic markets (e.g. ``"UP"``, ``"DOWN"``).
    """

    exchange: Literal["polymarket", "kalshi", "echo"]
    market_id: str | None = None
    token_id: str | None = None
    slug: str | None = None
    outcome: str | None = None

    @model_validator(mode="after")
    def _validate_spec(self) -> Self:
        has_slug = self.slug is not None
        if has_slug:
            if self.market_id is not None or self.token_id is not None:
                raise ValueError(
                    "Specify either slug (dynamic) or market_id/token_id (static), not both."
                )
            if self.outcome is None:
                raise ValueError("slug requires outcome (e.g. 'UP', 'DOWN', 'YES', 'NO').")
            return self
        # Static path: market_id is required
        if self.market_id is None:
            raise ValueError(
                "market_id is required for static markets (or use slug for dynamic resolution)."
            )
        # token_id required for Polymarket (ERC-1155 outcome token)
        if self.exchange == "polymarket" and self.token_id is None:
            raise ValueError(
                "token_id is required for polymarket static markets. "
                "Use slug + outcome for dynamic/recurring markets."
            )
        return self


class SignalSubscription(BaseModel):
    """A single signal feed subscription for a bot.

    Args:
        name: Registry key of the signal (e.g. ``"btc_usd_price"``).
        params: Optional feed-specific parameters (e.g. window size).
    """

    name: str
    params: dict[str, Any] = Field(default_factory=dict)


class RiskOverride(BaseModel):
    """Per-bot risk cap overrides.

    All caps are mandatory — there is no implicit global default so every
    bot entry must state its risk tolerance explicitly.

    Args:
        max_position_notional: Maximum open position size in collateral units.
        max_daily_loss: Maximum cumulative daily loss (positive value).
        max_orders_per_minute: Maximum orders placed in any rolling 60-second
            window (burst protection).
    """

    max_position_notional: Decimal
    max_daily_loss: Decimal
    max_orders_per_minute: int

    @model_validator(mode="after")
    def _positive_values(self) -> Self:
        """Ensure all numeric caps are strictly positive."""
        if self.max_position_notional <= 0:
            raise ValueError(
                f"max_position_notional must be positive, got {self.max_position_notional}"
            )
        if self.max_daily_loss <= 0:
            raise ValueError(f"max_daily_loss must be positive, got {self.max_daily_loss}")
        if self.max_orders_per_minute <= 0:
            raise ValueError(
                f"max_orders_per_minute must be positive, got {self.max_orders_per_minute}"
            )
        return self


class SecretRef(BaseModel):
    """References to keys in ``secrets/*.enc.yaml`` — never plaintext.

    The value is a dotted path into the decrypted secrets dict, e.g.
    ``"polymarket.disposable"`` resolves to
    ``secrets["exchanges"]["polymarket"]["disposable"]``.

    Args:
        exchange_credentials: Dotted key path into the loaded secrets.
    """

    exchange_credentials: str


# Matches "every:Ns" where N is a positive integer, e.g. "every:60s".
_EVERY_PATTERN = re.compile(r"^every:(\d+)s$")


def _parse_schedule(value: str) -> int:
    """Parse a schedule string into an interval in seconds.

    Args:
        value: Schedule string, e.g. ``"every:60s"``.

    Returns:
        Interval in seconds.

    Raises:
        NotImplementedError: If the string uses cron syntax (not yet supported).
        ValueError: If the string is not a valid schedule expression.
    """
    m = _EVERY_PATTERN.match(value)
    if m:
        seconds = int(m.group(1))
        if seconds <= 0:
            raise ValueError(f"Schedule interval must be positive, got {seconds!r}")
        return seconds
    # Detect cron-like strings (contain spaces or special cron chars) and
    # give a clear error rather than a confusing parse failure.
    if " " in value or value.startswith("@"):
        raise NotImplementedError(
            f"Cron-style schedules are not yet supported: {value!r}. "
            "Use 'every:Ns' syntax (e.g. 'every:900s')."
        )
    raise ValueError(
        f"Invalid schedule string: {value!r}. Expected format: 'every:Ns' (e.g. 'every:60s')."
    )


class BotEntry(BaseModel):
    """One bot instance as declared in ``config/bots.yaml``.

    Args:
        id: Stable, unique bot identifier.  Used as the ``bot_id`` in all
            DB tables.  Must never change after the first deployment.
        strategy: Registered strategy name (registry key).
        market: Market this bot trades.
        mode: Execution mode — ``"paper"`` (default) or ``"live"``.
            Setting ``"live"`` here is one of the three locks (invariant 1).
        schedule: Tick schedule string, e.g. ``"every:60s"``.
        signals: Signal subscriptions declared by this bot.
        risk: Risk cap overrides (mandatory).
        secrets: Symbolic references to secrets (never plaintext).
        alerts: Optional per-bot alerting route overrides.
    """

    id: str = Field(..., pattern=r"^[a-z0-9_-]{3,64}$")
    strategy: str
    market: MarketRef
    mode: Literal["paper", "live"] = "paper"
    schedule: str
    signals: list[SignalSubscription] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    risk: RiskOverride
    secrets: SecretRef
    alerts: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_schedule(self) -> Self:
        """Validate and parse the schedule string on construction."""
        _parse_schedule(self.schedule)  # raises ValueError / NotImplementedError on bad input
        return self

    def schedule_seconds(self) -> int:
        """Return the schedule interval in seconds.

        Returns:
            Positive integer tick interval.
        """
        return _parse_schedule(self.schedule)


class BotsRoster(BaseModel):
    """The complete roster of bot instances from ``config/bots.yaml``.

    Validates on load that:
    - All bot ids are unique.
    - Every ``BotEntry`` passes its own validation (schedule, risk caps, etc.).

    Args:
        bots: List of bot entries.  An empty list is valid (no bots spawned).
    """

    bots: list[BotEntry]

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        """Reject duplicate bot ids — they partition the DB and must be unique."""
        seen: set[str] = set()
        for entry in self.bots:
            if entry.id in seen:
                raise ValueError(
                    f"Duplicate bot id in roster: {entry.id!r}. "
                    "Each bot must have a globally unique id."
                )
            seen.add(entry.id)
        return self


def load_roster(path: Path) -> BotsRoster:
    """Load and validate ``config/bots.yaml``.

    Args:
        path: Absolute path to the bots YAML file.

    Returns:
        Validated :class:`BotsRoster`.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If the YAML is malformed or fails Pydantic validation.
    """
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping at the top level, got {type(data).__name__}")
    return BotsRoster.model_validate(data)
