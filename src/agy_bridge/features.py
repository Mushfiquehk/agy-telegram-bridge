"""Bigger workflows on top of the bridge: background tasks, PRs and CI,
schedules, session/queue pickers. Mixed into bot.Bridge."""

from __future__ import annotations

import asyncio
import html
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telegram import Update

from . import ghcli, gitops, tasks
from .core import Buttons, ChatWorker, Job
from .schedules import Schedule, parse_at, parse_interval

if TYPE_CHECKING:  # pragma: no cover
    from .bot import Bridge

log = logging.getLogger(__name__)


def esc(s: Any) -> str:
    return html.escape(str(s), quote=False)


def pre(text: str, lang: str = "") -> str:
    cls = f' class="language-{lang}"' if lang else ""
    return f"<pre><code{cls}>{esc(text)}</code></pre>"


class FeaturesMixin:
    # attributes provided by Bridge
    tasks: dict[str, tasks.Task]
    schedules: dict[str, Schedule]

    def _init_features(self: "Bridge") -> None:
        self.tasks = {}
        self.schedules = {}
        self._watchers: set[asyncio.Task] = set()
        for raw in self.store.extras.get("tasks", []):
            try:
                t = tasks.Task(**raw)
            except TypeError:
                continue
            if t.status == "running":
                t.status = "interrupted"
            if t.status not in ("discarded", "merged") and Path(t.path).exists():
                self.tasks[t.id] = t
        for raw in self.store.extras.get("schedules", []):
            try:
                s = Schedule(**raw)
            except TypeError:
                continue
            self.schedules[s.id] = s

    def _save_extras(self: "Bridge") -> None:
        self.store.extras["tasks"] = [t.to_dict() for t in self.tasks.values()]
        self.store.extras["schedules"] = [s.to_dict() for s in self.schedules.values()]
        self.store.save()

    def _spawn(self: "Bridge", coro) -> None:
        t = asyncio.create_task(coro)
        self._watchers.add(t)
        t.add_done_callback(self._watchers.discard)

    # ============================================================ tasks
    def _task_worker(self: "Bridge", task: tasks.Task, chat_id: int, thread: int | None) -> ChatWorker:
        key = f"task:{task.id}"
        w = self.workers.get(key)
        if w is None:
            w = self.make_worker(chat_id, thread, key)
            w.state.cwd = task.path
            w.title = f"🧵 {task.name}"
            w.on_turn_done = self._task_turn_done
            w.task_id = task.id  # type: ignore[attr-defined]
        return w

    def _task_buttons(self: "Bridge", w: ChatWorker, task: tasks.Task) -> Buttons:
        base = task.base_branch if task.base_branch != "HEAD" else "main checkout"
        return [
            [("📄 Diff", w.action_token("task_diff", task.id)), ("🔀 Merge into " + base[:16], w.action_token("task_merge", task.id))],
            [("⬆️ Open PR", w.action_token("task_pr", task.id)), ("🗑 Discard", w.action_token("task_discard", task.id))],
        ]

    async def cmd_task(self: "Bridge", update: Update, ctx) -> None:
        msg = update.effective_message
        parts = (msg.text or "").split(None, 1)
        prompt = parts[1].strip() if len(parts) > 1 else ""
        if msg.reply_to_message and (msg.reply_to_message.text or msg.reply_to_message.caption):
            prompt = (prompt + "\n\n" if prompt else "") + (msg.reply_to_message.text or msg.reply_to_message.caption)
        if not prompt:
            await self.reply(update, "Usage: /task &lt;what to do&gt;\nRuns in the background on its own branch and worktree, so your checkout and this chat stay free. /tasks lists them.")
            return
        running = [t for t in self.tasks.values() if t.status == "running"]
        if len(running) >= self.config.max_parallel_tasks:
            await self.reply(update, f"⏳ {len(running)} tasks are already running (MAX_PARALLEL_TASKS={self.config.max_parallel_tasks}). /tasks")
            return
        main = self.worker(update)
        chat_id, thread, key = self._key(update)
        task_id = uuid.uuid4().hex[:5]
        try:
            task = await tasks.create(main.cwd, prompt, task_id, key)
        except tasks.TaskError as exc:
            await self.reply(update, f"❌ {esc(exc)}")
            return
        self.tasks[task.id] = task
        self._save_extras()
        w = self._task_worker(task, chat_id, thread)
        await w.messenger.send(
            f"🧵 <b>Task {esc(task.name)}</b> started on branch <code>{esc(task.branch)}</code>\n"
            f"<i>Worktree: {esc(task.path)}</i>\nReply to any of this task's messages to steer it.",
            silent=True,
        )
        await w.submit(Job(prompt=prompt + "\n\n(When you finish, summarize what you changed and whether tests pass. "
                                           "Don't push; the bridge commits your work to this branch.)", display=prompt))

    async def _task_turn_done(self: "Bridge", w: ChatWorker, record, ok: bool) -> None:
        task = self.tasks.get(getattr(w, "task_id", ""))
        if task is None:
            return
        committed, out = await tasks.commit_all(task, f"agy: {task.prompt[:68]}")
        stat, _ = await tasks.diff(task)
        task.status = "done" if ok else "failed"
        task.summary = (record.response or "")[:500]
        self._save_extras()
        headline = "✅ finished" if ok else "⚠️ ended with errors"
        changes = stat.strip().splitlines()[-1] if stat.strip() else "no changes"
        body = f"🧵 <b>{esc(task.name)}</b> {headline} · <code>{esc(task.branch)}</code>\n{esc(changes)}"
        if not committed:
            body += f"\n⚠️ Commit failed:\n{pre(out[-800:])}"
        await w.messenger.send(body, buttons=self._task_buttons(w, task))

    async def cmd_tasks(self: "Bridge", update: Update, ctx) -> None:
        main = self.worker(update)
        live = [t for t in self.tasks.values() if t.status not in ("discarded", "merged")]
        if not live:
            await self.reply(update, "No background tasks. Start one with /task &lt;what to do&gt;.")
            return
        icons = {"running": "🟡", "done": "✅", "failed": "⚠️", "interrupted": "⏸", "pr": "⬆️"}
        for t in sorted(live, key=lambda t: t.created):
            age = int((time.time() - t.created) // 60)
            line = f"{icons.get(t.status, '•')} <b>{esc(t.name)}</b> · {t.status} · {age}m ago\n<code>{esc(t.branch)}</code>"
            if t.pr_url:
                line += f"\n{esc(t.pr_url)}"
            buttons = None if t.status == "running" else self._task_buttons(main, t)
            if t.status == "running":
                tw = self.workers.get(f"task:{t.id}")
                if tw:
                    buttons = [[("⏹ Stop", tw.action_token("stop"))]]
            await self.messenger(update).send(line, buttons=buttons, silent=True)

    async def act_task_diff(self: "Bridge", update, w, task_id) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            await self.reply(update, "That task is gone.")
            return
        stat, full = await tasks.diff(task)
        if not full.strip():
            await self.reply(update, "No changes on that branch.")
            return
        await self._deliver_diff(update, stat.strip(), full)

    async def act_task_merge(self: "Bridge", update, w, task_id) -> None:
        task = self.tasks.get(task_id)
        if task is None or task.status == "running":
            await self.reply(update, "That task is gone or still running.")
            return
        ok, out = await tasks.merge(task)
        if ok:
            task.status = "merged"
            tw = self.workers.pop(f"task:{task.id}", None)
            if tw:
                await tw.shutdown()  # release the worktree folder (Windows locks it)
            await tasks.remove(task, delete_branch=True)
            self.tasks.pop(task.id, None)
            self._save_extras()
            await self._drop_buttons(update)
        await self.send_output(update, f"🔀 <b>Merged {esc(task.name)}</b> and cleaned up the worktree" if ok else "❌ <b>Merge failed</b>", out)

    async def act_task_pr(self: "Bridge", update, w, task_id) -> None:
        task = self.tasks.get(task_id)
        if task is None or task.status == "running":
            await self.reply(update, "That task is gone or still running.")
            return
        await self.reply(update, "⬆️ Pushing and opening a draft PR…")
        body = f"{task.prompt}\n\n---\n{task.summary}\n\n_Opened from Telegram via agy-telegram-bridge._"
        res = await ghcli.create_pr(Path(task.path), task.branch, title=task.prompt.splitlines()[0][:72], body=body)
        if res.ok:
            task.status = "pr"
            task.pr_url = res.url
            self._save_extras()
            await self.messenger(update).send(f"⬆️ <b>PR for {esc(task.name)}</b>\n{esc(res.url)}")
            self._spawn(self._watch_ci(update, Path(task.path), task.branch))
        else:
            await self.send_output(update, "❌ <b>Couldn't open the PR</b>", res.output)

    async def act_task_discard(self: "Bridge", update, w, task_id) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            await self.reply(update, "That task is gone.")
            return
        await self.messenger(update).send(f"Discard <b>{esc(task.name)}</b>? This deletes branch <code>{esc(task.branch)}</code> and its worktree.",
                                          buttons=[[("🗑 Yes, discard", w.action_token("task_discard_yes", task_id)), ("Keep", w.action_token("dismiss"))]])

    async def act_task_discard_yes(self: "Bridge", update, w, task_id) -> None:
        await self._drop_buttons(update)
        task = self.tasks.pop(task_id, None)
        if task is None:
            return
        tw = self.workers.pop(f"task:{task.id}", None)
        if tw:
            await tw.reset()
            await tw.shutdown()
        out = await tasks.remove(task, delete_branch=True)
        task.status = "discarded"
        self._save_extras()
        await self.reply(update, f"🗑 Discarded {esc(task.name)}." + (f"\n{pre(out)}" if out else ""))

    # ============================================================ PR / CI
    async def cmd_pr(self: "Bridge", update: Update, ctx) -> None:
        w = self.worker(update)
        root = await self._repo(update, w)
        if root is None:
            return
        st = await gitops.status(root)
        if st is None or not st.branch or st.branch == "(detached)":
            await self.reply(update, "Check out a branch first.")
            return
        title = " ".join(ctx.args or []).strip()
        extra = "" if ghcli.available() else "\n<i>gh CLI isn't installed, so I'll push and send you the compare link.</i>"
        dirty = "\n⚠️ You have uncommitted changes; they won't be in the PR (/commit first)." if not st.clean else ""
        await self.reply(update, f"Push <code>{esc(st.branch)}</code> and open a draft PR{(': <i>' + esc(title) + '</i>') if title else ' (title from commits)'}?{dirty}{extra}",
                         [[("⬆️ Open PR", w.action_token("pr_yes", (str(root), st.branch, title))), ("Cancel", w.action_token("dismiss"))]])

    async def act_pr_yes(self: "Bridge", update, w, payload) -> None:
        await self._drop_buttons(update)
        root, branch, title = payload
        res = await ghcli.create_pr(Path(root), branch, title=title)
        if res.ok:
            await self.messenger(update).send(f"⬆️ <b>Pull request</b>\n{esc(res.url)}")
            self._spawn(self._watch_ci(update, Path(root), branch))
        else:
            await self.send_output(update, "❌ <b>Couldn't open the PR</b>", res.output)

    async def cmd_ci(self: "Bridge", update: Update, ctx) -> None:
        w = self.worker(update)
        root = await self._repo(update, w)
        if root is None:
            return
        if not ghcli.available():
            await self.reply(update, "/ci needs the GitHub CLI on the PC: <code>winget install GitHub.cli</code>, then <code>gh auth login</code>.")
            return
        st = await gitops.status(root)
        rs = await ghcli.runs(root, branch=st.branch if st else "", limit=6)
        if not rs:
            await self.reply(update, "No workflow runs for this branch.")
            return
        lines = [f"{r.icon} {esc(r.name)} · {esc(r.title[:50])}\n   {esc(r.url)}" for r in rs]
        buttons = None
        failed = next((r for r in rs if r.done and r.conclusion == "failure"), None)
        if failed:
            buttons = [[("🛠 Ask agy to fix CI", w.action_token("fix_ci", (str(root), failed.url)))]]
        await self.messenger(update).send(f"<b>CI · {esc(st.branch if st else '')}</b>\n" + "\n".join(lines), buttons=buttons)

    async def _watch_ci(self: "Bridge", update: Update, root: Path, branch: str, timeout: int = 2400) -> None:
        """After a push: wait for the commit's workflow runs and report once."""
        if not ghcli.available():
            return
        sha = await gitops.head(root)
        w = self.worker(update)
        deadline = time.monotonic() + timeout
        seen_any = False
        await asyncio.sleep(20)
        while time.monotonic() < deadline:
            rs = await ghcli.runs(root, commit=sha, limit=10)
            if rs:
                seen_any = True
                if all(r.done for r in rs):
                    bad = [r for r in rs if r.conclusion not in ("success", "skipped", "neutral")]
                    lines = "\n".join(f"{r.icon} {esc(r.name)} {esc(r.url)}" for r in rs)
                    buttons = [[("🛠 Ask agy to fix CI", w.action_token("fix_ci", (str(root), bad[0].url)))]] if bad else None
                    await w.messenger.send(f"{'❌ <b>CI failed</b>' if bad else '✅ <b>CI passed</b>'} · <code>{esc(branch)}</code> @ {sha[:7]}\n{lines}", buttons=buttons)
                    return
            elif not seen_any and time.monotonic() > deadline - timeout + 180:
                return  # no workflows for this repo
            await asyncio.sleep(30)

    async def act_fix_ci(self: "Bridge", update, w: ChatWorker, payload) -> None:
        await self._drop_buttons(update)
        root, url = payload
        logs = await ghcli.failed_log(Path(root), url)
        prompt = (f"CI failed: {url}\nFind the cause and fix it locally, run the relevant tests, and commit the fix (don't push).\n\n"
                  f"Failed job logs (tail):\n```\n{logs or '(could not fetch logs; use `gh run view --log-failed`)'}\n```")
        await self._enqueue(update, Job(prompt=prompt, label="fix CI", display=f"(fix CI: {url})"))

    # ============================================================ schedules
    async def cmd_every(self: "Bridge", update: Update, ctx) -> None:
        await self._add_schedule(update, ctx, daily=False)

    async def cmd_at(self: "Bridge", update: Update, ctx) -> None:
        await self._add_schedule(update, ctx, daily=True)

    async def _add_schedule(self: "Bridge", update: Update, ctx, daily: bool) -> None:
        args = list(ctx.args or [])
        usage = "Usage: /at 09:00 &lt;prompt&gt; (daily, PC's local time)" if daily else "Usage: /every 2h &lt;prompt&gt; (5m minimum)"
        if len(args) < 2:
            await self.reply(update, usage)
            return
        try:
            spec = parse_at(args[0]) if daily else parse_interval(args[0])
        except ValueError as exc:
            await self.reply(update, f"❌ {esc(exc)}\n{usage}")
            return
        w = self.worker(update)
        chat_id, thread, key = self._key(update)
        s = Schedule(id=uuid.uuid4().hex[:6], chat_key=key, chat_id=chat_id, thread_id=thread,
                     prompt=" ".join(args[1:]), at=spec if daily else "", every_seconds=0 if daily else spec, cwd=str(w.cwd))
        s.next_run = s.compute_next()
        self.schedules[s.id] = s
        self._save_extras()
        await self.reply(update, f"⏰ Scheduled {esc(s.describe())} in <code>{esc(Path(s.cwd).name)}</code>:\n<i>{esc(s.prompt)}</i>\n/schedules to manage.")

    async def cmd_schedules(self: "Bridge", update: Update, ctx) -> None:
        w = self.worker(update)
        if not self.schedules:
            await self.reply(update, "No schedules. Try <code>/at 09:00 summarize yesterday's commits and open issues</code> or <code>/every 6h run the tests and report failures</code>.")
            return
        for s in self.schedules.values():
            await self.messenger(update).send(
                f"⏰ {esc(s.describe())} · <code>{esc(Path(s.cwd).name)}</code>\n<i>{esc(s.prompt[:300])}</i>",
                buttons=[[("▶️ Run now", w.action_token("sched_run", s.id)), ("🗑 Delete", w.action_token("sched_del", s.id))]], silent=True,
            )

    async def act_sched_del(self: "Bridge", update, w, sid) -> None:
        self.schedules.pop(sid, None)
        self._save_extras()
        await self._drop_buttons(update)
        await self.reply(update, "🗑 Schedule deleted.")

    async def act_sched_run(self: "Bridge", update, w, sid) -> None:
        s = self.schedules.get(sid)
        if s:
            await self._fire(s)

    async def _fire(self: "Bridge", s: Schedule) -> None:
        w = self.make_worker(s.chat_id, s.thread_id, s.chat_key)
        if s.cwd and Path(s.cwd).is_dir() and str(w.cwd) != s.cwd:
            await w.messenger.send(f"⏰ Skipped a schedule: this chat is in <code>{esc(w.cwd.name)}</code>, the schedule is for <code>{esc(Path(s.cwd).name)}</code>. Switch back or recreate it.", silent=True)
            return
        await w.submit(Job(prompt=s.prompt, label="⏰ scheduled", display=f"⏰ {s.prompt}"))

    async def _scheduler(self: "Bridge") -> None:
        while True:
            try:
                await asyncio.sleep(30)
                now = time.time()
                for s in list(self.schedules.values()):
                    if s.next_run and s.next_run <= now:
                        s.last_run = now
                        s.next_run = s.compute_next(now)
                        self._save_extras()
                        await self._fire(s)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("scheduler error")

    # ============================================================ pickers
    async def cmd_sessions(self: "Bridge", update: Update, ctx) -> None:
        w = self.worker(update)
        titles: dict[str, str] = {}
        when: dict[str, str] = {}
        for rec in w.transcript.records():
            c = rec.get("conversation") or ""
            if rec.get("role") == "user" and c and c not in titles:
                titles[c] = rec.get("text", "")
            if c:
                when[c] = rec.get("t", "")
        convs = list(w.state.history)
        if not convs:
            await self.reply(update, "No conversations yet.")
            return
        rows: Buttons = []
        for c in convs[:10]:
            mark = "▶️ " if c == w.state.conversation_id else ""
            label = f"{mark}{(titles.get(c) or c[:8])[:34]}"
            rows.append([(label, w.action_token("resume", c))])
        await self.reply(update, "<b>Recent conversations</b> (tap to continue one)", rows)

    async def act_resume(self: "Bridge", update, w: ChatWorker, conv) -> None:
        if w.busy:
            await self.reply(update, "⏳ A turn is running; /stop it first.")
            return
        await w.reset()
        w.state.conversation_id = conv
        w.state.remember_conversation(conv)
        self.store.save()
        await self.reply(update, f"↩️ Continuing <code>{esc(conv[:8])}…</code>. Your next message picks it up.")

    async def cmd_queue(self: "Bridge", update: Update, ctx) -> None:
        w = self.worker(update)
        jobs = w.pending()
        head = f"🟡 Running: <i>{esc(w.current_prompt[:100])}</i>\n" if w.busy else ""
        if not jobs:
            await self.reply(update, head + "Queue is empty.")
            return
        rows: Buttons = [[(f"✖️ {j.display or j.prompt}"[:40], w.action_token("cancel_job", j.id)),
                          ("⚡ Next", w.action_token("bump_job", j.id))] for j in jobs[:10]]
        await self.reply(update, head + f"<b>{len(jobs)} queued</b> (✖️ cancel · ⚡ run right after the current turn)", rows)

    async def act_cancel_job(self: "Bridge", update, w: ChatWorker, job_id) -> None:
        ok = w.cancel_job(job_id)
        await self.reply(update, "✖️ Cancelled." if ok else "That one already started or finished.")

    async def act_bump_job(self: "Bridge", update, w: ChatWorker, job_id) -> None:
        q = getattr(w.queue, "_queue", None)
        if q is None:
            return
        for j in list(q):
            if j.id == job_id and not j.cancelled:
                q.remove(j)
                q.appendleft(j)
                await self.reply(update, "⚡ Moved to the front.")
                return
        await self.reply(update, "That one already started.")

    async def act_interrupt_with(self: "Bridge", update, w: ChatWorker, job_id) -> None:
        await self._drop_buttons(update)
        await self.act_bump_job(update, w, job_id)
        await w.stop_turn(clear_queue=False)

    async def act_set_opt(self: "Bridge", update, w: ChatWorker, payload) -> None:
        field_name, value = payload
        setattr(w.state, field_name, value)
        self.store.save()
        await w.invalidate()
        await self._drop_buttons(update)
        await self.reply(update, f"✅ {field_name.capitalize()} → <code>{esc(value or 'default')}</code> (from the next message)")

    async def act_implement_plan(self: "Bridge", update, w: ChatWorker, payload) -> None:
        await self._drop_buttons(update)
        overrides = {"mode": ""} if w.options().mode == "plan" else {}
        await self._enqueue(update, Job(prompt="The plan is approved. Implement it now, then run the relevant tests and summarize what changed.",
                                        overrides=overrides, label="implement plan", display="(approved the plan)"))

    async def act_send_file(self: "Bridge", update, w: ChatWorker, path) -> None:
        p = Path(path)
        if not p.is_file():
            await self.reply(update, "That file is gone.")
            return
        await self.messenger(update).send_document(p.read_bytes(), p.name, caption=str(p.name))
