"""Telegram front end (python-telegram-bot v21, long polling: no open ports)."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import __version__, agysettings, ghcli, gitops, projects, voice
from .agy import run_simple
from .config import VALID_EFFORTS, VALID_MODES, Config
from .core import Buttons, ChatWorker, Job
from .features import FeaturesMixin
from .procs import ProcResult, run_shell
from .prompts import all_shortcuts
from .state import StateStore

log = logging.getLogger(__name__)

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
STARTED_AT = time.time()
BRIDGE_DIR = Path(__file__).resolve().parents[2]
RESTART_CODE = 3
INLINE_OUTPUT = 3500

HELP = """<b>agy ↔ Telegram</b>
Type anything: it goes to <code>agy</code> on your PC in the current project, same conversation. Reply to a message to quote it. Send files, screenshots or voice notes.

<b>Projects</b>
/projects – tap to switch codebase (each keeps its own conversation)
/p &lt;name&gt; – switch by name · /cwd [path|-] – show/change folder
/ls [path] · /tree [depth] · /cat &lt;file&gt; [from-to] · /get &lt;file&gt;
/find &lt;name&gt; · /grep &lt;text&gt;

<b>Git</b>
/git – status · /diff [file|staged|turn] · /log [n] · /branches
/undo – revert the last agy turn's changes · /commit &lt;msg&gt; · /push · /pull

<b>Background tasks</b> (own branch + worktree, run in parallel)
/task &lt;what to do&gt; – reply to its messages to steer · /tasks – Diff / Merge / PR / Discard

<b>GitHub</b> (needs the gh CLI)
/pr [title] – push + open a draft PR · /ci – runs for this branch (CI is watched after every push)

<b>Run</b>
/test [cmd] – run tests (failures → one tap to have agy fix them)
/settest &lt;cmd&gt; – set this project's test command
/sh &lt;cmd&gt; or <code>!cmd</code> – run a shell command (if ALLOW_SHELL=true)

<b>Shortcuts</b> (/prompts lists all, incl. your prompts.toml)
/review · /fix · /explain [x] · /plan &lt;task&gt; (then ✅ Implement) · /commitmsg · /prdesc · /debug &lt;error&gt; · /tests &lt;x&gt; · /refactor &lt;x&gt; · /deps

<b>Session</b>
/new · /stop · /retry · /queue · /sessions · /status · /last · /export
/at 09:00 &lt;prompt&gt; · /every 2h &lt;prompt&gt; · /schedules

<b>Settings</b>
/model [slug] · /models · /effort [..] · /mode [default|accept-edits|auto|plan]
/allow [rule] – list or add agy permission rules · /yolo on|off

<b>Bridge</b>
/usage · /logs · /restart · /update · /whoami

💡 Use a Telegram group with Topics to run one agy per topic, e.g. a topic per repo, in parallel."""


def kb(buttons: Buttons | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in buttons if row])


def esc(s: Any) -> str:
    return html.escape(str(s), quote=False)


def pre(text: str, lang: str = "") -> str:
    cls = f' class="language-{lang}"' if lang else ""
    return f"<pre><code{cls}>{esc(text)}</code></pre>"


class TelegramMessenger:
    """Implements core.Messenger on top of a Telegram chat (optionally a forum topic)."""

    def __init__(self, app: Application, chat_id: int, thread_id: int | None = None, on_sent=None) -> None:
        self.bot = app.bot
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.on_sent = on_sent  # callback(message) so replies can be routed back to this worker

    def _track(self, msg):
        if msg is not None and self.on_sent is not None:
            try:
                self.on_sent(msg)
            except Exception:
                pass
        return msg

    def _where(self) -> dict[str, Any]:
        d: dict[str, Any] = {"chat_id": self.chat_id}
        if self.thread_id:
            d["message_thread_id"] = self.thread_id
        return d

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

    async def send(self, html_text: str, plain: str | None = None, *, buttons: Buttons | None = None, silent: bool = False) -> Message | None:
        try:
            return self._track(await self._retry(
                self.bot.send_message, **self._where(), text=html_text, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True, reply_markup=kb(buttons), disable_notification=silent,
            ))
        except BadRequest as exc:
            log.warning("HTML rejected (%s); sending plain text", exc)
            text = plain if plain is not None else html.unescape(re.sub(r"<[^>]+>", "", html_text))
            return self._track(await self._retry(
                self.bot.send_message, **self._where(), text=text[:4096] or "…",
                disable_web_page_preview=True, reply_markup=kb(buttons), disable_notification=silent,
            ))

    async def edit(self, handle: Message | None, html_text: str, *, buttons: Buttons | None = None) -> None:
        if handle is None:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id, message_id=handle.message_id, text=html_text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb(buttons),
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

    async def send_document(self, data: bytes, filename: str, caption: str = "", buttons: Buttons | None = None) -> None:
        self._track(await self._retry(
            self.bot.send_document, **self._where(), document=data, filename=filename,
            caption=caption[:1000], reply_markup=kb(buttons),
        ))

    async def typing(self) -> None:
        await self.bot.send_chat_action(**self._where(), action=ChatAction.TYPING)


class Bridge(FeaturesMixin):
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = StateStore(config.state_file)
        self.workers: dict[str, ChatWorker] = {}
        self.app: Application | None = None
        self.exit_code = 0
        self.shortcuts = all_shortcuts(config.prompts_file)
        self._project_cache: tuple[float, list[Path]] = (0.0, [])
        self.msg_owner: dict[tuple[int, int], str] = {}  # (chat, message id) -> worker key
        self._init_features()

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _key(update: Update) -> tuple[int, int | None, str]:
        chat = update.effective_chat
        msg = update.effective_message
        thread = msg.message_thread_id if msg is not None and getattr(msg, "is_topic_message", False) else None
        return chat.id, thread, f"{chat.id}:{thread}" if thread else str(chat.id)

    def make_worker(self, chat_id: int, thread: int | None, key: str) -> ChatWorker:
        if key not in self.workers:
            assert self.app is not None

            def on_sent(msg, key=key, chat_id=chat_id):
                self.msg_owner[(chat_id, msg.message_id)] = key
                if len(self.msg_owner) > 5000:
                    for k in list(self.msg_owner)[:1000]:
                        self.msg_owner.pop(k, None)

            m = TelegramMessenger(self.app, chat_id, thread, on_sent=on_sent)
            self.workers[key] = ChatWorker(chat_id, self.config, self.store, m, key=key)
        return self.workers[key]

    def worker(self, update: Update) -> ChatWorker:
        chat_id, thread, key = self._key(update)
        return self.make_worker(chat_id, thread, key)

    def worker_for_reply(self, update: Update) -> ChatWorker:
        """The worker that sent the message being replied to (e.g. a background task), else this chat's."""
        msg = update.effective_message
        r = msg.reply_to_message if msg else None
        if r is not None:
            key = self.msg_owner.get((update.effective_chat.id, r.message_id))
            if key and key in self.workers:
                return self.workers[key]
        return self.worker(update)

    def messenger(self, update: Update) -> TelegramMessenger:
        return self.worker(update).messenger  # type: ignore[return-value]

    @staticmethod
    async def reply(update: Update, text: str, buttons: Buttons | None = None) -> Message | None:
        if update.effective_message:
            return await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb(buttons)
            )
        return None

    async def send_output(self, update: Update, title: str, out: str, filename: str = "output.txt",
                          lang: str = "", buttons: Buttons | None = None, keep: str = "tail") -> None:
        """Short output inline as <pre>; long output as a file plus its tail."""
        out = out.rstrip() or "(no output)"
        m = self.messenger(update)
        if len(out) <= INLINE_OUTPUT:
            await m.send(f"{title}\n{pre(out, lang)}", buttons=buttons)
            return
        await m.send_document(out.encode("utf-8"), filename, caption=re.sub(r"<[^>]+>", "", title)[:900])
        piece = out[-INLINE_OUTPUT + 200:] if keep == "tail" else out[: INLINE_OUTPUT - 200]
        label = "last lines" if keep == "tail" else "first lines"
        await m.send(f"{title} <i>({label}; full output attached)</i>\n{pre(piece, lang)}", buttons=buttons)

    def resolve_path(self, w: ChatWorker, arg: str) -> Path | None:
        p = Path(arg.strip().strip('"')).expanduser()
        if not p.is_absolute():
            p = w.cwd / p
        try:
            p = p.resolve()
        except OSError:
            return None
        if not (self.config.is_path_allowed(p) or _inside(p, w.cwd)):
            return None
        return p

    def project_list(self, refresh: bool = False) -> list[Path]:
        ts, cached = self._project_cache
        if refresh or not cached or time.time() - ts > 300:
            cached = projects.discover(self.config.allowed_roots, max_depth=self.config.project_scan_depth)
            self._project_cache = (time.time(), cached)
        return cached

    def test_command(self, w: ChatWorker) -> str:
        return w.state.test_commands.get(str(w.cwd)) or self.config.test_command or projects.detect_test_command(w.cwd)

    # ------------------------------------------------------------------- build
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
        viewers = filters.User(user_id=list(self.config.allowed_user_ids | set(self.config.viewer_user_ids)))
        read_only = {"help", "start", "status", "git", "diff", "log", "branches", "ls", "tree", "cat", "find",
                     "grep", "tasks", "ci", "queue", "history", "sessions", "schedules", "prompts"}

        app.add_handler(CommandHandler("whoami", self.cmd_whoami))  # open to anyone: only echoes their id
        cmds = {
            "start": self.cmd_help, "help": self.cmd_help, "new": self.cmd_new, "reset": self.cmd_new,
            "stop": self.cmd_stop, "cancel": self.cmd_stop, "status": self.cmd_status, "retry": self.cmd_retry,
            "last": self.cmd_last, "export": self.cmd_export,
            "cwd": self.cmd_cwd, "cd": self.cmd_cwd, "ls": self.cmd_ls, "tree": self.cmd_tree,
            "cat": self.cmd_cat, "get": self.cmd_get, "find": self.cmd_find, "grep": self.cmd_grep,
            "projects": self.cmd_projects, "p": self.cmd_p,
            "git": self.cmd_git, "diff": self.cmd_diff, "log": self.cmd_log, "branches": self.cmd_branches,
            "undo": self.cmd_undo, "commit": self.cmd_commit, "push": self.cmd_push, "pull": self.cmd_pull,
            "test": self.cmd_test, "settest": self.cmd_settest, "sh": self.cmd_sh,
            "prompts": self.cmd_prompts,
            "model": self.cmd_model, "models": self.cmd_models, "effort": self.cmd_effort,
            "mode": self.cmd_mode, "yolo": self.cmd_yolo, "allow": self.cmd_allow, "resume": self.cmd_resume,
            "history": self.cmd_history, "usage": self.cmd_usage,
            "logs": self.cmd_logs, "restart": self.cmd_restart, "update": self.cmd_update,
            "task": self.cmd_task, "tasks": self.cmd_tasks, "pr": self.cmd_pr, "ci": self.cmd_ci,
            "every": self.cmd_every, "at": self.cmd_at, "schedules": self.cmd_schedules,
            "sessions": self.cmd_sessions, "queue": self.cmd_queue,
        }
        for name in self.shortcuts:
            if name not in cmds:
                cmds[name] = self.cmd_shortcut
        for name, fn in cmds.items():
            app.add_handler(CommandHandler(name, fn, filters=viewers if name in read_only else allowed))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_handler(MessageHandler(allowed & (filters.Document.ALL | filters.PHOTO), self.on_file))
        app.add_handler(MessageHandler(allowed & (filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE), self.on_voice))
        app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(MessageHandler(allowed & filters.COMMAND, self.on_unknown_command))
        app.add_handler(MessageHandler(viewers & ~allowed, self.on_viewer))
        app.add_handler(MessageHandler(~viewers, self.on_stranger))
        app.add_error_handler(self.on_error)
        return app

    async def _post_init(self, app: Application) -> None:
        me = await app.bot.get_me()
        log.info("bot @%s ready (bridge v%s); allowed users: %s", me.username, __version__, self.config.allowed_user_ids)
        menu = [
            ("projects", "Switch project"), ("git", "Git status"), ("diff", "Show changes"),
            ("undo", "Undo last agy turn"), ("test", "Run tests"), ("review", "Review my changes"),
            ("fix", "Fix failing tests"), ("plan", "Plan a task (no edits)"), ("new", "Fresh conversation"),
            ("task", "Background task on its own branch"), ("tasks", "Background tasks"),
            ("stop", "Stop the running turn"), ("retry", "Retry last prompt"), ("status", "Session status"),
            ("commit", "Commit all changes"), ("push", "Push"), ("pr", "Open a pull request"), ("ci", "CI status"),
            ("sessions", "Pick a conversation"), ("queue", "Queued prompts"), ("schedules", "Scheduled prompts"), ("cat", "Show a file"), ("get", "Download a file"),
            ("sh", "Run a shell command"), ("model", "Show/set model"), ("mode", "Show/set mode"),
            ("allow", "agy permission rules"), ("prompts", "All shortcuts"), ("help", "Help"),
        ]
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in menu])
        self._spawn(self._scheduler())
        for uid in self.config.allowed_user_ids:
            try:
                await app.bot.send_message(
                    uid, f"🟢 agy bridge v{__version__} online · <code>{esc(self.config.workdir)}</code>",
                    parse_mode=ParseMode.HTML, disable_notification=True,
                )
            except Exception:
                log.info("could not greet user %s (they must /start the bot first)", uid)

    async def _post_shutdown(self, app: Application) -> None:
        for w in self.workers.values():
            await w.shutdown()
        self.store.save()

    # ---------------------------------------------------------------- messages
    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not msg.text:
            return
        text = msg.text
        if text.startswith("!") and len(text) > 1:
            await self._shell(update, text[1:].strip())
            return
        target = self.worker_for_reply(update)
        if target is not self.worker(update):
            # a reply to a background task's message steers that task
            ahead = await target.submit(Job(prompt=text, display=msg.text))
            await self.reply(update, f"🧵 Sent to {esc(target.title or target.key)}" + (f" (queued, {ahead} ahead)" if ahead else ""))
            return
        if msg.reply_to_message and (msg.reply_to_message.text or msg.reply_to_message.caption):
            quoted = (msg.reply_to_message.text or msg.reply_to_message.caption or "")[:2000]
            text = "(Replying to this earlier message:)\n> " + quoted.replace("\n", "\n> ") + f"\n\n{text}"
        await self._enqueue(update, Job(prompt=text, display=msg.text))

    async def _enqueue(self, update: Update, job: Job) -> None:
        w = self.worker(update)
        ahead = await w.submit(job)
        if ahead:
            await self.reply(update, f"📥 Queued ({ahead} ahead). /queue to manage.",
                             [[("⚡ Stop current & run this", w.action_token("interrupt_with", job.id)),
                               ("✖️ Cancel", w.action_token("cancel_job", job.id))]])

    async def _save_upload(self, w: ChatWorker, tg_file_ref, name: str) -> Path:
        dest_dir = w.cwd / ".telegram_uploads"
        dest_dir.mkdir(parents=True, exist_ok=True)
        gi = dest_dir / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")  # keep uploads out of the user's commits
        safe = SAFE_NAME_RE.sub("_", name).strip("._") or "file"
        dest = dest_dir / f"{int(time.time())}_{safe}"
        tg_file = await tg_file_ref.get_file()
        await tg_file.download_to_drive(custom_path=str(dest))
        return dest

    async def on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        w = self.worker(update)
        if msg.document:
            ref, name, size = msg.document, msg.document.file_name or "file", msg.document.file_size or 0
        else:
            photo = msg.photo[-1]
            ref, name, size = photo, f"photo_{msg.message_id}.jpg", photo.file_size or 0
        if size > self.config.max_upload_mb * 1024 * 1024:
            await self.reply(update, f"❌ File too large (limit {self.config.max_upload_mb} MB; Telegram bots can download up to 20 MB).")
            return
        dest = await self._save_upload(w, ref, name)
        caption = (msg.caption or "").strip()
        rel = dest.relative_to(w.cwd)
        if caption:
            prompt = f"{caption}\n\n[The user attached a file, saved at: {rel} (absolute: {dest})]"
            await self._enqueue(update, Job(prompt=prompt, display=f"{caption} [file: {rel}]"))
        else:
            await self.reply(update, f"📎 Saved to <code>{esc(rel)}</code>. Tell agy what to do with it (reply to this message).")

    async def on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        w = self.worker(update)
        backend = voice.backend_name(self.config.voice_backend, self.config.openai_api_key)
        if not backend:
            await self.reply(update, "🎙 Voice notes need a transcriber: run <code>pip install faster-whisper</code> in the bridge's venv (free, local), or set OPENAI_API_KEY in .env. Then /restart.")
            return
        ref = msg.voice or msg.audio or msg.video_note
        folder = Path("data") / "voice"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{int(time.time())}_{msg.message_id}.ogg"
        await (await ref.get_file()).download_to_drive(custom_path=str(path))
        note = await self.reply(update, "🎙 Transcribing…")
        try:
            text = await voice.transcribe(path, backend, self.config.openai_api_key, self.config.whisper_model, self.config.voice_language)
        except Exception as exc:
            log.exception("transcription failed")
            await self.reply(update, f"❌ Transcription failed: <code>{esc(exc)}</code>")
            return
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        if not text:
            await self.reply(update, "🎙 I couldn't hear anything in that.")
            return
        if self.config.voice_confirm:
            buttons = [[("▶️ Send to agy", w.action_token("voice_send", text)), ("✖️ Discard", w.action_token("dismiss"))]]
            if note:
                await note.edit_text(f"🎙 <i>{esc(text)}</i>", parse_mode=ParseMode.HTML, reply_markup=kb(buttons))
            return
        if note:
            await note.edit_text(f"🎙 <i>{esc(text)}</i>", parse_mode=ParseMode.HTML)
        await self._enqueue(update, Job(prompt=text, display=f"🎙 {text}"))

    async def on_unknown_command(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.reply(update, "Unknown command. /help lists them; anything without a slash goes to agy.")

    async def on_viewer(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.reply(update, "👀 You have read-only access: /status, /git, /diff, /log, /cat, /tasks, /ci … Only the owner can prompt agy.")

    async def on_stranger(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        log.warning("ignored update from unauthorized user %s (@%s)", user.id if user else "?", user.username if user else "?")

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("telegram handler error", exc_info=ctx.error)
        if isinstance(update, Update) and update.effective_user and update.effective_user.id in self.config.allowed_user_ids:
            try:
                await self.reply(update, f"❌ <code>{esc(repr(ctx.error))[:500]}</code>")
            except Exception:
                pass

    # ----------------------------------------------------------------- buttons
    async def on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        if not q.from_user or q.from_user.id not in self.config.allowed_user_ids:
            await q.answer("Not allowed.", show_alert=True)
            return
        data = q.data or ""
        w = self.worker(update)
        action, payload = w.actions.get(data[2:], (None, None)) if data.startswith("a:") else (None, None)
        if action is None:
            await q.answer("That button has expired.")
            return
        await q.answer()
        handler = getattr(self, f"act_{action}", None)
        if handler is None:
            return
        await handler(update, w, payload)

    async def _drop_buttons(self, update: Update) -> None:
        try:
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    async def act_stop(self, update, w: ChatWorker, payload) -> None:
        await w.stop_turn(clear_queue=False)

    async def act_dismiss(self, update, w, payload) -> None:
        await self._drop_buttons(update)

    async def act_retry(self, update, w: ChatWorker, payload) -> None:
        if not w.last_turn:
            await self.reply(update, "Nothing to retry.")
            return
        await self._enqueue(update, w.last_turn.job)

    async def act_diff(self, update, w: ChatWorker, payload) -> None:
        await self._send_turn_diff(update, w)

    async def act_undo(self, update, w: ChatWorker, payload) -> None:
        await self._ask_undo(update, w)

    async def act_undo_yes(self, update, w: ChatWorker, payload) -> None:
        await self._drop_buttons(update)
        rec = w.last_turn
        if rec is None or rec is not payload or rec.checkpoint is None:
            await self.reply(update, "That undo has expired (another turn ran since).")
            return
        if w.busy:
            await self.reply(update, "⏳ agy is working; /stop it first.")
            return
        ok, msg = await gitops.undo(rec.checkpoint)
        if ok:
            rec.undone = True
            await w.submit(Job(
                prompt="[Note from the bridge: the user reverted all the file changes from your previous turn. "
                       "The files are back to how they were before it. Just acknowledge briefly.]",
                label="undo", display="(undo note)",
            ))
        await self.reply(update, ("↩️ " if ok else "❌ ") + esc(msg))

    async def act_approve_once(self, update, w: ChatWorker, payload) -> None:
        await self._drop_buttons(update)
        prompt = "You were blocked from running a tool in your last turn. The user has now approved it for this turn: retry what was blocked and finish the task."
        await self._enqueue(update, Job(prompt=prompt, overrides={"skip_permissions": True}, label="approved once", display="(approved: retry with full access)"))

    async def act_allow_rule(self, update, w: ChatWorker, payload) -> None:
        await self._drop_buttons(update)
        await self._add_rule(update, w, str(payload), retry=True)

    async def act_fix_tests(self, update, w: ChatWorker, payload) -> None:
        await self._drop_buttons(update)
        cmd, out = payload
        prompt = (f"I ran the tests with `{cmd}` and they failed. Find the root cause and fix it, then rerun `{cmd}` "
                  f"until it passes. Here is the end of the output:\n\n```\n{out[-6000:]}\n```")
        await self._enqueue(update, Job(prompt=prompt, label="fix tests", display=f"(fix failing tests: {cmd})"))

    async def act_explain_output(self, update, w: ChatWorker, payload) -> None:
        await self._drop_buttons(update)
        cmd, out = payload
        prompt = f"I ran `{cmd}` in the workspace. Explain this output and what to do about it:\n\n```\n{out[-6000:]}\n```"
        await self._enqueue(update, Job(prompt=prompt, display=f"(explain output of: {cmd})"))

    async def act_commit_yes(self, update, w: ChatWorker, payload) -> None:
        await self._drop_buttons(update)
        root, message = payload
        r = await gitops.git(root, "add", "-A")
        if r.ok:
            r = await gitops.git(root, "commit", "-m", message)
        await self.send_output(update, "✅ <b>Committed</b>" if r.ok else "❌ <b>Commit failed</b>", r.out)

    async def act_push_yes(self, update, w: ChatWorker, payload) -> None:
        await self._drop_buttons(update)
        root, set_upstream = payload
        args = ["push", "-u", "origin", "HEAD"] if set_upstream else ["push"]
        await self.reply(update, "⬆️ Pushing…")
        r = await gitops.git(root, *args, timeout=180)
        note = "\n<i>I'll report back when CI finishes.</i>" if r.ok and ghcli.available() else ""
        await self.send_output(update, ("✅ <b>Pushed</b>" + note) if r.ok else "❌ <b>Push failed</b>", r.out)
        if r.ok:
            br = await gitops.git(root, "rev-parse", "--abbrev-ref", "HEAD")
            self._spawn(self._watch_ci(update, root, br.out.strip()))

    async def act_project(self, update, w: ChatWorker, payload) -> None:
        await self._switch(update, w, Path(payload))

    async def act_voice_send(self, update, w: ChatWorker, payload) -> None:
        await self._drop_buttons(update)
        await self._enqueue(update, Job(prompt=str(payload), display=f"🎙 {payload}"))

    async def act_restart_yes(self, update, w, payload) -> None:
        await self._drop_buttons(update)
        await self.reply(update, "🔄 Restarting… (back in a few seconds if you run it with run.ps1 or the autostart task)")
        self._request_exit(RESTART_CODE)

    # ---------------------------------------------------------------- session
    async def cmd_whoami(self, update: Update, ctx) -> None:
        u = update.effective_user
        allowed = u.id in self.config.allowed_user_ids
        await self.reply(update, f"Your Telegram user id: <code>{u.id}</code>\n" + ("✅ You're allowed." if allowed else "Add it to ALLOWED_USER_IDS in .env, then restart the bridge."))

    async def cmd_help(self, update: Update, ctx) -> None:
        await self.reply(update, HELP)

    async def cmd_new(self, update: Update, ctx) -> None:
        await self.worker(update).reset()
        await self.reply(update, "🆕 Fresh conversation in this project. Your next message starts a new agy thread.")

    async def cmd_stop(self, update: Update, ctx) -> None:
        running, dropped = await self.worker(update).stop_turn()
        if not running and not dropped:
            await self.reply(update, "Nothing is running.")
        elif dropped:
            await self.reply(update, f"⏹ Stopping. Dropped {dropped} queued message(s).")

    async def cmd_retry(self, update: Update, ctx) -> None:
        await self.act_retry(update, self.worker(update), None)

    async def cmd_last(self, update: Update, ctx) -> None:
        w = self.worker(update)
        text = w.transcript.last_reply()
        if not text:
            await self.reply(update, "No reply yet.")
            return
        from .core import TurnPresenter

        await TurnPresenter(w).send_markdown(text)

    async def cmd_export(self, update: Update, ctx) -> None:
        w = self.worker(update)
        md = w.transcript.to_markdown(w.state.conversation_id or None if ctx.args and ctx.args[0] == "this" else None)
        if md.count("\n## ") == 0:
            await self.reply(update, "Nothing to export yet.")
            return
        await self.messenger(update).send_document(md.encode("utf-8"), f"agy-transcript-{time.strftime('%Y%m%d-%H%M')}.md",
                                                   caption="Transcript of this chat (/export this = current conversation only)")

    async def cmd_status(self, update: Update, ctx) -> None:
        w = self.worker(update)
        o = w.options()
        s = w.session
        if w.busy:
            secs = int(time.monotonic() - w.turn_started)
            state = f"🟡 working ({secs}s): <i>{esc(w.current_prompt[:120])}</i>"
        elif s and s.alive:
            state = "🟢 idle, agy process warm"
        else:
            state = "⚪ idle, agy starts on next message"
        up = int(time.time() - STARTED_AT)
        st = await gitops.status(w.cwd)
        git_line = ""
        if st is not None:
            n = len(st.staged) + len(st.unstaged) + len(st.untracked)
            git_line = f"\nGit: 🌿 <code>{esc(st.branch)}</code>" + (f" · {n} changed" if n else " · clean") + (f" · ↑{st.ahead}" if st.ahead else "") + (f" · ↓{st.behind}" if st.behind else "")
        test = self.test_command(w)
        lines = [
            f"<b>Status</b> {state}",
            f"Project: <code>{esc(w.cwd)}</code>{git_line}",
            f"Conversation: <code>{esc(w.state.conversation_id or '(new)')}</code>",
            f"Model: <code>{esc(o.model or 'default')}</code> · Effort: <code>{o.effort or 'default'}</code> · Mode: <code>{o.mode or 'default'}</code>",
            f"Auto-approve tools: {'⚠️ ON' if o.skip_permissions else 'off'} · Sandbox: {'on' if o.sandbox else 'off'} · Shell: {'on' if self.config.allow_shell else 'off'}",
            f"Tests: <code>{esc(test or 'not detected (/settest)')}</code>",
            f"Queued: {w.queue.qsize()} · Run mode: {self.config.run_mode} · Uptime: {up // 3600}h{(up % 3600) // 60:02d}m · v{__version__}",
        ]
        await self.reply(update, "\n".join(lines))

    async def cmd_resume(self, update: Update, ctx) -> None:
        w = self.worker(update)
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

    async def cmd_history(self, update: Update, ctx) -> None:
        w = self.worker(update)
        if not w.state.history:
            await self.reply(update, "No conversations yet.")
            return
        lines = [f"{'▶️' if c == w.state.conversation_id else '•'} <code>{c}</code>" for c in w.state.history]
        await self.reply(update, "<b>Recent conversations</b> (tap to copy, then /resume &lt;id&gt;)\n" + "\n".join(lines))

    # --------------------------------------------------------------- projects
    async def _switch(self, update: Update, w: ChatWorker, target: Path) -> None:
        try:
            target = target.resolve()
        except OSError:
            pass
        if not target.is_dir():
            await self.reply(update, f"❌ Not a folder: <code>{esc(target)}</code>")
            return
        if not self.config.is_path_allowed(target):
            await self.reply(update, "❌ That folder is outside ALLOWED_ROOTS.")
            return
        if w.busy:
            await self.reply(update, "⏳ A turn is running; /stop it first.")
            return
        conv = await w.switch_workspace(target)
        st = await gitops.status(target)
        branch = f" · 🌿 <code>{esc(st.branch)}</code>" if st else ""
        resumed = f"resuming conversation <code>{esc(conv[:8])}…</code>" if conv else "new conversation"
        await self.reply(update, f"📁 <b>{esc(target.name)}</b>{branch}\n<code>{esc(target)}</code>\n{resumed} (/new for a fresh one)")

    async def cmd_projects(self, update: Update, ctx) -> None:
        w = self.worker(update)
        refresh = bool(ctx.args and ctx.args[0] == "refresh")
        found = await asyncio.to_thread(self.project_list, refresh)
        if not found:
            await self.reply(update, "No projects found under ALLOWED_ROOTS (looked for .git, package.json, pyproject.toml, …).")
            return
        recent = [Path(p) for p in w.state.recent_projects if Path(p).is_dir()]
        ordered = recent + [p for p in found if p not in recent]
        rows: Buttons = []
        row: list[tuple[str, str]] = []
        for p in ordered[:40]:
            mark = "📂 " if str(p) == str(w.cwd) else ""
            row.append((f"{mark}{p.name}"[:30], w.action_token("project", str(p))))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        await self.reply(update, f"<b>Projects</b> ({len(found)}) · now in <code>{esc(w.cwd.name)}</code>\n<i>/projects refresh rescans</i>", buttons=rows)

    async def cmd_p(self, update: Update, ctx) -> None:
        w = self.worker(update)
        q = " ".join(ctx.args or []).strip()
        if not q:
            await self.cmd_projects(update, ctx)
            return
        hits = projects.match(await asyncio.to_thread(self.project_list), q)
        if not hits:
            hits = projects.match(await asyncio.to_thread(self.project_list, True), q)
        if len(hits) == 1:
            await self._switch(update, w, hits[0])
        elif not hits:
            await self.reply(update, f"No project matches <code>{esc(q)}</code>. Try /projects.")
        else:
            rows = [[(p.name[:30], w.action_token("project", str(p)))] for p in hits[:10]]
            await self.reply(update, "Which one?", buttons=rows)

    async def cmd_cwd(self, update: Update, ctx) -> None:
        w = self.worker(update)
        arg = " ".join(ctx.args).strip() if ctx.args else ""
        if not arg:
            roots = ", ".join(f"<code>{esc(r)}</code>" for r in self.config.allowed_roots)
            await self.reply(update, f"Project: <code>{esc(w.cwd)}</code>\nAllowed roots: {roots}")
            return
        if arg == "-":
            prev = [p for p in w.state.recent_projects if p != str(w.cwd)]
            if not prev:
                await self.reply(update, "No previous project.")
                return
            await self._switch(update, w, Path(prev[0]))
            return
        target = Path(arg).expanduser()
        if not target.is_absolute():
            target = w.cwd / target
        await self._switch(update, w, target)

    async def cmd_ls(self, update: Update, ctx) -> None:
        w = self.worker(update)
        target = self.resolve_path(w, " ".join(ctx.args)) if ctx.args else w.cwd
        if target is None or not target.is_dir():
            await self.reply(update, "❌ Not a folder I can show.")
            return
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            await self.reply(update, f"❌ {esc(exc)}")
            return
        shown = []
        for p in entries[:80]:
            if p.is_dir():
                shown.append(f"📁 {p.name}/")
            else:
                try:
                    size = _size(p.stat().st_size)
                except OSError:
                    size = "?"
                shown.append(f"📄 {p.name}  {size}")
        more = f"\n… and {len(entries) - 80} more" if len(entries) > 80 else ""
        await self.reply(update, f"<code>{esc(target)}</code>\n<pre>{esc(chr(10).join(shown) or '(empty)')}</pre>{more}")

    async def cmd_tree(self, update: Update, ctx) -> None:
        w = self.worker(update)
        depth = 2
        if ctx.args and ctx.args[0].isdigit():
            depth = max(1, min(6, int(ctx.args[0])))
        root = w.cwd
        files = None
        if await gitops.repo_root(root) is not None:
            r = await gitops.git(root, "ls-files", "--cached", "--others", "--exclude-standard")
            if r.ok:
                files = [l for l in r.out.splitlines() if l]
        text = await asyncio.to_thread(_tree, root, depth, files)
        await self.send_output(update, f"🌳 <b>{esc(root.name)}</b> (depth {depth})", text, "tree.txt", keep="head")

    async def cmd_cat(self, update: Update, ctx) -> None:
        w = self.worker(update)
        if not ctx.args:
            await self.reply(update, "Usage: /cat &lt;file&gt; [from-to], e.g. /cat src/app.py 40-80")
            return
        args = list(ctx.args)
        rng = None
        if len(args) > 1 and re.fullmatch(r"\d+(-\d*)?", args[-1]):
            rng = args.pop()
        path = self.resolve_path(w, " ".join(args))
        if path is None or not path.is_file():
            await self.reply(update, "❌ File not found (or outside the allowed folders).")
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            await self.reply(update, f"❌ {esc(exc)}")
            return
        if b"\0" in data[:8000]:
            await self.reply(update, "That's a binary file; use /get to download it.")
            return
        lines = data.decode("utf-8", errors="replace").splitlines()
        start, end = 1, len(lines)
        if rng:
            a, _, b = rng.partition("-")
            start = max(1, int(a))
            end = min(len(lines), int(b)) if b else (min(len(lines), start + 79) if "-" not in rng else len(lines))
        width = len(str(end))
        body = "\n".join(f"{i:>{width}}  {lines[i - 1]}" for i in range(start, end + 1))
        lang = _lang_for(path)
        rel = _rel(path, w.cwd)
        title = f"📄 <b>{esc(rel)}</b> · lines {start}-{end} of {len(lines)}"
        if len(body) > INLINE_OUTPUT:
            await self.messenger(update).send_document(data, path.name, caption=f"{rel} ({len(lines)} lines). Tip: /cat {rel} 1-80")
            return
        await self.messenger(update).send(f"{title}\n{pre(body, lang)}")

    async def cmd_get(self, update: Update, ctx) -> None:
        w = self.worker(update)
        if not ctx.args:
            await self.reply(update, "Usage: /get &lt;file&gt;")
            return
        path = self.resolve_path(w, " ".join(ctx.args))
        if path is None or not path.is_file():
            await self.reply(update, "❌ File not found (or outside the allowed folders).")
            return
        if path.stat().st_size > 50 * 1024 * 1024:
            await self.reply(update, "❌ Telegram bots can send files up to 50 MB.")
            return
        await self.messenger(update).send_document(path.read_bytes(), path.name, caption=_rel(path, w.cwd))

    async def cmd_find(self, update: Update, ctx) -> None:
        w = self.worker(update)
        q = " ".join(ctx.args or []).strip().lower()
        if not q:
            await self.reply(update, "Usage: /find &lt;part of a file name&gt;")
            return
        r = await gitops.git(w.cwd, "ls-files", "--cached", "--others", "--exclude-standard")
        if r.ok:
            names = r.out.splitlines()
        else:
            names = await asyncio.to_thread(_walk_names, w.cwd)
        hits = [n for n in names if q in n.lower()]
        if not hits:
            await self.reply(update, f"No files match <code>{esc(q)}</code>.")
            return
        await self.send_output(update, f"🔎 {len(hits)} file(s) matching <code>{esc(q)}</code>", "\n".join(hits[:500]), "find.txt", keep="head")

    async def cmd_grep(self, update: Update, ctx) -> None:
        w = self.worker(update)
        q = " ".join(ctx.args or []).strip()
        if not q:
            await self.reply(update, "Usage: /grep &lt;text or regex&gt;")
            return
        r = await gitops.git(w.cwd, "grep", "-n", "-I", "--untracked", "-E", "-e", q, timeout=60)
        if r.code == 1 and not r.out.strip():
            await self.reply(update, f"No matches for <code>{esc(q)}</code>.")
            return
        if r.code not in (0, 1):
            from .procs import run_argv

            r = await run_argv(["rg", "-n", "--no-heading", "-e", q], w.cwd, timeout=60)
        lines = r.out.splitlines()
        await self.send_output(update, f"🔎 {len(lines)} match(es) for <code>{esc(q)}</code>", "\n".join(lines[:800]), "grep.txt", keep="head")

    # -------------------------------------------------------------------- git
    async def _repo(self, update: Update, w: ChatWorker) -> Path | None:
        root = await gitops.repo_root(w.cwd)
        if root is None:
            await self.reply(update, "This project isn't a git repository.")
        return root

    async def cmd_git(self, update: Update, ctx) -> None:
        w = self.worker(update)
        root = await self._repo(update, w)
        if root is None:
            return
        st = await gitops.status(root)
        if st is None:
            await self.reply(update, "❌ git status failed.")
            return
        last = await gitops.git(root, "log", "-1", "--format=%h %s (%cr)")
        text = gitops.format_status(st) + (f"\n🕑 {last.out.strip()}" if last.ok and last.out.strip() else "")
        buttons: Buttons = []
        if not st.clean:
            buttons.append([("📄 Diff", w.action_token("cmd", "diff"))])
        await self.messenger(update).send(f"<b>{esc(root.name)}</b>\n{pre(text)}", buttons=buttons or None)

    async def act_cmd(self, update, w, payload) -> None:
        if payload == "diff":
            root = await gitops.repo_root(w.cwd)
            if root:
                await self._send_diff(update, root, [])

    async def cmd_diff(self, update: Update, ctx) -> None:
        w = self.worker(update)
        args = list(ctx.args or [])
        if args[:1] == ["turn"]:
            await self._send_turn_diff(update, w)
            return
        root = await self._repo(update, w)
        if root is None:
            return
        await self._send_diff(update, root, args)

    async def _send_diff(self, update: Update, root: Path, args: list[str]) -> None:
        if args[:1] in (["staged"], ["--staged"], ["--cached"]):
            git_args = ["diff", "--cached"]
            paths = args[1:]
        else:
            git_args = ["diff", "HEAD"] if await gitops.head(root) else ["diff"]
            paths = args
        stat = await gitops.git(root, *git_args, "--stat", "--", *paths)
        r = await gitops.git(root, *git_args, "--", *paths)
        new = sorted(await gitops.untracked(root)) if not paths and git_args[1:2] != ["--cached"] else []
        if not r.out.strip() and not new:
            await self.reply(update, "No changes.")
            return
        extra = ""
        if new:
            extra = "\n\nNew (untracked) files:\n" + "\n".join(f"  + {n}" for n in new[:50])
        await self._deliver_diff(update, stat.out.strip() + extra, r.out)

    async def _send_turn_diff(self, update: Update, w: ChatWorker) -> None:
        rec = w.last_turn
        if rec is None or rec.checkpoint is None:
            await self.reply(update, "No checkpoint for the last turn (not a git repo, or no turn yet).")
            return
        if rec.undone:
            await self.reply(update, "That turn was undone.")
            return
        diff = await gitops.diff_since(rec.checkpoint)
        if not diff.strip():
            await self.reply(update, "The last turn didn't change any files.")
            return
        ch = await gitops.changes_since(rec.checkpoint)
        summary = ch.headline() + "\n" + "\n".join(f"  {s} {p}" for s, p in ch.files[:40])
        buttons = [[("↩️ Undo this turn", w.action_token("undo"))]] if not ch.head_moved else None
        await self._deliver_diff(update, summary, diff, buttons)

    async def _deliver_diff(self, update: Update, summary: str, diff: str, buttons: Buttons | None = None) -> None:
        m = self.messenger(update)
        if len(diff) + len(summary) < INLINE_OUTPUT:
            await m.send(f"{pre(summary)}\n{pre(diff, 'diff')}", buttons=buttons)
            return
        await m.send_document(diff.encode("utf-8"), f"changes-{time.strftime('%H%M%S')}.diff", caption="Full diff")
        await m.send(pre(summary[-INLINE_OUTPUT:]), buttons=buttons)

    async def cmd_log(self, update: Update, ctx) -> None:
        w = self.worker(update)
        root = await self._repo(update, w)
        if root is None:
            return
        n = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 15
        r = await gitops.git(root, "log", f"-{min(n, 200)}", "--format=%h %ad %an%n   %s", "--date=short", "--decorate")
        await self.send_output(update, f"🕑 <b>{esc(root.name)}</b> last {n} commits", r.out, "log.txt", keep="head")

    async def cmd_branches(self, update: Update, ctx) -> None:
        w = self.worker(update)
        root = await self._repo(update, w)
        if root is None:
            return
        r = await gitops.git(root, "branch", "-vv", "--sort=-committerdate")
        await self.send_output(update, "🌿 <b>Branches</b> (ask agy to switch, e.g. “checkout feature-x”)", r.out, "branches.txt", keep="head")

    async def _ask_undo(self, update: Update, w: ChatWorker) -> None:
        rec = w.last_turn
        if rec is None or rec.checkpoint is None:
            await self.reply(update, "Nothing to undo: no checkpoint for the last turn (not a git repo, or no turn yet).")
            return
        if rec.undone:
            await self.reply(update, "Already undone.")
            return
        ch = await gitops.changes_since(rec.checkpoint)
        if ch.empty:
            await self.reply(update, "The last turn didn't change any files.")
            return
        if ch.head_moved:
            await self.reply(update, "agy committed during that turn, so I won't rewrite history automatically. Use <code>/sh git reset --soft HEAD~1</code> (if shell is on) or ask agy.")
            return
        files = "\n".join(f"  {s} {p}" for s, p in ch.files[:30])
        buttons = [[("↩️ Yes, revert these", w.action_token("undo_yes", rec)), ("Keep", w.action_token("dismiss"))]]
        await self.messenger(update).send(f"Revert the last turn? {esc(ch.headline())}\n{pre(files)}\nYour own edits made during that turn would be reverted too.", buttons=buttons)

    async def cmd_undo(self, update: Update, ctx) -> None:
        await self._ask_undo(update, self.worker(update))

    async def cmd_commit(self, update: Update, ctx) -> None:
        w = self.worker(update)
        root = await self._repo(update, w)
        if root is None:
            return
        message = " ".join(ctx.args or []).strip()
        if not message:
            await self.reply(update, "Usage: /commit &lt;message&gt; (commits all changes). Or /commitmsg to let agy write the message.")
            return
        st = await gitops.status(root)
        if st is None or st.clean:
            await self.reply(update, "Nothing to commit.")
            return
        stat = await gitops.git(root, "diff", "HEAD", "--shortstat")
        n = len(set(st.staged + st.unstaged)) + len(st.untracked)
        buttons = [[("✅ Commit", w.action_token("commit_yes", (root, message))), ("Cancel", w.action_token("dismiss"))]]
        await self.reply(update, f"Commit all changes on <code>{esc(st.branch)}</code> ({n} file(s){', ' + esc(stat.out.strip()) if stat.out.strip() else ''})?\n<i>{esc(message)}</i>", buttons)

    async def cmd_push(self, update: Update, ctx) -> None:
        w = self.worker(update)
        root = await self._repo(update, w)
        if root is None:
            return
        st = await gitops.status(root)
        if st is None:
            return
        if st.upstream and not st.ahead:
            await self.reply(update, "Nothing to push: up to date with " + esc(st.upstream))
            return
        log_r = await gitops.git(root, "log", "--oneline", "-10", "@{u}..HEAD" if st.upstream else "HEAD")
        target = st.upstream or f"origin/{st.branch} (new upstream)"
        buttons = [[("⬆️ Push", w.action_token("push_yes", (root, not st.upstream))), ("Cancel", w.action_token("dismiss"))]]
        await self.messenger(update).send(f"Push <code>{esc(st.branch)}</code> → <code>{esc(target)}</code>?\n{pre(log_r.out.strip() or '(commits)')}", buttons=buttons)

    async def cmd_pull(self, update: Update, ctx) -> None:
        w = self.worker(update)
        root = await self._repo(update, w)
        if root is None:
            return
        if w.busy:
            await self.reply(update, "⏳ agy is working; /stop it first.")
            return
        r = await gitops.git(root, "pull", "--ff-only", timeout=180)
        await self.send_output(update, "⬇️ <b>Pulled</b>" if r.ok else "❌ <b>Pull failed</b> (fast-forward only; ask agy to merge/rebase)", r.out)

    # ---------------------------------------------------------------- run
    async def cmd_test(self, update: Update, ctx) -> None:
        w = self.worker(update)
        cmd = " ".join(ctx.args or []).strip() or self.test_command(w)
        if not cmd:
            await self.reply(update, "I couldn't detect a test command. Set one with /settest &lt;cmd&gt; (or TEST_COMMAND in .env).")
            return
        if ctx.args and not self.config.allow_shell:
            await self.reply(update, "🔒 Custom commands need ALLOW_SHELL=true. Use /settest to save one (that's stored, not run), then /test.")
            return
        note = await self.reply(update, f"🧪 Running <code>{esc(cmd)}</code>…")
        started = time.monotonic()
        typing = asyncio.create_task(_typing_loop(self.messenger(update)))
        try:
            r = await run_shell(cmd, w.cwd, timeout=self.config.test_timeout, shell=self.config.shell)
        finally:
            typing.cancel()
        secs = int(time.monotonic() - started)
        if note:
            try:
                await note.delete()
            except Exception:
                pass
        if r.ok:
            await self.send_output(update, f"✅ <b>Tests passed</b> · {secs}s · <code>{esc(cmd)}</code>", _tail_lines(r.out, 25), "test-output.txt")
        else:
            label = "⏱ <b>Tests timed out</b>" if r.timed_out else f"❌ <b>Tests failed</b> (exit {r.code})"
            buttons = [[("🛠 Ask agy to fix", w.action_token("fix_tests", (cmd, r.out)))]]
            await self.send_output(update, f"{label} · {secs}s · <code>{esc(cmd)}</code>", r.out, "test-output.txt", buttons=buttons)

    async def cmd_settest(self, update: Update, ctx) -> None:
        w = self.worker(update)
        cmd = " ".join(ctx.args or []).strip()
        if not cmd:
            await self.reply(update, f"Test command here: <code>{esc(self.test_command(w) or 'none')}</code>\nSet: /settest &lt;cmd&gt; · reset to auto: /settest auto")
            return
        if cmd == "auto":
            w.state.test_commands.pop(str(w.cwd), None)
        else:
            w.state.test_commands[str(w.cwd)] = cmd
        self.store.save()
        await self.reply(update, f"🧪 /test in <b>{esc(w.cwd.name)}</b> runs <code>{esc(self.test_command(w) or 'nothing (none detected)')}</code>")

    async def cmd_sh(self, update: Update, ctx) -> None:
        msg = update.effective_message
        text = (msg.text or "").split(None, 1)
        await self._shell(update, text[1].strip() if len(text) > 1 else "")

    async def _shell(self, update: Update, command: str) -> None:
        if not self.config.allow_shell:
            await self.reply(update, "🔒 Shell commands are off. Set ALLOW_SHELL=true in .env to run them directly with /sh or !cmd (agy can still run commands for you).")
            return
        if not command:
            await self.reply(update, "Usage: /sh &lt;command&gt; or !&lt;command&gt;, e.g. <code>!git log --oneline -5</code>")
            return
        w = self.worker(update)
        typing = asyncio.create_task(_typing_loop(self.messenger(update)))
        started = time.monotonic()
        try:
            r: ProcResult = await run_shell(command, w.cwd, timeout=self.config.shell_timeout, shell=self.config.shell)
        finally:
            typing.cancel()
        secs = time.monotonic() - started
        status = "⏱ timed out" if r.timed_out else ("✅" if r.ok else f"❌ exit {r.code}")
        buttons = None if r.ok else [[("🤖 Ask agy about this", w.action_token("explain_output", (command, r.out)))]]
        await self.send_output(update, f"<code>$ {esc(command[:200])}</code> · {status} · {secs:.1f}s", r.out, "shell-output.txt", buttons=buttons)

    # ------------------------------------------------------------- shortcuts
    async def cmd_shortcut(self, update: Update, ctx) -> None:
        msg = update.effective_message
        name = (msg.text or "").split()[0][1:].split("@")[0].lower()
        sc = self.shortcuts.get(name)
        if sc is None:
            return
        w = self.worker(update)
        args = (msg.text or "").split(None, 1)
        argtext = args[1] if len(args) > 1 else ""
        if msg.reply_to_message and (msg.reply_to_message.text or msg.reply_to_message.caption):
            argtext = (argtext + "\n\n" if argtext else "") + (msg.reply_to_message.text or msg.reply_to_message.caption)
        overrides = {"mode": sc.mode} if sc.mode else {}
        await self._enqueue(update, Job(prompt=sc.render(argtext, str(w.cwd)), overrides=overrides, label=f"/{name}", display=msg.text or f"/{name}"))

    async def cmd_prompts(self, update: Update, ctx) -> None:
        lines = [f"/{s.name} – {esc(s.description)}" + (f" <i>({s.mode} mode)</i>" if s.mode else "") for s in self.shortcuts.values()]
        await self.reply(update, "<b>Shortcuts</b> (add args after the command, or reply to a message to include it)\n" + "\n".join(lines)
                         + f"\n\nAdd your own in <code>{esc(self.config.prompts_file)}</code> (see prompts.example.toml), then /restart.")

    # -------------------------------------------------------------- settings
    async def _set_option(self, update: Update, ctx, field: str, valid: set[str] | None, label: str) -> None:
        w = self.worker(update)
        arg = (ctx.args[0].strip() if ctx.args else "")
        current = getattr(w.options(), field) or "default"
        if not arg:
            buttons = None
            if valid is not None:
                opts = sorted(valid - {""}) + ["default"]
                buttons = [[((("● " if (v == current) else "") + v), w.action_token("set_opt", (field, "" if v == "default" else v))) for v in opts[i:i + 3]]
                           for i in range(0, len(opts), 3)]
            await self.reply(update, f"{label}: <code>{esc(current)}</code>", buttons)
            return
        value = "" if arg.lower() == "default" else arg
        if valid is not None and value not in valid:
            await self.reply(update, f"❌ {label} must be one of: " + ", ".join(sorted(v or "default" for v in valid)))
            return
        setattr(w.state, field, value)
        self.store.save()
        await w.invalidate()
        await self.reply(update, f"✅ {label} → <code>{esc(value or 'default')}</code> (applies from the next message)")

    async def cmd_model(self, update, ctx):
        await self._set_option(update, ctx, "model", None, "Model")

    async def cmd_effort(self, update, ctx):
        await self._set_option(update, ctx, "effort", VALID_EFFORTS, "Effort")

    async def cmd_mode(self, update, ctx):
        await self._set_option(update, ctx, "mode", VALID_MODES - {"default"}, "Mode")

    async def cmd_yolo(self, update: Update, ctx) -> None:
        w = self.worker(update)
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

    async def _add_rule(self, update: Update, w: ChatWorker, rule: str, retry: bool = False) -> None:
        path = self.config.agy_settings_path
        try:
            added = await asyncio.to_thread(agysettings.add_allow_rule, path, rule)
        except agysettings.SettingsError as exc:
            await self.reply(update, f"❌ {esc(exc)}")
            return
        await w.invalidate()  # agy reads settings at start
        await self.reply(update, (f"📌 Added <code>{esc(rule)}</code>" if added else f"<code>{esc(rule)}</code> was already allowed")
                         + f" in <code>{esc(path)}</code> (backup saved).")
        if retry:
            await self._enqueue(update, Job(prompt="You were blocked from running a tool in your last turn. It's allowed now: retry what was blocked and finish the task.", label="retry", display="(retry after allow rule)"))

    async def cmd_allow(self, update: Update, ctx) -> None:
        w = self.worker(update)
        path = self.config.agy_settings_path
        arg = " ".join(ctx.args or []).strip()
        if arg.startswith("-") and len(arg) > 1:
            try:
                removed = await asyncio.to_thread(agysettings.remove_allow_rule, path, arg[1:].strip())
            except agysettings.SettingsError as exc:
                await self.reply(update, f"❌ {esc(exc)}")
                return
            await w.invalidate()
            await self.reply(update, "🗑 Removed." if removed else "That rule isn't in the list.")
            return
        if arg:
            await self._add_rule(update, w, arg)
            return
        try:
            rules = await asyncio.to_thread(agysettings.allow_rules, path)
        except agysettings.SettingsError as exc:
            await self.reply(update, f"❌ {esc(exc)}")
            return
        body = "\n".join(rules) or "(none)"
        await self.reply(update, f"<b>agy allow rules</b> · <code>{esc(path)}</code>\n{pre(body)}\nAdd: <code>/allow command(npm)</code> · remove: <code>/allow -command(npm)</code>\nRegex: <code>/allow command(regex:npm (run|test).*)</code>")

    # ------------------------------------------------------------------ bridge
    async def _run_cli(self, update: Update, args: list[str], title: str) -> None:
        w = self.worker(update)
        code, out = await run_simple(self.config.agy_command, args, w.cwd, timeout=90)
        await self.send_output(update, f"<b>{title}</b>" + ("" if code == 0 else f" (exit {code})"), out)

    async def cmd_models(self, update, ctx):
        await self._run_cli(update, ["models"], "agy models")

    async def cmd_usage(self, update, ctx):
        await self._run_cli(update, ["-p", "/usage"], "agy usage")

    async def cmd_logs(self, update: Update, ctx) -> None:
        path = Path("data") / "bridge.log"
        if not path.exists():
            await self.reply(update, "No log file yet.")
            return
        n = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 40
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-min(n, 400):]
        text = re.sub(r"bot\d+:[A-Za-z0-9_-]{30,}", "bot<token>", "\n".join(lines))
        await self.send_output(update, "📜 <b>bridge.log</b>", text, "bridge-log.txt")

    def _request_exit(self, code: int) -> None:
        self.exit_code = code
        assert self.app is not None
        loop = asyncio.get_running_loop()
        loop.call_later(1.0, self.app.stop_running)

    async def cmd_restart(self, update: Update, ctx) -> None:
        w = self.worker(update)
        busy = [x for x in self.workers.values() if x.busy]
        if busy and not (ctx.args and ctx.args[0] == "now"):
            await self.reply(update, f"{len(busy)} agy turn(s) running. Restart anyway?", [[("🔄 Restart", w.action_token("restart_yes")), ("Cancel", w.action_token("dismiss"))]])
            return
        await self.act_restart_yes(update, w, None)

    async def cmd_update(self, update: Update, ctx) -> None:
        if not (BRIDGE_DIR / ".git").exists():
            await self.reply(update, "The bridge wasn't installed from git, so I can't update it in place.")
            return
        await self.reply(update, "⬇️ Updating the bridge…")
        r = await gitops.git(BRIDGE_DIR, "pull", "--ff-only", timeout=120)
        if not r.ok:
            await self.send_output(update, "❌ <b>git pull failed</b>", r.out)
            return
        if "Already up to date" in r.out:
            await self.reply(update, "✅ Already up to date.")
            return
        from .procs import run_argv

        pip = await run_argv([sys.executable, "-m", "pip", "install", "-q", "-e", str(BRIDGE_DIR)], BRIDGE_DIR, timeout=300)
        await self.send_output(update, "✅ <b>Updated</b>" + ("" if pip.ok else " (pip reported problems)"), r.out + ("\n" + pip.out if pip.out.strip() else ""))
        await self.reply(update, "🔄 Restarting to load the new version…")
        self._request_exit(RESTART_CODE)


# ---------------------------------------------------------------------- utils
def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _rel(path: Path, cwd: Path) -> str:
    try:
        return str(path.relative_to(cwd.resolve()))
    except ValueError:
        return str(path)


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


_LANGS = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".json": "json", ".md": "markdown", ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php",
    ".sh": "bash", ".ps1": "powershell", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".html": "html",
    ".css": "css", ".sql": "sql", ".swift": "swift", ".dart": "dart", ".vue": "vue", ".xml": "xml",
}


def _lang_for(path: Path) -> str:
    return _LANGS.get(path.suffix.lower(), "")


def _tail_lines(text: str, n: int) -> str:
    return "\n".join(text.rstrip().splitlines()[-n:])


def _walk_names(root: Path, limit: int = 20000) -> list[str]:
    out = []
    for p in root.rglob("*"):
        if any(part in projects.SKIP_DIRS or part.startswith(".") for part in p.relative_to(root).parts[:-1]):
            continue
        if p.is_file():
            out.append(str(p.relative_to(root)).replace("\\", "/"))
            if len(out) >= limit:
                break
    return out


def _tree(root: Path, depth: int, files: list[str] | None) -> str:
    """Directory tree. Uses git's file list when available (respects .gitignore)."""
    if files is None:
        files = _walk_names(root)
    tree: dict = {}
    for f in files:
        parts = f.replace("\\", "/").split("/")
        node = tree
        for part in parts[:depth]:
            node = node.setdefault(part, {})
        if len(parts) > depth:
            node["…"] = node.get("…", 0) + 1 if isinstance(node.get("…"), int) else 1

    lines: list[str] = [f"{root.name}/"]

    def walk(node: dict, prefix: str) -> None:
        items = sorted(node.items(), key=lambda kv: (kv[0] == "…", not (isinstance(kv[1], dict) and kv[1]), kv[0].lower()))
        for i, (name, child) in enumerate(items):
            last = i == len(items) - 1
            branch = "└── " if last else "├── "
            if name == "…":
                lines.append(f"{prefix}{branch}… {child} more")
                continue
            is_dir = isinstance(child, dict) and bool(child)
            lines.append(f"{prefix}{branch}{name}{'/' if is_dir else ''}")
            if is_dir:
                walk(child, prefix + ("    " if last else "│   "))

    walk(tree, "")
    return "\n".join(lines[:1500])


async def _typing_loop(m: TelegramMessenger) -> None:
    try:
        while True:
            try:
                await m.typing()
            except Exception:
                pass
            await asyncio.sleep(4.5)
    except asyncio.CancelledError:
        return


def run(config: Config) -> int:
    bridge = Bridge(config)
    app = bridge.build()
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    return bridge.exit_code
