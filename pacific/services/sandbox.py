"""ocean_sandbox: runs Python in a throwaway directory with CPU, memory and time limits.

This is resource-limited, not a security boundary: code runs as the local user with network
access. Fine for a simulation driven by our own agents; isolate it (container/VM) before
letting untrusted agents in.
"""

from __future__ import annotations

import asyncio
import os
import resource
import sys
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .common import ok

MAX_OUTPUT = 20_000
MAX_TIMEOUT = 30


def _limits(cpu_seconds: int):
    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024,) * 2)
        try:  # not enforced on macOS, but honoured on Linux
            resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024,) * 2)
        except (ValueError, OSError):
            pass

    return apply


router = APIRouter(prefix="/ocean_sandbox")


class Run(BaseModel):
    code: str = Field(min_length=1, max_length=200_000)
    timeout_s: float = 10
    stdin: str = ""
    files: dict[str, str] = Field(default_factory=dict, description="extra files to place next to main.py")


@router.post("/run")
async def run(req: Run):
    timeout = max(1.0, min(float(req.timeout_s), MAX_TIMEOUT))
    with tempfile.TemporaryDirectory(prefix="sandbox_") as tmp:
        root = Path(tmp)
        for rel, content in {**req.files, "main.py": req.code}.items():
            dest = root / rel.lstrip("/")
            if ".." in Path(rel).parts:
                raise HTTPException(400, f"bad path {rel!r}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        before = {p for p in root.rglob("*") if p.is_file()}
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "main.py",
            cwd=tmp,
            env={"PATH": "/usr/bin:/bin", "HOME": tmp, "PYTHONIOENCODING": "utf-8"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            preexec_fn=_limits(int(timeout) + 1),
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(req.stdin.encode()), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            out, err = await proc.communicate()
        duration = time.monotonic() - t0
        created = {
            str(p.relative_to(root)): p.read_text(errors="replace")[:MAX_OUTPUT]
            for p in sorted(root.rglob("*"))
            if p.is_file() and p not in before and p.stat().st_size <= 1024 * 1024
        }
    return ok(
        {
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "duration_s": round(duration, 3),
            "stdout": out.decode(errors="replace")[:MAX_OUTPUT],
            "stderr": err.decode(errors="replace")[:MAX_OUTPUT],
            "files_created": created,
        },
        units=max(0.1, round(duration, 1)),
    )
