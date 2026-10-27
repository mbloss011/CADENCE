"""OS-level isolation for shell commands (`bash` tool and goal verify commands).

* none   — run directly (previous behaviour)
* bwrap  — bubblewrap: read-only host root, writable workspace + tmpfs /tmp, $HOME hidden by default,
           optional network namespace. No daemon or image needed.
* docker — throwaway container with the workspace bind-mounted at the same path, as your uid.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import SandboxConfig

MODES = ("none", "bwrap", "docker")


@dataclass
class ShellResult:
    exit: int
    output: str
    timed_out: bool = False


class SandboxError(RuntimeError):
    """The configured sandbox cannot be used."""


class Sandbox:
    def __init__(self, cfg: SandboxConfig, workspace: Path):
        if cfg.mode not in MODES:
            raise SandboxError(f"sandbox.mode must be one of {MODES} (got {cfg.mode!r})")
        self.cfg = cfg
        self.workspace = Path(workspace).resolve()

    @property
    def mode(self) -> str:
        return self.cfg.mode

    def check(self) -> str:
        """Return '' if usable, else a human-readable reason."""
        if self.mode == "bwrap" and not shutil.which("bwrap"):
            return "bwrap not installed (apt install bubblewrap)"
        if self.mode == "docker":
            docker = shutil.which("docker")
            if not docker:
                return "docker not installed"
            if docker.startswith("/snap/") and str(self.workspace).startswith("/tmp/"):
                return "snap-packaged docker cannot see /tmp (it has a private /tmp); move the workspace or use bwrap"
        return ""

    def argv(self, command: str, name: str = "", ro: list[str] | None = None, env: dict[str, str] | None = None) -> list[str]:
        """`ro`: extra paths mounted read-only for this call only (e.g. a skill's folder under the hidden $HOME)."""
        ws = str(self.workspace)
        extra = [str(Path(p).expanduser().resolve()) for p in ro or []]
        if self.mode == "none":
            return ["bash", "-c", command]
        if self.mode == "bwrap":
            args = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
            if self.cfg.network:
                args.append("--share-net")
            args += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
            home = str(Path.home())
            if self.cfg.hide_home:
                args += ["--tmpfs", home]
            for p in self.cfg.ro_binds:
                p = str(Path(p).expanduser().resolve())
                args += ["--ro-bind-try", p, p]
            for p in extra:  # after the $HOME / /tmp tmpfs layers, so they show through them
                args += ["--ro-bind", p, p]
            args += ["--bind", ws, ws]
            for p in self.cfg.rw_binds:
                p = str(Path(p).expanduser().resolve())
                args += ["--bind-try", p, p]
            args += ["--chdir", ws, "--setenv", "HOME", home, "--", "bash", "-c", command]
            return args
        args = ["docker", "run", "--rm", "-i", "--init", "--name", name or f"cadence-sbx-{uuid.uuid4().hex[:10]}",
                "-v", f"{ws}:{ws}", "-w", ws, "-e", "HOME=/tmp", "--user", f"{os.getuid()}:{os.getgid()}"]
        if not self.cfg.network:
            args += ["--network", "none"]
        for p in [*(str(Path(x).expanduser().resolve()) for x in self.cfg.ro_binds), *extra]:
            args += ["-v", f"{p}:{p}:ro"]
        for k, v in (env or {}).items():  # docker does not inherit the caller's environment
            args += ["-e", f"{k}={v}"]
        for p in self.cfg.rw_binds:
            p = str(Path(p).expanduser().resolve())
            args += ["-v", f"{p}:{p}"]
        return args + [self.cfg.image, "bash", "-c", command]

    async def run(self, command: str, timeout: float, env: dict[str, str] | None = None, ro: list[str] | None = None) -> ShellResult:
        """Run `command`; kills the whole process group (and container) on timeout or cancellation."""
        reason = self.check()
        if reason:
            raise SandboxError(reason)
        name = f"cadence-sbx-{uuid.uuid4().hex[:10]}"
        proc = await asyncio.create_subprocess_exec(
            *self.argv(command, name, ro, env),
            cwd=self.workspace,
            env={**os.environ, **(env or {})},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._kill(proc, name)
            return ShellResult(-1, "", timed_out=True)
        except asyncio.CancelledError:
            await self._kill(proc, name)
            raise
        return ShellResult(proc.returncode if proc.returncode is not None else -1, out.decode("utf-8", "replace"))

    async def _kill(self, proc: asyncio.subprocess.Process, name: str) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if self.mode == "docker":  # the client dying does not stop the container
            rm = await asyncio.create_subprocess_exec("docker", "rm", "-f", name, stdout=asyncio.subprocess.DEVNULL,
                                                      stderr=asyncio.subprocess.DEVNULL)
            await rm.wait()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            pass
