"""Drive the Antigravity CLI (`agy`) through its headless stream-json protocol.

Protocol (https://antigravity.google/docs/cli/headless):

* stdin  -- one JSON object per line: {"event": "user", "message": {"content": "..."}}
* stdout -- NDJSON events: one ``init``, then ``step_update`` events, and one
  ``result`` event per turn.

Prompts are always written to stdin, never passed on the command line, so no
shell quoting (cmd.exe or otherwise) can ever mangle or inject into them.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

STREAM_LIMIT = 64 * 1024 * 1024  # tool outputs inside one NDJSON line can be big
STDERR_TAIL = 200


class AgyNotFound(RuntimeError):
    pass


@dataclass
class AgyOptions:
    """Per-session knobs mapped onto agy command-line flags."""

    model: str = ""
    effort: str = ""
    mode: str = ""
    skip_permissions: bool = False
    sandbox: bool = False
    extra_args: list[str] = field(default_factory=list)

    def to_args(self) -> list[str]:
        args: list[str] = []
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        if self.mode:
            args += ["--mode", self.mode]
        if self.sandbox:
            args.append("--sandbox")
        if self.skip_permissions:
            args.append("--dangerously-skip-permissions")
        args += list(self.extra_args)
        return args


@dataclass
class AgyEvent:
    """A normalized event from agy's stdout (or synthesized by the runner)."""

    kind: str  # init | text | tool | step | result | error | stderr
    conversation_id: str = ""
    text: str = ""
    step_index: int = -1
    state: str = ""
    step_type: str = ""
    tool_name: str = ""
    tool_params: dict[str, Any] = field(default_factory=dict)
    tool_output: str = ""
    tool_error: str = ""
    status: str = ""
    response: str = ""
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    duration: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.kind == "result" and self.status == "SUCCESS"


def parse_line(line: str) -> AgyEvent | None:
    """Parse one stdout line. Non-JSON lines are returned as ``stderr``-ish noise."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return AgyEvent(kind="stderr", text=line)
    if not isinstance(obj, dict):
        return AgyEvent(kind="stderr", text=line)

    event = obj.get("event")
    if event == "init":
        return AgyEvent(kind="init", conversation_id=obj.get("conversation_id", ""), raw=obj)

    if event == "result":
        r = obj.get("result") or {}
        return AgyEvent(
            kind="result",
            conversation_id=r.get("conversation_id", ""),
            status=r.get("status", ""),
            response=r.get("response", "") or "",
            error=r.get("error", "") or "",
            usage=r.get("usage") or {},
            duration=float(r.get("duration_seconds") or 0.0),
            raw=obj,
        )

    if event == "step_update":
        s = obj.get("step_update") or {}
        step_type = s.get("step_type", "")
        base = dict(
            conversation_id=s.get("conversation_id", ""),
            step_index=int(s.get("step_index", -1)),
            state=s.get("state", ""),
            step_type=step_type,
            usage=s.get("usage") or {},
            duration=float(s.get("duration_seconds") or 0.0),
            raw=obj,
        )
        if step_type == "agent_response":
            return AgyEvent(kind="text", text=s.get("text_delta", "") or "", **base)
        if step_type == "tool" or s.get("tool_name") or s.get("tool_info"):
            info = s.get("tool_info") or {}
            err = info.get("error") or {}
            err_text = ""
            if isinstance(err, dict) and err:
                err_text = f"{err.get('type', 'error')}: {err.get('message', '')}".strip(": ")
            elif isinstance(err, str):
                err_text = err
            output = info.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            params = info.get("parameters") or {}
            if not isinstance(params, dict):
                params = {"value": params}
            return AgyEvent(
                kind="tool",
                tool_name=s.get("tool_name") or info.get("name", "") or "tool",
                tool_params=params,
                tool_output=output,
                tool_error=err_text,
                **base,
            )
        return AgyEvent(kind="step", text=s.get("text_delta", "") or "", **base)

    # Unknown event types are passed through so newer CLIs don't break us.
    return AgyEvent(kind="step", raw=obj, step_type=str(event or ""))


def resolve_command(cmd: list[str]) -> list[str]:
    """Resolve the agy executable, handling Windows .cmd/.bat shims."""
    if not cmd:
        raise AgyNotFound("AGY_COMMAND is empty")
    exe, rest = cmd[0], cmd[1:]
    found = shutil.which(exe)
    if found is None:
        p = Path(exe)
        if p.exists():
            found = str(p)
        else:
            raise AgyNotFound(
                f"Could not find {exe!r} on PATH. Install the Antigravity CLI, run `agy` once to sign in, "
                "or set AGY_COMMAND to the full path of agy.exe."
            )
    if sys.platform == "win32" and found.lower().endswith((".cmd", ".bat")):
        # Only flags (never user text) go through cmd.exe; prompts travel over stdin.
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", found, *rest]
    return [found, *rest]


async def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a process and its children (agy may spawn shells/tools)."""
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
    except ProcessLookupError:
        pass
    except Exception:  # pragma: no cover - best effort
        log.exception("kill_tree failed; falling back to proc.kill()")
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:  # pragma: no cover
        log.warning("agy pid %s did not exit after kill", proc.pid)


class TurnTimeout(Exception):
    pass


class AgySession:
    """One agy process speaking stream-json.

    ``persistent=True`` keeps the process alive across turns (fast: no startup
    cost, warm context). ``persistent=False`` starts a process per turn and
    closes stdin right after the prompt; conversation continuity then comes
    from ``--conversation <id>``.
    """

    def __init__(
        self,
        command: list[str],
        cwd: Path,
        options: AgyOptions | None = None,
        conversation_id: str | None = None,
        persistent: bool = True,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.cwd = Path(cwd)
        self.options = options or AgyOptions()
        self.conversation_id = conversation_id or ""
        self.persistent = persistent
        self.env = env
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL)
        self._turn_stderr: list[str] = []
        self._stderr_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.busy = False
        self.turns = 0

    # ------------------------------------------------------------------ process
    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def build_argv(self) -> list[str]:
        argv = resolve_command(self.command)
        argv += ["--input-format", "stream-json", "--output-format", "stream-json"]
        argv += self.options.to_args()
        if self.conversation_id:
            argv += ["--conversation", self.conversation_id]
        return argv

    async def start(self) -> None:
        if self.alive:
            return
        argv = self.build_argv()
        log.info("starting agy: %s (cwd=%s)", argv, self.cwd)
        env = dict(os.environ if self.env is None else self.env)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("NO_COLOR", "1")
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            kwargs["start_new_session"] = True
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=STREAM_LIMIT,
            **kwargs,
        )
        self._stderr_task = asyncio.create_task(self._pump_stderr(self.proc))

    async def _pump_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            try:
                raw = await proc.stderr.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self.stderr_tail.append(line)
                self._turn_stderr.append(line)
                log.debug("agy stderr: %s", line)

    async def close(self, graceful: bool = True) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.returncode is None and graceful and proc.stdin is not None:
            try:
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError, OSError):
                pass
        if proc.returncode is None:
            await kill_tree(proc)
        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None
        self.busy = False

    async def interrupt(self) -> None:
        """Hard-stop the current turn. The conversation id survives, so the next
        turn resumes the same thread with ``--conversation``."""
        await self.close(graceful=False)

    # --------------------------------------------------------------------- turn
    async def ask(self, prompt: str, timeout: float | None = None) -> AsyncIterator[AgyEvent]:
        """Send a prompt and yield events until this turn's ``result``.

        Always ends with exactly one ``result`` or ``error`` event.
        """
        async with self._lock:
            self.busy = True
            self._turn_stderr = []
            try:
                async for ev in self._ask(prompt, timeout):
                    yield ev
            finally:
                self.busy = False
                self.turns += 1
                if not self.persistent:
                    await self.close(graceful=True)

    def turn_stderr(self) -> list[str]:
        return list(self._turn_stderr)

    async def _ask(self, prompt: str, timeout: float | None) -> AsyncIterator[AgyEvent]:
        try:
            await self.start()
        except AgyNotFound as exc:
            yield AgyEvent(kind="error", error=str(exc))
            return
        except OSError as exc:
            yield AgyEvent(kind="error", error=f"Failed to start agy: {exc}")
            return

        proc = self.proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None
        message = {"event": "user", "message": {"content": prompt}}
        try:
            proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            if not self.persistent:
                proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            await self.close(graceful=False)
            yield AgyEvent(kind="error", error=f"agy exited before accepting the prompt ({exc}). {self._stderr_hint()}")
            return

        loop = asyncio.get_running_loop()
        deadline = None if not timeout else loop.time() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - loop.time())
            try:
                if remaining is not None and remaining <= 0:
                    raise asyncio.TimeoutError
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                await self.interrupt()
                yield AgyEvent(kind="error", status="TIMEOUT", error=f"Turn timed out after {int(timeout or 0)}s; agy was stopped.")
                return
            except (ValueError, asyncio.LimitOverrunError) as exc:
                log.warning("oversized line from agy skipped: %s", exc)
                continue

            if not raw:
                # EOF: process ended without a result for this turn.
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(0.05)  # let stderr pump drain
                code = proc.returncode
                await self.close(graceful=False)
                yield AgyEvent(kind="error", error=f"agy exited (code {code}) without a result. {self._stderr_hint()}")
                return

            ev = parse_line(raw.decode("utf-8", errors="replace"))
            if ev is None:
                continue
            if ev.conversation_id:
                self.conversation_id = ev.conversation_id
            yield ev
            if ev.kind == "result":
                return

    def _stderr_hint(self) -> str:
        tail = [l for l in self.stderr_tail if l.strip()][-8:]
        return ("stderr:\n" + "\n".join(tail)) if tail else ""


async def run_simple(command: list[str], args: list[str], cwd: Path, timeout: float = 120) -> tuple[int, str]:
    """Run a one-off agy command such as ``agy models`` or ``agy -p /usage``.

    Arguments here are fixed strings chosen by the bot, never user text.
    """
    argv = resolve_command(command) + args
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await kill_tree(proc)
        return 124, "timed out"
    return proc.returncode or 0, out.decode("utf-8", errors="replace")
