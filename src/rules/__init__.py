"""Provisional winning rules — optional, per-market/per-strategy continuation heuristics.

See ``.claude/rules/10-winning-rules.md`` for the full design contract.  A
``WinningRule`` never feeds P&L accounting or real settlement — it is purely
informational, letting a strategy decide whether to keep playing a window
while it waits for the exchange's own authoritative outcome.
"""

from __future__ import annotations
