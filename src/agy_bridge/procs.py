"""Run ordinary (non-agy) processes: git, test runners, shell commands."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agy import kill_tree


@dataclass
class ProcResult:
    code: int
    out: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


def _spawn_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["NO_COLOR"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"  # never hang waiting for a password
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    return env


async def _collect(proc: asyncio.subprocess.Process, timeout: float | None) -> ProcResult:
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await kill_tree(proc)
        return ProcResult(124, f"(timed out after {int(timeout or 0)}s)", timed_out=True)
    return ProcResult(proc.returncode or 0, out.decode("utf-8", errors="replace"))


async def run_argv(argv: list[str], cwd: Path, timeout: float | None = 60, stdin: bytes | None = None) -> ProcResult:
    """Run an argv list (no shell). stdout and stderr are merged."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_env(),
            **_spawn_kwargs(),
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
        return ProcResult(127, f"{argv[0]}: {exc}")
    if stdin is not None:
        try:
            out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
        except asyncio.TimeoutError:
            await kill_tree(proc)
            return ProcResult(124, f"(timed out after {int(timeout or 0)}s)", timed_out=True)
        return ProcResult(proc.returncode or 0, out.decode("utf-8", errors="replace"))
    return await _collect(proc, timeout)


def shell_argv(command: str, shell: str = "") -> list[str]:
    """argv that runs `command` through the user's shell.

    On Windows the default is PowerShell (what you'd type in Windows Terminal);
    SHELL_COMMAND=cmd switches to cmd.exe.
    """
    shell = (shell or "").strip().lower()
    if sys.platform == "win32":
        if shell in {"cmd", "cmd.exe"}:
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
        exe = "pwsh" if shell in {"pwsh", "pwsh.exe"} else "powershell"
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    exe = shell or os.environ.get("SHELL") or "/bin/sh"
    return [exe, "-c", command]


async def run_shell(command: str, cwd: Path, timeout: float | None = 300, shell: str = "") -> ProcResult:
    return await run_argv(shell_argv(command, shell), cwd, timeout=timeout)
