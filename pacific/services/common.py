"""Shared plumbing for platform services.

Every endpoint returns {"result": ..., "usage": {"units": n}}. The exchange reads `units` to
bill the caller according to the endpoint's price plan, so services never touch money.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import Depends, HTTPException

from ..grants import require_grant

_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def ok(result, units: float = 0.0) -> dict:
    return {"result": result, "usage": {"units": round(float(units), 4)}}


def workspace(grant: dict = Depends(require_grant)) -> str:
    """The workspace the exchange's grant names: a caller can't choose it."""
    ws = grant.get("ws")
    if not isinstance(ws, str) or not _SAFE.match(ws):
        raise HTTPException(400, "bad workspace id")
    return ws


def safe_name(name: str, what: str = "name") -> str:
    if not isinstance(name, str) or not _SAFE.match(name):
        raise HTTPException(400, f"invalid {what} {name!r}: use letters, digits, _ . - (max 128)")
    return name


def workspace_dir(root: Path, kind: str, ws: str) -> Path:
    d = root / kind / safe_name(ws, "workspace")
    d.mkdir(parents=True, exist_ok=True)
    return d
