import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from agy_bridge import ghcli, tasks
from agy_bridge.core import ChatWorker, Job
from agy_bridge.schedules import Schedule, parse_at, parse_interval
from test_bot_commands import _anoop, all_text, ctx, setup, upd
from test_core import FakeMessenger


def fake_workers(b):
    msgs = {}

    def make_worker(chat_id, thread, key):
        if key not in b.workers:
            m = FakeMessenger()
            msgs[key] = m
            b.workers[key] = ChatWorker(chat_id, b.config, b.store, m, key=key)
        return b.workers[key]

    b.make_worker = make_worker
    return msgs


def buttons_of(m):
    return [(l, d) for rows in m.buttons if rows for row in rows for (l, d) in row]


async def settle(b, timeout=30):
    for _ in range(3):
        for w in list(b.workers.values()):
            await asyncio.wait_for(w.queue.join(), timeout)
        await asyncio.sleep(0.2)


def git(path, *a):
    return subprocess.run(["git", *a], cwd=path, capture_output=True, text=True).stdout


@pytest.mark.asyncio
async def test_background_task_lifecycle(tmp_path, fake_cmd):
    repo = tmp_path / "repo"
    repo.mkdir()
    b, w, m, r = setup(repo, fake_cmd)
    msgs = fake_workers(b)
    await b.cmd_task(upd("/task please write a feature", r), ctx())
    assert len(b.tasks) == 1
    task = next(iter(b.tasks.values()))
    await settle(b)
    tm = msgs[f"task:{task.id}"]
    text = "\n".join(h for h, _ in tm.sent)
    assert "started on branch" in text and "finished" in text
    assert task.status == "done"
    # main checkout untouched; work committed on the task branch
    assert not (repo / "new_file.txt").exists()
    assert "agy: please write a feature" in git(repo, "log", "--oneline", task.branch)
    labels = [l for l, _ in buttons_of(tm)]
    assert any(l.startswith("🔀 Merge") for l in labels) and "🗑 Discard" in labels
    # merge
    u = upd("x", r)
    u.callback_query = SimpleNamespace(edit_message_reply_markup=_anoop)
    tw = b.workers[f"task:{task.id}"]
    await b.act_task_merge(u, tw, task.id)
    assert (repo / "new_file.txt").exists()
    assert task.id not in b.tasks
    assert task.branch not in git(repo, "branch")


@pytest.mark.asyncio
async def test_task_discard_and_reply_routing(tmp_path, fake_cmd):
    repo = tmp_path / "repo"
    repo.mkdir()
    b, w, m, r = setup(repo, fake_cmd)
    msgs = fake_workers(b)
    await b.cmd_task(upd("/task write things", r), ctx())
    task = next(iter(b.tasks.values()))
    await settle(b)
    # route a reply to the task's message
    b.msg_owner[(42, 999)] = f"task:{task.id}"
    u = upd("also add docs", r)
    u.effective_message.reply_to_message = SimpleNamespace(message_id=999, text="t", caption=None)
    await b.on_text(u, ctx())
    await settle(b)
    assert any("You said: also add docs" in h for h, _ in msgs[f"task:{task.id}"].sent)
    u.callback_query = SimpleNamespace(edit_message_reply_markup=_anoop)
    await b.act_task_discard_yes(u, w, task.id)
    assert not __import__("pathlib").Path(task.path).exists()
    assert task.branch not in git(repo, "branch")


@pytest.mark.asyncio
async def test_task_needs_git(tmp_path, fake_cmd):
    b, w, m, r = setup(tmp_path, fake_cmd)
    w.state.cwd = str(tmp_path.parent)  # not a repo
    with pytest.raises(tasks.TaskError):
        await tasks.create(tmp_path.parent / "nope", "x", "1", "k")


def test_schedule_parsing():
    assert parse_interval("2h") == 7200
    assert parse_interval("30m") == 1800
    with pytest.raises(ValueError):
        parse_interval("1m")
    assert parse_at("9:05") == "09:05"
    with pytest.raises(ValueError):
        parse_at("25:00")
    s = Schedule(id="a", chat_key="1", chat_id=1, thread_id=None, prompt="p", at="09:00")
    import time as _t
    n = s.compute_next()
    assert 0 < n - _t.time() <= 86400
    assert "daily at 09:00" in s.describe()


@pytest.mark.asyncio
async def test_schedule_commands_and_fire(tmp_path, fake_cmd):
    b, w, m, r = setup(tmp_path, fake_cmd)
    fake_workers(b)
    b.workers["42"] = w
    await b.cmd_every(upd("/every 2h run checks", r), ctx("2h", "run", "checks"))
    assert len(b.schedules) == 1
    s = next(iter(b.schedules.values()))
    assert s.every_seconds == 7200 and s.prompt == "run checks"
    await b._fire(s)
    await settle(b)
    assert any("You said: run checks" in h for h, _ in m.sent)
    assert b.store.extras["schedules"][0]["prompt"] == "run checks"


@pytest.mark.asyncio
async def test_plan_button_file_buttons_and_queue(tmp_path, fake_cmd):
    b, w, m, r = setup(tmp_path, fake_cmd)
    (tmp_path / "report.csv").write_text("a,b\n")
    await w.submit(Job(prompt="see `report.csv`", overrides={"mode": "plan"}))
    await settle(b)
    labels = [l for l, _ in buttons_of(m)]
    assert "✅ Implement this plan" in labels
    assert "📎 report.csv" in labels
    # queue cancel
    await w.submit("sleep 2")
    await asyncio.sleep(0.5)
    j = Job(prompt="later")
    await w.submit(j)
    assert [x.id for x in w.pending()] == [j.id]
    assert w.cancel_job(j.id)
    await w.stop_turn(clear_queue=False)
    await settle(b)
    assert not any("You said: later" in h for h, _ in m.sent)


@pytest.mark.asyncio
async def test_remote_url_parsing(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:me/proj.git"], cwd=tmp_path)
    assert await ghcli.remote_web_url(tmp_path) == "https://github.com/me/proj"
    assert (await ghcli.compare_url(tmp_path, "agy/x")).endswith("/compare/agy/x?expand=1")


def test_viewer_registration(tmp_path):
    from agy_bridge.bot import Bridge
    from agy_bridge.config import Config

    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ALLOWED_USER_IDS": "1", "VIEWER_USER_IDS": "2,3",
                           "AGY_WORKDIR": str(tmp_path), "STATE_FILE": str(tmp_path / "s.json"),
                           "PROMPTS_FILE": str(tmp_path / "none.toml")})
    assert cfg.viewer_user_ids == frozenset({2, 3})
    app = Bridge(cfg).build()
    cmds = set()
    for h in [h for g in app.handlers.values() for h in g]:
        cmds |= set(getattr(h, "commands", ()))
    for c in ["task", "tasks", "pr", "ci", "every", "at", "schedules", "sessions", "queue", "prdesc"]:
        assert c in cmds, c
