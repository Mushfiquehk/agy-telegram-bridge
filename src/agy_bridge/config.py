"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional dependency
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


VALID_MODES = {"", "default", "accept-edits", "auto", "plan"}
VALID_EFFORTS = {"", "low", "medium", "high", "max"}
VALID_RUN_MODES = {"persistent", "oneshot"}


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _int(name: str, value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _float(name: str, value: str | None, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def split_command(cmd: str) -> list[str]:
    """Split a command string the way the current OS shell would (roughly)."""
    cmd = cmd.strip()
    if not cmd:
        return []
    if sys.platform == "win32":
        parts = shlex.split(cmd, posix=False)
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    return shlex.split(cmd)


@dataclass
class Config:
    telegram_token: str
    allowed_user_ids: set[int]
    agy_command: list[str] = field(default_factory=lambda: ["agy"])
    workdir: Path = field(default_factory=Path.home)
    allowed_roots: list[Path] = field(default_factory=list)
    model: str = ""
    effort: str = ""
    mode: str = ""
    skip_permissions: bool = False
    allow_yolo_toggle: bool = False
    sandbox: bool = False
    extra_args: list[str] = field(default_factory=list)
    run_mode: str = "persistent"
    turn_timeout: int = 1800
    idle_shutdown: int = 1800
    show_tool_calls: bool = True
    edit_interval: float = 1.5
    state_file: Path = field(default_factory=lambda: Path("data") / "state.json")
    max_upload_mb: int = 20

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, dotenv_path: str | None = None) -> "Config":
        if env is None:
            if load_dotenv is not None:
                load_dotenv(dotenv_path or ".env", override=False)
            env = dict(os.environ)

        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required (get one from @BotFather).")

        raw_ids = env.get("ALLOWED_USER_IDS", "").replace(" ", "")
        if not raw_ids:
            raise ConfigError(
                "ALLOWED_USER_IDS is required. Send /whoami to your bot (or message @userinfobot) "
                "to get your numeric Telegram user id."
            )
        try:
            allowed = {int(x) for x in raw_ids.split(",") if x}
        except ValueError as exc:
            raise ConfigError(f"ALLOWED_USER_IDS must be comma-separated integers, got {raw_ids!r}") from exc

        workdir = Path(os.path.expandvars(os.path.expanduser(env.get("AGY_WORKDIR", "") or str(Path.home()))))
        roots_raw = env.get("ALLOWED_ROOTS", "")
        roots = [
            Path(os.path.expandvars(os.path.expanduser(r.strip())))
            for r in roots_raw.split(";" if ";" in roots_raw else ",")
            if r.strip()
        ] or [workdir]

        mode = env.get("AGY_MODE", "").strip()
        if mode not in VALID_MODES:
            raise ConfigError(f"AGY_MODE must be one of {sorted(VALID_MODES)}, got {mode!r}")
        effort = env.get("AGY_EFFORT", "").strip()
        if effort not in VALID_EFFORTS:
            raise ConfigError(f"AGY_EFFORT must be one of {sorted(VALID_EFFORTS)}, got {effort!r}")
        run_mode = env.get("RUN_MODE", "persistent").strip() or "persistent"
        if run_mode not in VALID_RUN_MODES:
            raise ConfigError(f"RUN_MODE must be one of {sorted(VALID_RUN_MODES)}, got {run_mode!r}")

        agy_cmd = split_command(env.get("AGY_COMMAND", "agy") or "agy")

        return cls(
            telegram_token=token,
            allowed_user_ids=allowed,
            agy_command=agy_cmd,
            workdir=workdir,
            allowed_roots=roots,
            model=env.get("AGY_MODEL", "").strip(),
            effort=effort,
            mode="" if mode == "default" else mode,
            skip_permissions=_bool(env.get("AGY_SKIP_PERMISSIONS")),
            allow_yolo_toggle=_bool(env.get("ALLOW_YOLO_TOGGLE")),
            sandbox=_bool(env.get("AGY_SANDBOX")),
            extra_args=split_command(env.get("AGY_EXTRA_ARGS", "")),
            run_mode=run_mode,
            turn_timeout=_int("TURN_TIMEOUT_SECONDS", env.get("TURN_TIMEOUT_SECONDS"), 1800),
            idle_shutdown=_int("IDLE_SHUTDOWN_SECONDS", env.get("IDLE_SHUTDOWN_SECONDS"), 1800),
            show_tool_calls=_bool(env.get("SHOW_TOOL_CALLS"), True),
            edit_interval=_float("EDIT_INTERVAL_SECONDS", env.get("EDIT_INTERVAL_SECONDS"), 1.5),
            state_file=Path(env.get("STATE_FILE", "") or Path("data") / "state.json"),
            max_upload_mb=_int("MAX_UPLOAD_MB", env.get("MAX_UPLOAD_MB"), 20),
        )

    def is_path_allowed(self, path: Path) -> bool:
        """True when `path` sits inside one of the allowed roots."""
        try:
            resolved = path.resolve()
        except OSError:
            return False
        for root in self.allowed_roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except (ValueError, OSError):
                continue
        return False
