"""Telegram front end (python-telegram-bot v21, long polling: no open ports)."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from pathlib import Path
from typing import Any

from telegram import BotCommand, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import __version__
from .agy import run_simple
from .config import VALID_EFFORTS, VALID_MODES, Config
from .core import ChatWorker
from .state import StateStore

log = logging.getLogger(__name__)

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
STARTED_AT = time.time()

HELP = """<b>Antigravity ↔ Telegram</b>
Just type: every message goes to <code>agy</code> on your PC, in the same conversation.

<b>Session</b>
/new – start a fresh conversation
/stop – interrupt the running turn (and drop queued messages)
/status – what's running, where, with which settings
/resume &lt;id&gt; – switch to an earlier conversation id
/history – recent conversation ids

<b>Workspace</b>
/cwd [path] – show or change the folder agy works in
/ls – list the current folder

<b>Settings</b> (agy restarts, conversation kept)
/model [slug] – show or set the model (/models lists them)
/effort [low|medium|high|max|default]
/mode [default|accept-edits|auto|plan]
/yolo [on|off] – auto-approve all tools (only if enabled in .env)

<b>Info</b>
/usage – agy quota usage
/whoami – your Telegram user id

Send a file or photo (with an optional caption) and it's saved into the workspace for agy to use."""


class TelegramMessenger:
    """Implements core.Messenger on top of a Telegram chat."""

    def __init__(self, app: Application, chat_id: int) -> None:
        self.bot = app.bot
        self.chat_id = chat_id

    async def _retry(self, fn, *args, **kwargs):
        for attempt in range(4):
            try:
                return await fn(*args, **kwargs)
            except RetryAfter as exc:
                await asyncio.sleep(float(getattr(exc, "retry_after", 2)) + 0.5)
            except (TimedOut, NetworkError):
                if attempt == 3:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
        return None

    async def send(self, html_text: str, plain: str | None = None) -> Message | None:
        try:
            return await self._retry(
                self.bot.send_message,
                chat_id=self.chat_id,
                text=html_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            log.warning("HTML rejected (%s); sending plain text", exc)
            text = plain if plain is not None else re.sub(r"<[^>]+>", "", html_text)
            text = html.unescape(text) if plain is None else text
            return await self._retry(
                self.bot.send_message, chat_id=self.chat_id, text=text[:4096] or "…",
                disable_web_page_preview=True,
            )

    async def edit(self, handle: Message | None, html_text: str) -> None:
        if handle is None:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id, message_id=handle.message_id, text=html_text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
        except RetryAfter:
            pass  # live status edits are best-effort; skip this frame
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.debug("edit failed: %s", exc)
        except (TimedOut, NetworkError):
            pass

    async def delete(self, handle: Message | None) -> None:
        if handle is None:
            return
        try:
            await self.bot.delete_message(chat_id=self.chat_id, message_id=handle.message_id)
        except Exception:
            pass

    async def send_document(self, data: bytes, filename: str, caption: str = "") -> None:
        await self._retry(
            self.bot.send_document, chat_id=self.chat_id, document=data, filename=filename, caption=caption[:1000]
        )

    async def typing(self) -> None:
        await self.bot.send_chat_action(chat_id=self.chat_id, action=ChatAction.TYPING)


class Bridge:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = StateStore(config.state_file)
        self.workers: dict[int, ChatWorker] = {}
        self.app: Application | None = None

    # ----------------------------------------------------------------- helpers
    def worker(self, chat_id: int) -> ChatWorker:
        if chat_id not in self.workers:
            assert self.app is not None
            self.workers[chat_id] = ChatWorker(chat_id, self.config, self.store, TelegramMessenger(self.app, chat_id))
        return self.workers[chat_id]

    @staticmethod
    async def reply(update: Update, text: str) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    def build(self) -> Application:
        app = (
            ApplicationBuilder()
            .token(self.config.telegram_token)
            .concurrent_updates(True)  # /stop must work while a turn is running
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        self.app = app
        allowed = filters.User(user_id=list(self.config.allowed_user_ids))

        app.add_handler(CommandHandler("whoami", self.cmd_whoami))  # open to anyone: only echoes their id
        cmds = {
            "start": self.cmd_help, "help": self.cmd_help, "new": self.cmd_new, "reset": self.cmd_new,
            "stop": self.cmd_stop, "cancel": self.cmd_stop, "status": self.cmd_status,
            "cwd": self.cmd_cwd, "cd": self.cmd_cwd, "ls": self.cmd_ls,
            "model": self.cmd_model, "models": self.cmd_models, "effort": self.cmd_effort,
            "mode": self.cmd_mode, "yolo": self.cmd_yolo, "resume": self.cmd_resume,
            "history": self.cmd_history, "usage": self.cmd_usage,
        }
        for name, fn in cmds.items():
            app.add_handler(CommandHandler(name, fn, filters=allowed))
        app.add_handler(MessageHandler(allowed & (filters.Document.ALL | filters.PHOTO), self.on_file))
        app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(MessageHandler(allowed & (filters.VOICE | filters.AUDIO | filters.VIDEO), self.on_unsupported))
        app.add_handler(MessageHandler(~allowed, self.on_stranger))
        app.add_error_handler(self.on_error)
        return app

    async def _post_init(self, app: Application) -> None:
        me = await app.bot.get_me()
        log.info("bot @%s ready (bridge v%s); allowed users: %s", me.username, __version__, self.config.allowed_user_ids)
        await app.bot.set_my_commands([
            BotCommand("new", "Start a fresh agy conversation"),
            BotCommand("stop", "Interrupt the running turn"),
            BotCommand("status", "Show session status"),
            BotCommand("cwd", "Show/change workspace folder"),
            BotCommand("ls", "List the workspace folder"),
            BotCommand("model", "Show/set model"),
            BotCommand("models", "List available models"),
            BotCommand("effort", "Show/set reasoning effort"),
            BotCommand("mode", "Show/set execution mode"),
            BotCommand("yolo", "Toggle auto-approve tools"),
            BotCommand("resume", "Resume a conversation id"),
            BotCommand("history", "Recent conversation ids"),
            BotCommand("usage", "agy quota usage"),
            BotCommand("help", "Help"),
        ])
        for uid in self.config.allowed_user_ids:
            try:
                await app.bot.send_message(uid, f"🟢 agy bridge online · workspace <code>{html.escape(str(self.config.workdir))}</code>", parse_mode=ParseMode.HTML)
            except Exception:
                log.info("could not greet user %s (they must /start the bot first)", uid)

    async def _post_shutdown(self, app: Application) -> None:
        for w in self.workers.values():
            await w.shutdown()
        self.store.save()

    # ---------------------------------------------------------------- handlers
    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not msg.text:
            return
        text = msg.text
        if msg.reply_to_message and (msg.reply_to_message.text or msg.reply_to_message.caption):
            quoted = (msg.reply_to_message.text or msg.reply_to_message.caption or "")[:2000]
            text = f"(Replying to this earlier message:)\n> " + quoted.replace("\n", "\n> ") + f"\n\n{text}"
        await self._enqueue(update, text)

    async def _enqueue(self, update: Update, prompt: str) -> None:
        w = self.worker(update.effective_chat.id)
        ahead = await w.submit(prompt)
        if ahead:
            await self.reply(update, f"📥 Queued ({ahead} ahead). /stop cancels the current turn.")

    async def on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        w = self.worker(update.effective_chat.id)
        if msg.document:
            tg_file_ref, name, size = msg.document, msg.document.file_name or "file", msg.document.file_size or 0
        else:
            photo = msg.photo[-1]
            tg_file_ref, name, size = photo, f"photo_{msg.message_id}.jpg", photo.file_size or 0
        if size > self.config.max_upload_mb * 1024 * 1024:
            await self.reply(update, f"❌ File too large (limit {self.config.max_upload_mb} MB).")
            return
        dest_dir = w.cwd / ".telegram_uploads"
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe = SAFE_NAME_RE.sub("_", name).strip("._") or "file"
        dest = dest_dir / f"{int(time.time())}_{safe}"
        tg_file = await tg_file_ref.get_file()
        await tg_file.download_to_drive(custom_path=str(dest))
        caption = (msg.caption or "").strip()
        rel = dest.relative_to(w.cwd)
        if caption:
            prompt = f"{caption}\n\n[The user attached a file, saved at: {rel} (absolute: {dest})]"
            await self._enqueue(update, prompt)
        else:
            await self.reply(update, f"📎 Saved to <code>{html.escape(str(rel))}</code>. Tell agy what to do with it.")

    async def on_unsupported(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.reply(update, "Voice/audio/video isn't supported yet — send text, or a file with a caption.")

    async def on_stranger(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        log.warning("ignored message from unauthorized user %s (@%s)", user.id if user else "?", user.username if user else "?")

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("telegram handler error", exc_info=ctx.error)

    # ---------------------------------------------------------------- commands
    async def cmd_whoami(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        allowed = u.id in self.config.allowed_user_ids
        await self.reply(update, f"Your Telegram user id: <code>{u.id}</code>\n" + ("✅ You're allowed." if allowed else "Add it to ALLOWED_USER_IDS in .env, then restart the bridge."))

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.reply(update, HELP)

    async def cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.worker(update.effective_chat.id).reset()
        await self.reply(update, "🆕 Fresh conversation. Your next message starts a new agy thread.")

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        running, dropped = await self.worker(update.effective_chat.id).stop_turn()
        if not running and not dropped:
            await self.reply(update, "Nothing is running.")
        elif dropped:
            await self.reply(update, f"⏹ Stopping. Dropped {dropped} queued message(s).")

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        w = self.worker(update.effective_chat.id)
        o = w.options()
        s = w.session
        if w.busy:
            secs = int(time.monotonic() - w.turn_started)
            state = f"🟡 working ({secs}s): <i>{html.escape(w.current_prompt[:120])}</i>"
        elif s and s.alive:
            state = "🟢 idle, agy process warm"
        else:
            state = "⚪ idle, agy starts on next message"
        up = int(time.time() - STARTED_AT)
        lines = [
            f"<b>Status</b> {state}",
            f"Workspace: <code>{html.escape(str(w.cwd))}</code>",
            f"Conversation: <code>{html.escape(w.state.conversation_id or '(new)')}</code>",
            f"Model: <code>{html.escape(o.model or 'default')}</code> · Effort: <code>{o.effort or 'default'}</code> · Mode: <code>{o.mode or 'default'}</code>",
            f"Auto-approve tools: {'⚠️ ON' if o.skip_permissions else 'off'} · Sandbox: {'on' if o.sandbox else 'off'}",
            f"Queued: {w.queue.qsize()} · Run mode: {self.config.run_mode} · Uptime: {up // 3600}h{(up % 3600) // 60:02d}m",
        ]
        await self.reply(update, "\n".join(lines))

    async def cmd_cwd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        w = self.worker(update.effective_chat.id)
        arg = " ".join(ctx.args).strip() if ctx.args else ""
        if not arg:
            roots = ", ".join(f"<code>{html.escape(str(r))}</code>" for r in self.config.allowed_roots)
            await self.reply(update, f"Workspace: <code>{html.escape(str(w.cwd))}</code>\nAllowed roots: {roots}")
            return
        target = Path(arg).expanduser()
        if not target.is_absolute():
            target = w.cwd / target
        target = target.resolve()
        if not target.is_dir():
            await self.reply(update, f"❌ Not a folder: <code>{html.escape(str(target))}</code>")
            return
        if not self.config.is_path_allowed(target):
            await self.reply(update, "❌ That folder is outside ALLOWED_ROOTS.")
            return
        if w.busy:
            await self.reply(update, "⏳ A turn is running; /stop it first.")
            return
        await w.reset()  # agy conversations are tied to a workspace
        w.state.cwd = str(target)
        self.store.save()
        await self.reply(update, f"📁 Workspace is now <code>{html.escape(str(target))}</code> (new conversation).")

    async def cmd_ls(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        w = self.worker(update.effective_chat.id)
        try:
            entries = sorted(w.cwd.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            await self.reply(update, f"❌ {html.escape(str(exc))}")
            return
        shown = [("📁 " if p.is_dir() else "📄 ") + p.name for p in entries[:60]]
        more = f"\n… and {len(entries) - 60} more" if len(entries) > 60 else ""
        body = html.escape("\n".join(shown) or "(empty)")
        await self.reply(update, f"<code>{html.escape(str(w.cwd))}</code>\n<pre>{body}</pre>{more}")

    async def _set_option(self, update: Update, ctx, field: str, valid: set[str] | None, label: str) -> None:
        w = self.worker(update.effective_chat.id)
        arg = (ctx.args[0].strip() if ctx.args else "")
        current = getattr(w.options(), field) or "default"
        if not arg:
            await self.reply(update, f"{label}: <code>{html.escape(current)}</code>")
            return
        value = "" if arg.lower() == "default" else arg
        if valid is not None and value not in valid:
            await self.reply(update, f"❌ {label} must be one of: " + ", ".join(sorted(v or "default" for v in valid)))
            return
        setattr(w.state, field, value)
        self.store.save()
        await w.invalidate()
        await self.reply(update, f"✅ {label} → <code>{html.escape(value or 'default')}</code> (applies from the next message)")

    async def cmd_model(self, update, ctx):
        await self._set_option(update, ctx, "model", None, "Model")

    async def cmd_effort(self, update, ctx):
        await self._set_option(update, ctx, "effort", VALID_EFFORTS, "Effort")

    async def cmd_mode(self, update, ctx):
        await self._set_option(update, ctx, "mode", VALID_MODES - {"default"}, "Mode")

    async def cmd_yolo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        w = self.worker(update.effective_chat.id)
        arg = (ctx.args[0].lower() if ctx.args else "")
        if not arg:
            await self.reply(update, f"Auto-approve tools: {'⚠️ ON' if w.yolo else 'off'}")
            return
        if not self.config.allow_yolo_toggle:
            await self.reply(update, "🔒 /yolo is disabled. Set ALLOW_YOLO_TOGGLE=true in .env to allow it from Telegram.")
            return
        if arg not in {"on", "off"}:
            await self.reply(update, "Use /yolo on or /yolo off")
            return
        w.state.yolo = arg == "on"
        self.store.save()
        await w.invalidate()
        await self.reply(update, "⚠️ Auto-approve ON: agy will run commands and edit files without asking." if w.state.yolo else "✅ Auto-approve off.")

    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        w = self.worker(update.effective_chat.id)
        if not ctx.args:
            await self.reply(update, "Usage: /resume &lt;conversation-id&gt; (see /history)")
            return
        conv = ctx.args[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", conv):
            await self.reply(update, "❌ That doesn't look like a conversation id.")
            return
        await w.reset()
        w.state.conversation_id = conv
        w.state.remember_conversation(conv)
        self.store.save()
        await self.reply(update, f"↩️ Next message continues conversation <code>{conv}</code>.")

    async def cmd_history(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        w = self.worker(update.effective_chat.id)
        if not w.state.history:
            await self.reply(update, "No conversations yet.")
            return
        lines = [f"{'▶️' if c == w.state.conversation_id else '•'} <code>{c}</code>" for c in w.state.history]
        await self.reply(update, "<b>Recent conversations</b> (tap to copy, then /resume &lt;id&gt;)\n" + "\n".join(lines))

    async def _run_cli(self, update: Update, args: list[str], title: str) -> None:
        w = self.worker(update.effective_chat.id)
        code, out = await run_simple(self.config.agy_command, args, w.cwd, timeout=90)
        out = out.strip() or "(no output)"
        await self.reply(update, f"<b>{title}</b>\n<pre>{html.escape(out[-3500:])}</pre>" + ("" if code == 0 else f"\nexit {code}"))

    async def cmd_models(self, update, ctx):
        await self._run_cli(update, ["models"], "agy models")

    async def cmd_usage(self, update, ctx):
        await self._run_cli(update, ["-p", "/usage"], "agy usage")


def run(config: Config) -> None:
    bridge = Bridge(config)
    app = bridge.build()
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
