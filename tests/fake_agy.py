#!/usr/bin/env python3
"""A stand-in for `agy` that speaks the headless stream-json protocol.

Behaviour is chosen by keywords in the prompt:
  tool       -> emits a run_command tool step before answering
  sleep      -> hangs for 60s (to test /stop and timeouts)
  crash      -> exits with code 3 and no result
  permission -> prints a soft-deny notice on stderr
  long       -> returns a very long Markdown answer with a code block
  fail       -> returns status ERROR
  write      -> edits README.md and creates new_file.txt in the cwd
  denied     -> emits a run_command step that was soft-denied
"""
import json
import os
import sys
import time
import uuid

argv = sys.argv[1:]
log = os.environ.get("FAKE_AGY_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")

if argv[:1] == ["models"]:
    print("fake-model-high     Fake Model (High)")
    sys.exit(0)

conv = str(uuid.uuid4())
if "--conversation" in argv:
    conv = argv[argv.index("--conversation") + 1]

assert "--input-format" in argv and "--output-format" in argv, argv


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


emit({"event": "init", "conversation_id": conv, "init": {"cwd": os.getcwd(), "tools": ["run_command"], "permission_mode": "request-review"}})
turns = 0
step = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    content = msg["message"]["content"]
    if isinstance(content, list):
        content = "".join(b["text"] for b in content)
    turns += 1

    def step_update(**kw):
        global step
        kw.setdefault("conversation_id", conv)
        kw.setdefault("step_index", step)
        step += 1
        emit({"event": "step_update", "step_update": kw})

    step_update(state="DONE", step_type="user_input")
    if "crash" in content:
        sys.stderr.write("fatal: simulated crash\n")
        sys.stderr.flush()
        sys.exit(3)
    if "sleep" in content:
        time.sleep(60)
    if "permission" in content:
        sys.stderr.write("run_command was soft-denied: permission required. Add command(git) to permissions.allow\n")
        sys.stderr.flush()
    if "tool" in content:
        step_update(state="ACTIVE", step_type="tool", tool_name="run_command",
                    tool_info={"name": "run_command", "parameters": {"CommandLine": "echo hi"}})
        step -= 1
        step_update(state="DONE", step_type="tool", tool_name="run_command",
                    tool_info={"name": "run_command", "parameters": {"CommandLine": "echo hi"}, "output": "hi\r\n"})
    if "write" in content:
        with open("README.md", "a", encoding="utf-8") as fh:
            fh.write("added by agy\n")
        with open("new_file.txt", "w", encoding="utf-8") as fh:
            fh.write("brand new\n")
    if "denied" in content:
        step_update(state="DONE", step_type="tool", tool_name="run_command",
                    tool_info={"name": "run_command", "parameters": {"CommandLine": "npm.cmd test"},
                               "error": {"type": "permission", "message": "soft-denied: permission required"}})
    if "long" in content:
        answer = "# Big answer\n\n" + "\n".join(f"Line {i} with **bold** text" for i in range(1200)) + "\n```python\n" + "\n".join(f"x{i} = {i}" for i in range(300)) + "\n```\n"
    else:
        answer = f"You said: {content} (turn {turns}, cwd {os.path.basename(os.getcwd())})"
    half = len(answer) // 2
    step_update(state="ACTIVE", step_type="agent_response", text_delta=answer[:half])
    step -= 1
    step_update(state="DONE", step_type="agent_response", text_delta=answer[half:],
                usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})
    status = "ERROR" if "fail" in content else "SUCCESS"
    result = {"conversation_id": conv, "status": status, "response": answer, "duration_seconds": 0.1,
              "num_turns": turns, "usage": {"input_tokens": 100 * turns, "output_tokens": 10 * turns, "total_tokens": 110 * turns}}
    if status == "ERROR":
        result["error"] = "simulated failure"
    emit({"event": "result", "result": result})
