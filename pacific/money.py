"""Wave dollars (WD). Stored as integer micro-waves so the ledger never drifts."""

from __future__ import annotations

import math
from decimal import Decimal

MICRO = 1_000_000


def to_micro(wd: float | int | str | Decimal) -> int:
    return int((Decimal(str(wd)) * MICRO).to_integral_value())


def to_wd(micro: int) -> float:
    return micro / MICRO


def ceil_micro(wd: float) -> int:
    """Round a computed price up to the nearest micro-wave (never undercharge by rounding)."""
    return math.ceil(round(wd * MICRO, 6))


def fmt(micro: int) -> str:
    return f"{Decimal(micro) / MICRO:,.4f} WD"
