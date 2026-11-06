import json
import subprocess


import pytest

from agy_bridge import agysettings, gitops, projects
from agy_bridge.bot import _tree
from agy_bridge.core import suggest_rules
from agy_bridge.procs import run_shell
from agy_bridge.prompts import BUILTIN, load_custom
from agy_bridge.transcript import Transcript


def test_prompt_render():
    assert "src/app.py" in BUILTIN["explain"].render("src/app.py", "/x")
    r = BUILTIN["review"].render("", "/x")
    assert "{args}" not in r
    assert BUILTIN["plan"].mode == "plan"


def test_custom_prompts(tmp_path):
    f = tmp_path / "prompts.toml"
    f.write_text('[shortcuts.docs]\ndescription = "Docs"\nprompt = "Update docs {args}"\n[shortcuts."Bad Name"]\nprompt="x"\n')
    got = load_custom(f)
    assert list(got) == ["docs"]
    assert got["docs"].render("now", "/x") == "Update docs now"


def test_agy_settings_roundtrip(tmp_path):
    p = tmp_path / "s" / "settings.json"
    assert agysettings.add_allow_rule(p, "command(git)")
    assert not agysettings.add_allow_rule(p, "command(git)")
    p.write_text(json.dumps({"agentMode": "accept-edits", "permissions": {"allow": ["command(git)"]}}))
    assert agysettings.add_allow_rule(p, "command(npm)")
    data = json.loads(p.read_text())
    assert data["agentMode"] == "accept-edits" and data["permissions"]["allow"] == ["command(git)", "command(npm)"]
    assert list(p.parent.glob("settings.json.bak-*"))
    assert agysettings.remove_allow_rule(p, "command(git)")
    assert agysettings.allow_rules(p) == ["command(npm)"]
    with pytest.raises(agysettings.SettingsError):
        agysettings.add_allow_rule(p, "rm -rf /")
    p.write_text("{ // comment\n}")
    with pytest.raises(agysettings.SettingsError):
        agysettings.add_allow_rule(p, "command(ls)")


def test_suggest_rules():
    notices = ["run_command was soft-denied. Add command(git) to permissions.allow"]
    denied = [("run_command", {"CommandLine": "C:\\tools\\pnpm.cmd install"}), ("run_command", {"CommandLine": "git push"})]
    assert suggest_rules(notices, denied) == ["command(git)", "command(pnpm)"]


def test_projects_discover_and_detect(tmp_path):
    (tmp_path / "web" / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "web" / "package.json").write_text('{"scripts": {"test": "vitest"}}')
    (tmp_path / "web" / "pnpm-lock.yaml").write_text("")
    (tmp_path / "group" / "api").mkdir(parents=True)
    (tmp_path / "group" / "api" / "pyproject.toml").write_text("")
    (tmp_path / "group" / "api" / "sub").mkdir()
    (tmp_path / "group" / "api" / "sub" / "Cargo.toml").write_text("")
    (tmp_path / "notes").mkdir()
    found = projects.discover([tmp_path])
    assert [p.name for p in found] == ["api", "web"]
    assert projects.detect_test_command(tmp_path / "web") == "pnpm test"
    assert "pytest" in projects.detect_test_command(tmp_path / "group" / "api")
    assert projects.match(found, "AP")[0].name == "api"


def test_transcript(tmp_path):
    t = Transcript(tmp_path, "123:4")
    t.add("user", "hi", cwd="/x", conversation="c1")
    t.add("agy", "hello", conversation="c1")
    assert t.last_reply() == "hello"
    md = t.to_markdown()
    assert "## You" in md and "hello" in md


def test_tree():
    out = _tree(__import__("pathlib").Path("proj"), 2, ["a.py", "src/b.py", "src/deep/c.py", "src/deep/d.py"])
    assert "src/" in out and "deep/" in out and "… 2 more" in out


@pytest.mark.asyncio
async def test_run_shell(tmp_path):
    r = await run_shell("echo hello", tmp_path, timeout=30)
    assert r.ok and "hello" in r.out
    r = await run_shell("exit 3", tmp_path, timeout=30)
    assert r.code == 3


@pytest.mark.asyncio
async def test_git_status(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x")
    st = await gitops.status(tmp_path)
    assert st.branch == "main" and st.untracked == ["a.txt"] and not st.clean
    text = gitops.format_status(st)
    assert "untracked (1)" in text
    assert await gitops.checkpoint(tmp_path) is None  # no commits yet
