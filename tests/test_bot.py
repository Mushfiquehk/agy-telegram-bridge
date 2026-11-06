from agy_bridge.bot import HELP, Bridge
from agy_bridge.config import Config


def test_bridge_builds_handlers(tmp_path):
    cfg = Config.from_env({"TELEGRAM_BOT_TOKEN": "123456:ABCdefGhIJKlmNoPQRsTUVwxyZ", "ALLOWED_USER_IDS": "1",
                           "AGY_WORKDIR": str(tmp_path), "STATE_FILE": str(tmp_path / "s.json"),
                           "PROMPTS_FILE": str(tmp_path / "none.toml")})
    app = Bridge(cfg).build()
    handlers = [h for group in app.handlers.values() for h in group]
    commands = set()
    for h in handlers:
        commands |= set(getattr(h, "commands", ()))
    for c in ["whoami", "new", "stop", "status", "cwd", "model", "yolo", "resume", "usage",
              "projects", "p", "git", "diff", "undo", "commit", "push", "test", "sh", "cat", "get",
              "review", "fix", "plan", "allow", "restart", "update", "export"]:
        assert c in commands, c
    assert "/projects" in HELP
