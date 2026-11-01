import asyncio

import pytest

from agy_bridge.config import Config
from agy_bridge.core import ChatWorker
from agy_bridge.state import StateStore


class FakeMessenger:
    def __init__(self):
        self.sent = []      # (html, plain)
        self.edits = []
        self.deleted = []
        self.docs = []
        self._n = 0

    async def send(self, html, plain=None):
        self._n += 1
        self.sent.append((html, plain))
        return self._n

    async def edit(self, handle, html):
        self.edits.append((handle, html))

    async def delete(self, handle):
        self.deleted.append(handle)

    async def send_document(self, data, filename, caption=""):
        self.docs.append((filename, data, caption))

    async def typing(self):
        pass


def make(tmp_path, fake_cmd, **over):
    env = {"TELEGRAM_BOT_TOKEN": "1:x", "ALLOWED_USER_IDS": "1", "AGY_WORKDIR": str(tmp_path),
           "STATE_FILE": str(tmp_path / "state.json"), "EDIT_INTERVAL_SECONDS": "0", **over}
    cfg = Config.from_env(env)
    cfg.agy_command = fake_cmd
    store = StateStore(cfg.state_file)
    m = FakeMessenger()
    return ChatWorker(1, cfg, store, m), m, store


async def drain(w):
    await asyncio.wait_for(w.queue.join(), timeout=30)


@pytest.mark.asyncio
async def test_turn_flow_and_state_persisted(tmp_path, fake_cmd):
    w, m, store = make(tmp_path, fake_cmd)
    await w.submit("hello there")
    await drain(w)
    texts = [h for h, _ in m.sent]
    assert any("You said: hello there" in t for t in texts)
    assert m.deleted == [1]  # status bubble removed when no tools ran
    conv = w.state.conversation_id
    assert conv
    assert conv in (tmp_path / "state.json").read_text()
    await w.submit("second")
    await drain(w)
    assert any("(turn 2" in h for h, _ in m.sent)
    await w.shutdown()


@pytest.mark.asyncio
async def test_tool_lines_shown(tmp_path, fake_cmd):
    w, m, _ = make(tmp_path, fake_cmd)
    await w.submit("use a tool")
    await drain(w)
    assert any("run_command: echo hi" in h for _, h in m.edits)
    assert any("✅ Done" in h and "1 step" in h for _, h in m.edits)
    await w.shutdown()


@pytest.mark.asyncio
async def test_queue_and_stop(tmp_path, fake_cmd):
    w, m, _ = make(tmp_path, fake_cmd)
    await w.submit("sleep long")
    ahead = await w.submit("queued one")
    assert ahead == 1
    await asyncio.sleep(1.0)
    running, dropped = await w.stop_turn()
    assert running and dropped == 1
    await drain(w)
    assert any("Stopped" in h for h, _ in m.sent)
    # conversation survives the stop
    conv = w.state.conversation_id
    await w.submit("after stop")
    await drain(w)
    assert w.state.conversation_id == conv
    assert any("You said: after stop" in h for h, _ in m.sent)
    await w.shutdown()


@pytest.mark.asyncio
async def test_error_and_permission_notice(tmp_path, fake_cmd):
    w, m, _ = make(tmp_path, fake_cmd)
    await w.submit("crash")
    await drain(w)
    assert any("agy error" in h and "simulated crash" in h for h, _ in m.sent)
    await w.submit("permission please")
    await drain(w)
    assert any("Permission notices" in h for h, _ in m.sent)
    await w.submit("fail please")
    await drain(w)
    assert any("simulated failure" in h for h, _ in m.sent)
    await w.shutdown()


@pytest.mark.asyncio
async def test_long_reply_becomes_document(tmp_path, fake_cmd):
    w, m, _ = make(tmp_path, fake_cmd)
    await w.submit("long please")
    await drain(w)
    assert m.docs and m.docs[0][0] == "agy-response.md"
    await w.shutdown()


@pytest.mark.asyncio
async def test_reset_and_option_restart(tmp_path, fake_cmd, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("FAKE_AGY_LOG", str(log))
    w, m, _ = make(tmp_path, fake_cmd)
    await w.submit("a")
    await drain(w)
    first = w.state.conversation_id
    w.state.model = "fake-model-high"
    await w.invalidate()
    await w.submit("b")
    await drain(w)
    lines = log.read_text().splitlines()
    assert len(lines) == 2 and "fake-model-high" in lines[1] and first in lines[1]
    await w.reset()
    assert w.state.conversation_id == ""
    await w.submit("c")
    await drain(w)
    assert w.state.conversation_id and w.state.conversation_id != first
    await w.shutdown()


@pytest.mark.asyncio
async def test_oneshot_mode(tmp_path, fake_cmd):
    w, m, _ = make(tmp_path, fake_cmd, RUN_MODE="oneshot")
    await w.submit("x")
    await drain(w)
    assert w.session and not w.session.alive
    await w.submit("y")
    await drain(w)
    assert any("You said: y" in h for h, _ in m.sent)
    await w.shutdown()
