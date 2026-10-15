"""The platform services host. Only the exchange may call it: every request needs a grant the
exchange signed for that exact call (see grants.py)."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI

from .. import faults
from ..grants import Verifier, require_grant
from . import database, sandbox, shield, storage
from .search import tiers


def create_app(data_dir: Path, grants: Verifier | None, faults_file: Path | None = None) -> FastAPI:
    app = FastAPI(title="Pacific platform services", dependencies=[Depends(require_grant)])
    app.state.grants = grants
    for r in (tiers.brave, tiers.tavily, tiers.exa, shield.router, sandbox.router,
              database.router(data_dir), storage.router(data_dir)):
        app.include_router(r)
    if faults_file is not None:
        faults.install(app, faults_file)
    return app
