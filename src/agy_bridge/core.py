"""Chat workers: one queue + one agy session per Telegram chat (or forum topic).

This module knows nothing about Telegram; it talks to a ``Messenger`` so the
whole turn pipeline can be tested with a fake messenger and a fake agy.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from . import gitops
from .agy import AgyOptions, AgySession
from .config import Config
from .render import (
    TELEGRAM_LIMIT,
    format_usage,
    render_chunks,
    status_html,
    tool_summary,
)
from .state import StateStore
from .transcript import Transcript

log = logging.getLogger(__name__)

PERMISSION_HINT_RE = re.compile(
    r"permission|denied|not allowed|soft-den|permissions\.allow|dangerously-skip", re.IGNORECASE
)
RULE_RE = re.compile(r"\b(?:command|read|write|edit|mcp|tool|url|web)\([^()\s][^()]{0,120}\)")
MAX_INLINE_CHUNKS = 5

Buttons = list[list[tuple[str, str]]]  # rows of (label, callback data)


class Messenger(Protocol):
    async def send(self, html: str, plain: str | None = None, *, buttons: Buttons | None = None, silent: bool = False) -> Any: ...
    async def edit(self, handle: Any, html: str, *, buttons: Buttons | None = None) -> None: ...
    async def delete(self, handle: Any) -> None: ...
    async def send_document(self, data: bytes, filename: str, caption: str = "") -> None: ...
    async def typing(self) -> None: ...


@dataclass
class Job:
    prompt: str
    overrides: dict[str, Any] = field(default_factory=dict)  # AgyOptions fields for this turn only
    label: str = ""  # short description shown in the status header (e.g. "/review")
    display: str = ""  # what to log as the user's prompt (defaults to prompt)


@dataclass
class TurnRecord:
    """What the last turn did, for the Retry / Diff / Undo buttons."""

    job: Job
    checkpoint: gitops.Checkpoint | None = None
    changes: gitops.ChangeSummary | None = None
    denied_rules: list[str] = field(default_factory=list)
    undone: bool = False


class ChatWorker:
    def __init__(self, chat_id: int, config: Config, store: StateStore, messenger: Messenger, key: str | None = None) -> None:
        self.chat_id = chat_id
        self.key = key or str(chat_id)
        self.config = config
        self.store = store
        self.messenger = messenger
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.session: AgySession | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._restart_pending = False
        self.last_active = time.monotonic()
        self.current_prompt: str = ""
        self.current_label: str = ""
        self.turn_started: float = 0.0
        self.last_turn: TurnRecord | None = None
        self.actions: dict[str, tuple[str, Any]] = {}  # callback token -> (action, payload)
        self._tokens = itertools.count(1)
        self.transcript = Transcript(config.transcript_dir, self.key)

    # ----------------------------------------------------------------- settings
    @property
    def state(self):
        return self.store.get(self.key)

    @property
    def cwd(self) -> Path:
        return Path(self.state.cwd) if self.state.cwd else self.config.workdir

    @property
    def yolo(self) -> bool:
        return self.config.skip_permissions if self.state.yolo is None else bool(self.state.yolo)

    def options(self, overrides: dict[str, Any] | None = None) -> AgyOptions:
        s = self.state
        opts = AgyOptions(
            model=s.model or self.config.model,
            effort=s.effort or self.config.effort,
            mode=s.mode if s.mode != "" else self.config.mode,
            skip_permissions=self.yolo,
            sandbox=self.config.sandbox,
            extra_args=list(self.config.extra_args),
        )
        return replace(opts, **overrides) if overrides else opts

    @property
    def busy(self) -> bool:
        return bool(self.session and self.session.busy)

    def action_token(self, action: str, payload: Any = None) -> str:
        """Register a button action; returns callback data (<= 64 bytes)."""
        tok = f"{next(self._tokens):x}"
        self.actions[tok] = (action, payload)
        if len(self.actions) > 200:  # forget the oldest buttons
            for old in list(self.actions)[:50]:
                self.actions.pop(old, None)
        return f"a:{tok}"

    # ---------------------------------------------------------------- lifecycle
    def ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name=f"chat-{self.key}")

    async def submit(self, prompt: str | Job, **kw) -> int:
        """Queue a prompt. Returns how many prompts are ahead of it."""
        job = prompt if isinstance(prompt, Job) else Job(prompt=prompt, **kw)
        ahead = self.queue.qsize() + (1 if self.busy else 0)
        await self.queue.put(job)
        self.ensure_running()
        return ahead

    async def stop_turn(self, clear_queue: bool = True) -> tuple[bool, int]:
        """Interrupt the running turn. Returns (was_running, dropped_queued)."""
        dropped = 0
        if clear_queue:
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                    dropped += 1
                except asyncio.QueueEmpty:
                    break
        was_running = self.busy
        if self.session and was_running:
            self._stopping = True
            await self.session.interrupt()
        return was_running, dropped

    async def invalidate(self) -> None:
        """Settings changed: restart agy before the next turn (conversation kept)."""
        if self.busy:
            self._restart_pending = True
        elif self.session:
            await self.session.close()
            self.session = None

    async def reset(self) -> None:
        """Forget the conversation and start fresh on the next message."""
        await self.stop_turn(clear_queue=True)
        if self.session:
            await self.session.close()
            self.session = None
        self.state.conversation_id = ""
        self.state.conversations.pop(str(self.cwd), None)
        self.last_turn = None
        self.store.save()

    async def switch_workspace(self, target: Path) -> str:
        """Move to another folder, resuming that folder's last conversation.

        Returns the conversation id that will be resumed ("" for a new one).
        """
        await self.stop_turn(clear_queue=True)
        if self.session:
            await self.session.close()
            self.session = None
        s = self.state
        if s.conversation_id:
            s.conversations[str(self.cwd)] = s.conversation_id
        s.remember_project(str(self.cwd))
        s.cwd = str(target)
        s.conversation_id = s.conversations.get(str(target), "")
        s.remember_project(str(target))
        self.last_turn = None
        self.store.save()
        return s.conversation_id

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
        if self.session:
            await self.session.close()
            self.session = None

    def _make_session(self, options: AgyOptions) -> AgySession:
        return AgySession(
            command=self.config.agy_command,
            cwd=self.cwd,
            options=options,
            conversation_id=self.state.conversation_id or None,
            persistent=self.config.run_mode == "persistent",
        )

    async def _loop(self) -> None:
        while True:
            try:
                timeout = self.config.idle_shutdown if self.config.idle_shutdown > 0 else None
                job = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if self.session and self.session.alive:
                    log.info("chat %s idle; closing agy process", self.key)
                    await self.session.close()
                continue
            except asyncio.CancelledError:
                return
            try:
                await self._run_turn(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one bad turn kill the worker
                log.exception("turn failed in chat %s", self.key)
                try:
                    await self.messenger.send(f"❌ Bridge error: <code>{_esc(repr(exc))}</code>")
                except Exception:
                    pass
            finally:
                self.queue.task_done()
                self.last_active = time.monotonic()

    # --------------------------------------------------------------------- turn
    async def _run_turn(self, job: Job) -> None:
        options = self.options(job.overrides)
        if self.session and (self._restart_pending or self.session.options != options or self.session.cwd != self.cwd):
            await self.session.close()
            self.session = None
        self._restart_pending = False
        if self.session is None:
            self.session = self._make_session(options)
        session = self.session
        self._stopping = False
        self.current_prompt = job.display or job.prompt
        self.current_label = job.label
        self.turn_started = time.monotonic()

        record = TurnRecord(job=job)
        if self.config.checkpoints:
            try:
                record.checkpoint = await gitops.checkpoint(self.cwd)
            except Exception:
                log.exception("checkpoint failed")
        self.transcript.add("user", job.display or job.prompt, cwd=str(self.cwd), conversation=self.state.conversation_id)

        presenter = TurnPresenter(self, label=job.label)
        await presenter.begin()
        typing_task = asyncio.create_task(_keep_typing(self.messenger))
        result = None
        error = None
        try:
            async for ev in session.ask(job.prompt, timeout=self.config.turn_timeout or None):
                if ev.kind == "text":
                    presenter.on_text(ev.text)
                elif ev.kind == "tool":
                    presenter.on_tool(ev)
                elif ev.kind == "result":
                    result = ev
                elif ev.kind == "error":
                    error = ev
                await presenter.maybe_refresh()
        finally:
            typing_task.cancel()
            self.current_prompt = ""
            self.current_label = ""

        if session.conversation_id and session.conversation_id != self.state.conversation_id:
            self.state.conversation_id = session.conversation_id
            self.state.remember_conversation(session.conversation_id)
        if self.state.conversation_id:
            self.state.conversations[str(self.cwd)] = self.state.conversation_id
        self.store.save()

        await asyncio.sleep(0.15)  # let the stderr pump catch up with stdout
        notices = [l for l in session.turn_stderr() if PERMISSION_HINT_RE.search(l)]
        record.denied_rules = suggest_rules(notices, presenter.denied_tools)
        if record.checkpoint is not None:
            try:
                record.changes = await gitops.changes_since(record.checkpoint)
            except Exception:
                log.exception("change summary failed")
        self.last_turn = record

        if self._stopping:
            self._stopping = False
            await presenter.finish_stopped(record)
            self.transcript.add("agy", "(stopped)", conversation=self.state.conversation_id)
            return
        if result is not None:
            await presenter.finish_result(result, notices, record)
            self.transcript.add("agy", result.response or "", conversation=self.state.conversation_id, status=result.status)
        else:
            message = error.error if error else "agy ended unexpectedly"
            await presenter.finish_error(message, notices, record)
            self.transcript.add("agy", f"(error) {message}", conversation=self.state.conversation_id)

    # ------------------------------------------------------------------ buttons
    def reply_buttons(self, record: TurnRecord) -> Buttons:
        row: list[tuple[str, str]] = []
        if record.changes and not record.changes.empty:
            row.append(("📄 Diff", self.action_token("diff")))
            if not record.changes.head_moved:
                row.append(("↩️ Undo", self.action_token("undo")))
        row.append(("🔁 Retry", self.action_token("retry")))
        return [row]

    def approval_buttons(self, record: TurnRecord) -> Buttons:
        rows: Buttons = []
        if self.config.approval_buttons and not self.yolo:
            rows.append([("✅ Retry with full access (this turn only)", self.action_token("approve_once"))])
        for rule in record.denied_rules[:3]:
            rows.append([(f"📌 Always allow {rule}"[:60], self.action_token("allow_rule", rule))])
        return rows


def suggest_rules(notices: list[str], denied_tools: list[tuple[str, dict]]) -> list[str]:
    """Permission rules worth offering, from agy's notices and denied tool calls."""
    rules: list[str] = []
    for line in notices:
        for m in RULE_RE.finditer(line):
            if m.group(0) not in rules:
                rules.append(m.group(0))
    for name, params in denied_tools:
        if name == "run_command":
            cmd = ""
            for k in ("CommandLine", "command", "Command", "cmd"):
                if isinstance(params.get(k), str):
                    cmd = params[k].strip()
                    break
            first = cmd.split()[0] if cmd.split() else ""
            first = first.strip("\"'&")
            if first and re.fullmatch(r"[\w.\-\\/:]+", first):
                exe = re.split(r"[\\/]", first)[-1]
                exe = re.sub(r"\.(exe|cmd|bat|ps1)$", "", exe, flags=re.IGNORECASE)
                rule = f"command({exe})"
                if rule not in rules:
                    rules.append(rule)
    return rules


async def _keep_typing(messenger: Messenger) -> None:
    try:
        while True:
            try:
                await messenger.typing()
            except Exception:
                pass
            await asyncio.sleep(4.5)
    except asyncio.CancelledError:
        return


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TurnPresenter:
    """Shows a live status message during a turn, then the final answer."""

    def __init__(self, worker: ChatWorker, label: str = "") -> None:
        self.w = worker
        self.m = worker.messenger
        self.config = worker.config
        self.label = label
        self.status_handle: Any = None
        self.tools: dict[int, str] = {}
        self.tool_order: list[int] = []
        self.denied_tools: list[tuple[str, dict]] = []
        self.preview = ""
        self.started = time.monotonic()
        self._last_edit = 0.0
        self._last_rendered = ""
        self._dirty = False

    def tool_lines(self) -> list[str]:
        return [self.tools[i] for i in self.tool_order]

    def _header(self, text: str) -> str:
        return f"{text} · {self.label}" if self.label else text

    def _stop_button(self) -> Buttons:
        return [[("⏹ Stop", self.w.action_token("stop"))]]

    async def begin(self) -> None:
        self._last_rendered = status_html(self._header("⏳ Working…"), [])
        self.status_handle = await self.m.send(self._last_rendered, buttons=self._stop_button(), silent=True)
        self._last_edit = time.monotonic()

    def on_text(self, delta: str) -> None:
        if delta:
            self.preview += delta
            self._dirty = True

    def on_tool(self, ev) -> None:
        if ev.tool_error and PERMISSION_HINT_RE.search(ev.tool_error):
            self.denied_tools.append((ev.tool_name, ev.tool_params))
        if not self.config.show_tool_calls:
            return
        idx = ev.step_index if ev.step_index >= 0 else -(len(self.tool_order) + 1)
        line = tool_summary(ev.tool_name, ev.tool_params)
        if ev.tool_error:
            line = "❌ " + line
        if idx not in self.tools:
            self.tool_order.append(idx)
        if self.tools.get(idx) != line:
            self.tools[idx] = line
            self._dirty = True

    def _elapsed(self) -> str:
        secs = int(time.monotonic() - self.started)
        return f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"

    async def maybe_refresh(self, force: bool = False) -> None:
        if not self.status_handle or not (self._dirty or force):
            return
        now = time.monotonic()
        if not force and now - self._last_edit < self.config.edit_interval:
            return
        rendered = status_html(self._header(f"⏳ Working… {self._elapsed()}"), self.tool_lines(), self.preview)
        if rendered != self._last_rendered:
            await self.m.edit(self.status_handle, rendered, buttons=self._stop_button())
            self._last_rendered = rendered
        self._last_edit = now
        self._dirty = False

    async def _close_status(self, header: str) -> None:
        if not self.status_handle:
            return
        if self.tool_order:
            await self.m.edit(self.status_handle, status_html(header, self.tool_lines()), buttons=None)
        else:
            await self.m.delete(self.status_handle)

    def _change_line(self, record: TurnRecord) -> str:
        if record.changes and not record.changes.empty:
            ch = record.changes
            files = ", ".join(p for _, p in ch.files[:6]) + (f" +{len(ch.files) - 6} more" if len(ch.files) > 6 else "")
            return f"\n\n<i>{_esc(ch.headline())}: {_esc(files)}</i>"
        return ""

    async def finish_result(self, result, notices: list[str], record: TurnRecord) -> None:
        ok = result.status == "SUCCESS"
        usage = format_usage(result.usage)
        steps = len(self.tool_order)
        bits = [f"{'✅ Done' if ok else '⚠️ ' + (result.status or 'ERROR')}", self._elapsed()]
        if steps:
            bits.append(f"{steps} step{'s' if steps != 1 else ''}")
        if usage:
            bits.append(usage)
        await self._close_status(" · ".join(bits))

        text = result.response or ""
        if not ok and result.error:
            text = (text + "\n\n" if text else "") + f"**agy reported {result.status}:** {result.error}"
        if not text.strip():
            text = "_(agy finished without a text reply)_"
        await self.send_markdown(text, footer_html=self._change_line(record), buttons=self.w.reply_buttons(record))
        if notices or record.denied_rules:
            await self.send_notices(notices, record)

    async def finish_error(self, message: str, notices: list[str], record: TurnRecord) -> None:
        await self._close_status(f"❌ Failed · {self._elapsed()}")
        body = f"❌ <b>agy error</b>\n<pre>{_esc(message[-3000:])}</pre>" + self._change_line(record)
        await self.m.send(body, f"agy error\n{message[-3000:]}", buttons=self.w.reply_buttons(record))
        if notices or record.denied_rules:
            await self.send_notices(notices, record)

    async def finish_stopped(self, record: TurnRecord) -> None:
        await self._close_status(f"⏹ Stopped · {self._elapsed()}")
        extra = self._change_line(record)
        if not self.tool_order or extra:
            await self.m.send(
                "⏹ Stopped. The conversation is kept; just send your next message." + extra,
                buttons=self.w.reply_buttons(record) if extra else None,
            )

    async def send_markdown(self, text: str, footer_html: str = "", buttons: Buttons | None = None) -> None:
        chunks = render_chunks(text)
        if len(chunks) > MAX_INLINE_CHUNKS:
            first_html, first_plain = chunks[0]
            await self.m.send(first_html, first_plain)
            await self.m.send_document(
                text.encode("utf-8"),
                "agy-response.md",
                caption=f"Full reply ({len(text):,} chars, {len(chunks)} parts) attached.",
            )
            await self.m.send("⬆️ Full reply attached." + footer_html, buttons=buttons)
            return
        for i, (html_text, plain) in enumerate(chunks):
            last = i == len(chunks) - 1
            if len(html_text) > TELEGRAM_LIMIT:
                html_text = _esc(plain)[: TELEGRAM_LIMIT - 1]
            if last and footer_html and len(html_text) + len(footer_html) <= TELEGRAM_LIMIT:
                html_text += footer_html
                footer_html = ""
            await self.m.send(html_text, plain[: TELEGRAM_LIMIT - 1], buttons=buttons if last and not footer_html else None)
        if footer_html:
            await self.m.send(footer_html.strip(), buttons=buttons)

    async def send_notices(self, notices: list[str], record: TurnRecord) -> None:
        shown = "\n".join(_esc(n[:300]) for n in notices[-5:]) or "(a tool call was denied)"
        await self.m.send(
            "⚠️ <b>agy was blocked from using a tool</b>\n"
            f"<pre>{shown}</pre>\n"
            "Headless agy can't ask you, so it skipped it. Tap below to approve, "
            "or use /allow &lt;rule&gt; to always allow it.",
            buttons=self.w.approval_buttons(record) or None,
        )
