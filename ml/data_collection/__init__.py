"""Data collection package for offline ML dataset building.

This package lives under the sibling ``ml/`` package and must never be
imported by ``src/`` (production runtime code) — see the top-level
``CLAUDE.md`` and ``plan/sidetracks/ml/README.md`` for the architectural
rationale.
"""

from __future__ import annotations
