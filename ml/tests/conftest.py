"""Make the repo-root-relative ``ml.data_collection`` import path resolve
regardless of whether pytest is invoked from the repo root or from ``ml/``
(e.g. via ``cd ml && uv run pytest``).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
