"""The platform services host. Only the exchange may call it (shared gateway secret)."""

from __future__ import annotations

import secrets as pysecrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException

from . import database, sandbox, shield, storage
from .search import tiers


def create_app(data_dir: Path, gateway_secret: str) -> FastAPI:
    def require_gateway(x_pacific_gateway_secret: str = Header(default="")):
        if not pysecrets.compare_digest(x_pacific_gateway_secret, gateway_secret):
            raise HTTPException(401, "platform services are only reachable through the Pacific exchange")

    app = FastAPI(title="Pacific platform services", dependencies=[Depends(require_gateway)])
    for r in (tiers.brave, tiers.tavily, tiers.exa, shield.router, sandbox.router,
              database.router(data_dir), storage.router(data_dir)):
        app.include_router(r)
    return app
