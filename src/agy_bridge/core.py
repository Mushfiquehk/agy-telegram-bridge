"""Chat workers: one queue + one agy session per Telegram chat.

This module knows nothing about Telegram; it talks to a ``Messenger`` so the
whole turn pipeline can be tested with a fake messenger and a fake agy.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Protocol

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

log = logging.getLogger(__name__)

PERMISSION_HINT_RE = re.compile(
    r"permission|denied|not allowed|soft-den|permissions\.allow|dangerously-skip", re.IGNORECASE
)
MAX_INLINE_CHUNKS = 5


class Messenger(Protocol):
    async def send(self, html: str, plain: str | None = None) -> Any: ...
    async def edit(self, handle: Any, html: str) -> None: ...
    async def delete(self, handle: Any) -> None: ...
    async def send_document(self, data: bytes, filename: str, caption: str = "") -> None: ...
    async def typing(self) -> None: ...


class ChatWorker:
    def __init__(self, chat_id: int, config: Config, store: StateStore, messenger: Messenger) -> None:
        self.chat_id = chat_id
        self.config = config
        self.store = store
        self.messenger = messenger
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.session: AgySession | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._restart_pending = False
        self.last_active = time.monotonic()
        self.current_prompt: str = ""
        self.turn_started: float = 0.0

    # ----------------------------------------------------------------- settings
    @property
    def state(self):
        return self.store.get(self.chat_id)

    @property
    def cwd(self) -> Path:
        return Path(self.state.cwd) if self.state.cwd else self.config.workdir

    @property
    def yolo(self) -> bool:
        return self.config.skip_permissions if self.state.yolo is None else bool(self.state.yolo)

    def options(self) -> AgyOptions:
        s = self.state
        return AgyOptions(
            model=s.model or self.config.model,
            effort=s.effort or self.config.effort,
            mode=s.mode if s.mode != "" else self.config.mode,
            skip_permissions=self.yolo,
            sandbox=self.config.sandbox,
            extra_args=self.config.extra_args,
        )

    @property
    def busy(self) -> bool:
        return bool(self.session and self.session.busy)

    # ---------------------------------------------------------------- lifecycle
    def ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name=f"chat-{self.chat_id}")

    async def submit(self, prompt: str) -> int:
        """Queue a prompt. Returns how many prompts are ahead of it."""
        ahead = self.queue.qsize() + (1 if self.busy else 0)
        await self.queue.put(prompt)
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
        self.store.save()

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
        if self.session:
            await self.session.close()
            self.session = None

    def _make_session(self) -> AgySession:
        return AgySession(
            command=self.config.agy_command,
            cwd=self.cwd,
            options=self.options(),
            conversation_id=self.state.conversation_id or None,
            persistent=self.config.run_mode == "persistent",
        )

    async def _loop(self) -> None:
        while True:
            try:
                timeout = self.config.idle_shutdown if self.config.idle_shutdown > 0 else None
                prompt = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if self.session and self.session.alive:
                    log.info("chat %s idle; closing agy process", self.chat_id)
                    await self.session.close()
                continue
            except asyncio.CancelledError:
                return
            try:
                await self._run_turn(prompt)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one bad turn kill the worker
                log.exception("turn failed in chat %s", self.chat_id)
                try:
                    await self.messenger.send(f"❌ Bridge error: <code>{_esc(repr(exc))}</code>")
                except Exception:
                    pass
            finally:
                self.queue.task_done()
                self.last_active = time.monotonic()

    # --------------------------------------------------------------------- turn
    async def _run_turn(self, prompt: str) -> None:
        if self._restart_pending and self.session:
            await self.session.close()
            self.session = None
        self._restart_pending = False
        if self.session is None:
            self.session = self._make_session()
        session = self.session
        self._stopping = False
        self.current_prompt = prompt
        self.turn_started = time.monotonic()

        presenter = TurnPresenter(self.messenger, self.config)
        await presenter.begin()
        typing_task = asyncio.create_task(_keep_typing(self.messenger))
        result = None
        error = None
        try:
            async for ev in session.ask(prompt, timeout=self.config.turn_timeout or None):
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

        if session.conversation_id and session.conversation_id != self.state.conversation_id:
            self.state.conversation_id = session.conversation_id
            self.state.remember_conversation(session.conversation_id)
            self.store.save()

        await asyncio.sleep(0.15)  # let the stderr pump catch up with stdout
        notices = [l for l in session.turn_stderr() if PERMISSION_HINT_RE.search(l)]
        if self._stopping:
            self._stopping = False
            await presenter.finish_stopped()
            return
        if result is not None:
            await presenter.finish_result(result, notices)
        else:
            await presenter.finish_error(error.error if error else "agy ended unexpectedly", notices)


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

    def __init__(self, messenger: Messenger, config: Config) -> None:
        self.m = messenger
        self.config = config
        self.status_handle: Any = None
        self.tools: dict[int, str] = {}
        self.tool_order: list[int] = []
        self.preview = ""
        self.started = time.monotonic()
        self._last_edit = 0.0
        self._last_rendered = ""
        self._dirty = False

    def tool_lines(self) -> list[str]:
        return [self.tools[i] for i in self.tool_order]

    async def begin(self) -> None:
        self._last_rendered = status_html("⏳ Working…", [])
        self.status_handle = await self.m.send(self._last_rendered)
        self._last_edit = time.monotonic()

    def on_text(self, delta: str) -> None:
        if delta:
            self.preview += delta
            self._dirty = True

    def on_tool(self, ev) -> None:
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
        rendered = status_html(f"⏳ Working… {self._elapsed()}", self.tool_lines(), self.preview)
        if rendered != self._last_rendered:
            await self.m.edit(self.status_handle, rendered)
            self._last_rendered = rendered
        self._last_edit = now
        self._dirty = False

    async def _close_status(self, header: str) -> None:
        if not self.status_handle:
            return
        if self.tool_order:
            await self.m.edit(self.status_handle, status_html(header, self.tool_lines()))
        else:
            await self.m.delete(self.status_handle)

    async def finish_result(self, result, notices: list[str]) -> None:
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
        await self.send_markdown(text)
        if notices:
            await self.send_notices(notices)

    async def finish_error(self, message: str, notices: list[str]) -> None:
        await self._close_status(f"❌ Failed · {self._elapsed()}")
        body = f"❌ <b>agy error</b>\n<pre>{_esc(message[-3000:])}</pre>"
        await self.m.send(body, f"agy error\n{message[-3000:]}")
        if notices:
            await self.send_notices(notices)

    async def finish_stopped(self) -> None:
        await self._close_status(f"⏹ Stopped · {self._elapsed()}")
        if not self.tool_order:
            await self.m.send("⏹ Stopped. The conversation is kept; just send your next message.")

    async def send_markdown(self, text: str) -> None:
        chunks = render_chunks(text)
        if len(chunks) > MAX_INLINE_CHUNKS:
            first_html, first_plain = chunks[0]
            await self.m.send(first_html, first_plain)
            await self.m.send_document(
                text.encode("utf-8"),
                "agy-response.md",
                caption=f"Full reply ({len(text):,} chars, {len(chunks)} parts) attached.",
            )
            return
        for html_text, plain in chunks:
            if len(html_text) > TELEGRAM_LIMIT:
                await self.m.send(_esc(plain)[: TELEGRAM_LIMIT - 1], plain[: TELEGRAM_LIMIT - 1])
            else:
                await self.m.send(html_text, plain)

    async def send_notices(self, notices: list[str]) -> None:
        shown = "\n".join(_esc(n[:300]) for n in notices[-5:])
        await self.m.send(
            "⚠️ <b>Permission notices from agy</b>\n"
            f"<pre>{shown}</pre>\n"
            "Headless agy can't ask you to approve tools, so it skipped them. "
            "Allow them in <code>~/.gemini/antigravity-cli/settings.json</code> "
            "(<code>permissions.allow</code>) or use /yolo if you enabled it.",
        )
