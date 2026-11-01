import asyncio
import json

import pytest

from agy_bridge.agy import AgyOptions, AgySession, parse_line, run_simple


def test_parse_init_result_text_tool():
    ev = parse_line('{"event":"init","conversation_id":"abc","init":{}}')
    assert ev.kind == "init" and ev.conversation_id == "abc"

    ev = parse_line(json.dumps({"event": "step_update", "step_update": {
        "conversation_id": "abc", "step_index": 2, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "hel"}}))
    assert ev.kind == "text" and ev.text == "hel" and ev.step_index == 2

    ev = parse_line(json.dumps({"event": "step_update", "step_update": {
        "step_index": 4, "state": "DONE", "step_type": "tool", "tool_name": "run_command",
        "tool_info": {"name": "run_command", "parameters": {"CommandLine": "ls"}, "output": "a\n",
                      "error": {"type": "denied", "message": "nope"}}}}))
    assert ev.kind == "tool" and ev.tool_name == "run_command"
    assert ev.tool_params == {"CommandLine": "ls"} and ev.tool_error == "denied: nope"

    ev = parse_line('{"event":"result","result":{"conversation_id":"abc","status":"SUCCESS","response":"hi\\n","usage":{"total_tokens":5}}}')
    assert ev.ok and ev.response == "hi\n" and ev.usage["total_tokens"] == 5


def test_parse_garbage_and_unknown():
    assert parse_line("") is None
    assert parse_line("not json").kind == "stderr"
    assert parse_line('{"event":"future_thing"}').kind == "step"
    assert parse_line("[1,2]").kind == "stderr"


def test_options_to_args():
    o = AgyOptions(model="m", effort="high", mode="plan", skip_permissions=True, sandbox=True, extra_args=["--x"])
    assert o.to_args() == ["--model", "m", "--effort", "high", "--mode", "plan", "--sandbox",
                           "--dangerously-skip-permissions", "--x"]
    assert AgyOptions().to_args() == []


async def collect(session, prompt, timeout=20):
    return [ev async for ev in session.ask(prompt, timeout=timeout)]


@pytest.mark.asyncio
async def test_persistent_multi_turn(fake_cmd, tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("FAKE_AGY_LOG", str(log))
    s = AgySession(fake_cmd, tmp_path, persistent=True)
    evs = await collect(s, "hello")
    res = evs[-1]
    assert res.kind == "result" and res.ok and "You said: hello (turn 1" in res.response
    assert "".join(e.text for e in evs if e.kind == "text") == res.response
    conv = s.conversation_id
    assert conv
    evs = await collect(s, "again & | ^ \"quotes\" 'single' %PATH% $HOME")
    assert "(turn 2" in evs[-1].response  # same process kept context
    assert "%PATH% $HOME" in evs[-1].response  # prompt passed byte-exact via stdin
    assert s.alive
    await s.close()
    assert not s.alive
    assert len(log.read_text().splitlines()) == 1  # one process for both turns


@pytest.mark.asyncio
async def test_oneshot_resumes_with_conversation(fake_cmd, tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("FAKE_AGY_LOG", str(log))
    s = AgySession(fake_cmd, tmp_path, persistent=False)
    await collect(s, "one")
    conv = s.conversation_id
    assert not s.alive
    evs = await collect(s, "two")
    assert evs[-1].conversation_id == conv
    argvs = [json.loads(l) for l in log.read_text().splitlines()]
    assert len(argvs) == 2
    assert "--conversation" not in argvs[0]
    assert argvs[1][argvs[1].index("--conversation") + 1] == conv


@pytest.mark.asyncio
async def test_tool_events(fake_cmd, tmp_path):
    s = AgySession(fake_cmd, tmp_path)
    evs = await collect(s, "use a tool")
    tools = [e for e in evs if e.kind == "tool"]
    assert len(tools) == 2 and tools[-1].state == "DONE" and tools[-1].tool_output == "hi\r\n"
    await s.close()


@pytest.mark.asyncio
async def test_crash_reports_error_with_stderr(fake_cmd, tmp_path):
    s = AgySession(fake_cmd, tmp_path)
    evs = await collect(s, "crash now")
    assert evs[-1].kind == "error"
    assert "code 3" in evs[-1].error and "simulated crash" in evs[-1].error
    assert not s.alive
    # next turn transparently restarts and resumes the conversation
    conv = s.conversation_id
    evs = await collect(s, "after the oops")
    assert evs[-1].ok and evs[-1].conversation_id == conv
    await s.close()


@pytest.mark.asyncio
async def test_timeout_kills(fake_cmd, tmp_path):
    s = AgySession(fake_cmd, tmp_path)
    evs = await collect(s, "sleep please", timeout=1.5)
    assert evs[-1].kind == "error" and evs[-1].status == "TIMEOUT"
    assert not s.alive


@pytest.mark.asyncio
async def test_interrupt_mid_turn(fake_cmd, tmp_path):
    s = AgySession(fake_cmd, tmp_path)

    async def stop_soon():
        await asyncio.sleep(1.0)
        await s.interrupt()

    t = asyncio.create_task(stop_soon())
    evs = await collect(s, "sleep please", timeout=30)
    await t
    assert evs[-1].kind == "error"
    assert not s.busy


@pytest.mark.asyncio
async def test_permission_stderr_captured(fake_cmd, tmp_path):
    s = AgySession(fake_cmd, tmp_path)
    await collect(s, "permission test")
    await asyncio.sleep(0.1)
    assert any("soft-denied" in l for l in s.turn_stderr())
    await s.close()


@pytest.mark.asyncio
async def test_missing_binary(tmp_path):
    s = AgySession(["definitely-not-a-real-agy-binary"], tmp_path)
    evs = await collect(s, "hi")
    assert evs[-1].kind == "error" and "Could not find" in evs[-1].error


@pytest.mark.asyncio
async def test_run_simple(fake_cmd, tmp_path):
    code, out = await run_simple(fake_cmd, ["models"], tmp_path)
    assert code == 0 and "fake-model-high" in out
