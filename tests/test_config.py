from pathlib import Path

import pytest

from agy_bridge.config import Config, ConfigError


BASE = {"TELEGRAM_BOT_TOKEN": "123:abc", "ALLOWED_USER_IDS": "42, 7"}


def test_minimal(tmp_path):
    c = Config.from_env({**BASE, "AGY_WORKDIR": str(tmp_path)})
    assert c.allowed_user_ids == {42, 7}
    assert c.agy_command == ["agy"]
    assert c.allowed_roots == [tmp_path]
    assert c.run_mode == "persistent" and c.skip_permissions is False


def test_required():
    with pytest.raises(ConfigError):
        Config.from_env({"ALLOWED_USER_IDS": "1"})
    with pytest.raises(ConfigError):
        Config.from_env({"TELEGRAM_BOT_TOKEN": "x"})
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "ALLOWED_USER_IDS": "abc"})


def test_validation():
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "AGY_MODE": "turbo"})
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "RUN_MODE": "x"})
    c = Config.from_env({**BASE, "AGY_MODE": "default", "AGY_SKIP_PERMISSIONS": "yes", "AGY_EXTRA_ARGS": "--add-dir /tmp"})
    assert c.mode == "" and c.skip_permissions and c.extra_args == ["--add-dir", "/tmp"]


def test_allowed_roots(tmp_path):
    a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir(); (a / "sub").mkdir()
    c = Config.from_env({**BASE, "AGY_WORKDIR": str(a), "ALLOWED_ROOTS": f"{a};{tmp_path / 'zz'}"})
    assert c.is_path_allowed(a / "sub")
    assert not c.is_path_allowed(b)
    assert not c.is_path_allowed(a / ".." / "b")
