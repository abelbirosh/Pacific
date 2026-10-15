"""Builds the three apps and runs them, in the foreground (`pacific serve`) or on a background
thread (tests and `pacific sim --boot`)."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI

from .config import Config
from .faults import faults_path
from .grants import Verifier


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_apps(config: Config) -> list[tuple[str, FastAPI, int]]:
    from .agents.host import create_app as agents_app
    from .exchange.app import create_app as exchange_app
    from .services.app import create_app as services_app

    return [
        ("exchange", exchange_app(config), config.exchange_port),
        ("services", services_app(config.data_dir, Verifier.load(config.home), faults_path(config.home)),
         config.services_port),
        ("agents", agents_app(config), config.agents_port),
    ]


def servers(config: Config, log_level: str = "warning") -> list[uvicorn.Server]:
    return [uvicorn.Server(uvicorn.Config(app, host=config.host, port=port, log_level=log_level))
            for _, app, port in build_apps(config)]


@contextmanager
def running(config: Config, log_level: str = "error", startup_timeout: float = 15):
    """Serve the economy on a daemon thread for the duration of the block."""
    group = servers(config, log_level)

    async def serve_all():
        await asyncio.gather(*(s.serve() for s in group))

    thread = threading.Thread(target=lambda: asyncio.run(serve_all()), daemon=True)
    thread.start()
    deadline = time.time() + startup_timeout
    while not all(s.started for s in group):
        if time.time() > deadline or not thread.is_alive():
            raise RuntimeError("servers did not start")
        time.sleep(0.05)
    try:
        yield
    finally:
        for s in group:
            s.should_exit = True
        thread.join(timeout=10)
