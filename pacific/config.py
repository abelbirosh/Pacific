"""Runtime configuration. Everything lives under PACIFIC_HOME (default ./.pacific)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    home: Path
    exchange_port: int
    services_port: int
    agents_port: int
    host: str = "127.0.0.1"

    @property
    def ledger_path(self) -> Path:
        return self.home / "ledger.sqlite"

    @property
    def data_dir(self) -> Path:
        """Where platform services keep real state (databases, stored files)."""
        return self.home / "data"

    @property
    def secrets_path(self) -> Path:
        return self.home / "secrets.json"

    @property
    def exchange_url(self) -> str:
        return f"http://{self.host}:{self.exchange_port}"

    @property
    def services_url(self) -> str:
        return f"http://{self.host}:{self.services_port}"

    @property
    def agents_url(self) -> str:
        return f"http://{self.host}:{self.agents_port}"

    def secrets(self) -> dict:
        """Keys written at genesis: the admin key and every account's API key. (The exchange's
        grant-signing key pair sits next to this file; see grants.py.)"""
        if not self.secrets_path.exists():
            raise RuntimeError(f"No economy found at {self.home}. Run `pacific init` first.")
        return json.loads(self.secrets_path.read_text())


def load_config() -> Config:
    return Config(
        home=Path(os.environ.get("PACIFIC_HOME", ".pacific")).resolve(),
        exchange_port=int(os.environ.get("PACIFIC_EXCHANGE_PORT", 8700)),
        services_port=int(os.environ.get("PACIFIC_SERVICES_PORT", 8701)),
        agents_port=int(os.environ.get("PACIFIC_AGENTS_PORT", 8702)),
    )
