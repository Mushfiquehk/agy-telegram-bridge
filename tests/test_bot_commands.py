"""Drive bot command handlers with fake Telegram objects (no network)."""
import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from agy_bridge.bot import Bridge
from agy_bridge.config import Config
from agy_bridge.core import ChatWorker
from test_core import FakeMessenger, _git_repo


class FakeMsg:
    def __init__(self, text="", sink=None):
        self.text = text
        self.caption = None
        self.reply_to_message = None
        self.message_thread_id = None
        self.is_topic_message = False
        self.message_id = 1
        self.sink = sink

    async def reply_text(self, text, **kw):
        self.sink.append((text, kw.get("reply_markup")))
        return SimpleNamespace(delete=_anoop, edit_text=_anoop)


async def _anoop(*a, **k):
    return None


def setup(tmp_path, fake_cmd, **env):
    _git_repo(tmp_path)
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:x", "ALLOWED_USER_IDS": "7", "AGY_WORKDIR": str(tmp_path),
                           "ALLOWED_ROOTS": str(tmp_path), "STATE_FILE": str(tmp_path.parent / f"{tmp_path.name}-s.json"),
                           "TRANSCRIPT_DIR": str(tmp_path.parent / f"{tmp_path.name}-t"), "EDIT_INTERVAL_SECONDS": "0",
                           "PROMPTS_FILE": str(tmp_path.parent / "none.toml"), **env})
    cfg.agy_command = fake_cmd
    b = Bridge(cfg)
    b.app = SimpleNamespace(bot=None)
    m = FakeMessenger()
    w = ChatWorker(42, cfg, b.store, m, key="42")
    b.workers["42"] = w
    replies = []
    return b, w, m, replies


def upd(text, replies):
    msg = FakeMsg(text, replies)
    return SimpleNamespace(effective_chat=SimpleNamespace(id=42), effective_message=msg,
                           effective_user=SimpleNamespace(id=7, username="u"), callback_query=None)


def ctx(*args):
    return SimpleNamespace(args=list(args))


def all_text(m, replies):
    return "\n".join([h for h, _ in m.sent] + [t for t, _ in replies])


@pytest.mark.asyncio
async def test_dev_commands(tmp_path, fake_cmd):
    b, w, m, r = setup(tmp_path, fake_cmd, ALLOW_SHELL="true", TEST_COMMAND="echo all-good")
    (tmp_path / "README.md").write_text("hello\nchanged\n")
    await b.cmd_git(upd("/git", r), ctx())
    await b.cmd_diff(upd("/diff", r), ctx())
    await b.cmd_cat(upd("/cat README.md", r), ctx("README.md", "1-2"))
    await b.cmd_ls(upd("/ls", r), ctx())
    await b.cmd_tree(upd("/tree", r), ctx())
    await b.cmd_find(upd("/find read", r), ctx("read"))
    await b.cmd_grep(upd("/grep changed", r), ctx("changed"))
    await b.cmd_log(upd("/log", r), ctx())
    await b.cmd_test(upd("/test", r), ctx())
    await b._shell(upd("!echo hi-there", r), "echo hi-there")
    await b.cmd_status(upd("/status", r), ctx())
    await b.cmd_cat(upd("/cat ../../etc/passwd", r), ctx("../../etc/passwd"))
    text = all_text(m, r)
    for needle in ["modified", "+changed", "lines 1-2", "README.md", "Tests passed", "hi-there", "Status", "outside the allowed"]:
        assert needle in text, needle


@pytest.mark.asyncio
async def test_failing_test_offers_fix_and_button_flows(tmp_path, fake_cmd):
    b, w, m, r = setup(tmp_path, fake_cmd, TEST_COMMAND="exit 1")
    await b.cmd_test(upd("/test", r), ctx())
    assert "Tests failed" in all_text(m, r)
    fix = [d for rows in m.buttons if rows for row in rows for (l, d) in row if "fix" in l.lower()]
    assert fix
    action, payload = w.actions[fix[0][2:]]
    assert action == "fix_tests"
    # a turn that writes files, then undo through the confirmation flow
    await w.submit("write please")
    await asyncio.wait_for(w.queue.join(), 30)
    u = upd("/undo", r)
    await b.cmd_undo(u, ctx())
    yes = [d for rows in m.buttons if rows for row in rows for (l, d) in row if l.startswith("↩️ Yes")]
    act, rec = w.actions[yes[-1][2:]]
    u.callback_query = SimpleNamespace(edit_message_reply_markup=_anoop)
    await b.act_undo_yes(u, w, rec)
    assert not (tmp_path / "new_file.txt").exists()
    assert "Restored" in all_text(m, r)
    await asyncio.wait_for(w.queue.join(), 30)
    await w.shutdown()


@pytest.mark.asyncio
async def test_shell_off_by_default_and_projects(tmp_path, fake_cmd):
    b, w, m, r = setup(tmp_path, fake_cmd)
    await b._shell(upd("!dir", r), "dir")
    assert "Shell commands are off" in all_text(m, r)
    sub = tmp_path / "other"
    sub.mkdir()
    (sub / "package.json").write_text("{}")
    await b.cmd_p(upd("/p other", r), ctx("other"))
    assert w.cwd == sub.resolve()
    await b.cmd_cwd(upd("/cwd -", r), ctx("-"))
    assert w.cwd == tmp_path.resolve() or str(w.cwd) == str(tmp_path)


@pytest.mark.asyncio
async def test_commit_flow(tmp_path, fake_cmd):
    b, w, m, r = setup(tmp_path, fake_cmd)
    (tmp_path / "x.txt").write_text("x")
    u = upd("/commit add x", r)
    await b.cmd_commit(u, ctx("add", "x"))
    btn = [d for t, mk in r if mk for row in mk.inline_keyboard for d in [row[0].callback_data]]
    act, payload = w.actions[btn[-1][2:]]
    assert act == "commit_yes"
    u.callback_query = SimpleNamespace(edit_message_reply_markup=_anoop)
    await b.act_commit_yes(u, w, payload)
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "add x" in log
