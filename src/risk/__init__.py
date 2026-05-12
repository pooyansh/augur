"""Risk control layer — caps, kill switch, audit, and withdrawal allowlist.

Public API surface:

    from src.risk import (
        AuditLogger,
        AuditRecord,
        KillSwitchCascade,
        KillSwitchReader,
        KillSwitchWriter,
        RiskCapExceeded,
        RiskCaps,
        WithdrawalAllowlist,
        check_caps,
    )

The underlying DB classes remain in ``src.state.repository``; this package
re-exports them under the canonical ``src.risk`` namespace.
"""

from __future__ import annotations

from src.risk.audit import AuditLogger, AuditRecord
from src.risk.caps import RiskCapExceeded, RiskCaps, check_caps
from src.risk.kill_switch import KillSwitchCascade, KillSwitchReader, KillSwitchWriter
from src.risk.withdrawal_allowlist import WithdrawalAllowlist

__all__ = [
    "AuditLogger",
    "AuditRecord",
    "KillSwitchCascade",
    "KillSwitchReader",
    "KillSwitchWriter",
    "RiskCapExceeded",
    "RiskCaps",
    "WithdrawalAllowlist",
    "check_caps",
]
