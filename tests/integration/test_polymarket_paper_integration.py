"""Integration test: Polymarket paper mode end-to-end.

Requires live Polymarket WS connectivity.  Skipped by default unless
``POLYMARKET_PAPER_TEST=1`` is set in the environment.

TODO(phase-4-integration): Implement full paper integration test:
  1. Create a PolymarketAdapter in paper mode.
  2. Subscribe to a live token_id.
  3. Place a paper resting order at a price away from the market.
  4. Cancel it and verify a CancelEvent is emitted.
  5. Place a paper taking order (crossing the spread).
  6. Verify a FillEvent is emitted with price == best_ask (BUY) or best_bid (SELL).
  7. Wait for any SettlementEvent on a known resolved market.

Minimum window before live promotion: 5 days of paper trading with < N%
simulated fill deviation (defined in .claude/rules/07-testing.md Phase 7).
"""

from __future__ import annotations

import os

import pytest

# Skip the entire module unless the env var is set
pytestmark = pytest.mark.skipif(
    os.environ.get("POLYMARKET_PAPER_TEST") != "1",
    reason=(
        "Polymarket paper integration tests require live WS connectivity. "
        "Set POLYMARKET_PAPER_TEST=1 to run."
    ),
)


@pytest.mark.integration()
@pytest.mark.asyncio()
async def test_paper_place_and_cancel() -> None:
    """Place a resting paper order, then cancel it and observe the CancelEvent.

    TODO(phase-4-integration): Implement once Polymarket WS connectivity is
    available in CI.  Steps:
      1. Load test config (POLYMARKET_TEST_CONFIG env var points to a YAML file).
      2. Create PolymarketAdapter(Mode.PAPER, config).
      3. async with adapter: ...
      4. Subscribe to a token_id from a live market.
      5. Wait for WS book to populate (timeout 10s).
      6. Place an order at 0.01 away from best_bid (resting).
      7. Verify result.accepted == True.
      8. Cancel the order.
      9. Collect events for 2s; assert CancelEvent in events.
    """
    pytest.skip(
        "TODO(phase-4-integration): requires live Polymarket WS. "
        "See module docstring for implementation plan."
    )


@pytest.mark.integration()
@pytest.mark.asyncio()
async def test_paper_taking_order_fills_immediately() -> None:
    """Place a taking paper order (crossing the spread) and observe immediate fill.

    TODO(phase-4-integration): Implement once WS connectivity is available.
    Steps:
      1. Fetch live book to determine best_ask.
      2. Place BUY at best_ask or higher.
      3. Verify FillEvent is emitted within 1s.
      4. Verify fill_price == best_ask at time of placement.
    """
    pytest.skip(
        "TODO(phase-4-integration): requires live Polymarket WS. "
        "See module docstring for implementation plan."
    )


@pytest.mark.integration()
@pytest.mark.asyncio()
async def test_paper_settlement_detection() -> None:
    """Verify settlement detection on a known resolved market.

    TODO(phase-4-integration): Implement with a historically resolved market.
    Steps:
      1. Use a condition_id that is known to be resolved (from Gamma).
      2. Call poll_settlement(condition_id, token_id_yes).
      3. Assert SettlementEvent is returned with payout in {0, 1}.
    """
    pytest.skip(
        "TODO(phase-4-integration): use a known resolved market condition_id. "
        "See module docstring for implementation plan."
    )
