"""How ocean_sandbox contains the code it runs.

Agents submit arbitrary Python, so the sandbox must not let it read host files (the exchange's
keys live there), reach the network (the services and exchange listen on localhost), or start
other programs. Each run gets a fresh directory and nothing else:

seatbelt  macOS `sandbox-exec`: deny by default; read only the Python runtime, read/write only
          the job directory; no network, no exec of anything but the interpreter.
bwrap     Linux bubblewrap: new user, network, PID, IPC and mount namespaces; the Python
          runtime and system libraries mounted read-only, the job directory at /work.
unsafe    a plain subprocess as the server's user, with host file and network access. Only for
          trusted code, and only when asked for by name.

PACIFIC_SANDBOX picks one (default `auto`: seatbelt on macOS, bwrap on Linux). With no usable
backend the sandbox refuses to run code rather than running it unisolated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PYTHON = os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)
PYTHON_HOME = os.path.realpath(sys.base_prefix)


@dataclass(frozen=True)
class Backend:
    name: str

    def command(self, workdir: Path) -> tuple[list[str], str]:
        """argv that runs workdir/main.py contained, and the directory to start it in."""
        raise NotImplementedError


class Seatbelt(Backend):
    def command(self, workdir: Path) -> tuple[list[str], str]:
        work = os.path.realpath(workdir)
        profile = f"""(version 1)
(deny default)
(allow process-fork)
(allow process-exec (literal "{PYTHON}"))
(allow file-read* (literal "/") (subpath "{PYTHON_HOME}") (subpath "/usr/lib") (subpath "/System/Library")
  (subpath "/private/var/db/dyld") (subpath "/System/Volumes/Preboot/Cryptexes")
  (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random") (literal "/dev/zero"))
(allow file-read* file-write* (subpath "{work}"))
(allow file-write-data (literal "/dev/null"))
(allow sysctl-read)
"""
        return ["sandbox-exec", "-p", profile, PYTHON, "-I", "main.py"], work


class Bubblewrap(Backend):
    def command(self, workdir: Path) -> tuple[list[str], str]:
        argv = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
                "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/work", "--setenv", "PYTHONIOENCODING", "utf-8"]
        for path in ("/usr", "/lib", "/lib64", "/bin", PYTHON_HOME):
            argv += ["--ro-bind-try", path, path]
        argv += ["--proc", "/proc", "--dev", "/dev", "--bind", str(workdir), "/work", "--chdir", "/work",
                 PYTHON, "-I", "main.py"]
        return argv, str(workdir)


class Unsafe(Backend):
    def command(self, workdir: Path) -> tuple[list[str], str]:
        return [PYTHON, "-I", "main.py"], str(workdir)


BACKENDS = {"seatbelt": Seatbelt("seatbelt"), "bwrap": Bubblewrap("bwrap"), "unsafe": Unsafe("unsafe")}
_checked: dict[str, str | None] = {}


def _works(backend: Backend) -> str | None:
    """Run a trivial program through the backend once; returns why it can't be used, or None."""
    if backend.name not in _checked:
        with tempfile.TemporaryDirectory(prefix="sandbox_check_") as tmp:
            Path(tmp, "main.py").write_text("print('ok')\n")
            argv, cwd = backend.command(Path(tmp))
            try:
                r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=20,
                                   env={"PATH": "/usr/bin:/bin", "HOME": tmp})
                _checked[backend.name] = None if r.stdout.strip() == "ok" else f"self-test failed: {r.stderr[-200:]}"
            except (OSError, subprocess.SubprocessError) as e:
                _checked[backend.name] = f"self-test failed: {type(e).__name__}: {e}"
    return _checked[backend.name]


def select() -> tuple[Backend | None, str]:
    """The backend to use, or None and the reason code can't be run safely here."""
    mode = os.environ.get("PACIFIC_SANDBOX", "auto")
    if mode == "auto":
        if sys.platform == "darwin" and shutil.which("sandbox-exec"):
            mode = "seatbelt"
        elif sys.platform.startswith("linux") and shutil.which("bwrap"):
            mode = "bwrap"
        else:
            return None, ("no isolation available (needs sandbox-exec on macOS or bwrap on Linux); "
                          "set PACIFIC_SANDBOX=unsafe to run code unisolated")
    backend = BACKENDS.get(mode)
    if backend is None:
        return None, f"unknown PACIFIC_SANDBOX {mode!r}; use auto, seatbelt, bwrap or unsafe"
    problem = None if mode == "unsafe" else _works(backend)
    return (None, f"{mode}: {problem}") if problem else (backend, "")
