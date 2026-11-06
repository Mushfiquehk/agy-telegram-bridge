"""Read and update agy's settings.json permission rules (with a backup)."""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

RULE_OK = re.compile(r"^[a-z_]+\(.{1,200}\)$")


class SettingsError(RuntimeError):
    pass


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsError(
            f"{path} isn't plain JSON (comments or a typo at line {exc.lineno}), so I won't rewrite it. "
            "Add the rule by hand under permissions.allow."
        ) from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path} doesn't hold a JSON object")
    return data


def allow_rules(path: Path) -> list[str]:
    data = _load(path)
    rules = ((data.get("permissions") or {}).get("allow")) or []
    return [r for r in rules if isinstance(r, str)]


def add_allow_rule(path: Path, rule: str) -> bool:
    """Add a rule to permissions.allow. Returns False if it was already there."""
    rule = rule.strip()
    if not RULE_OK.match(rule):
        raise SettingsError("A rule looks like command(git) or command(regex:npm (run|test).*)")
    data = _load(path)
    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        raise SettingsError("permissions in settings.json isn't an object")
    allow = perms.setdefault("allow", [])
    if not isinstance(allow, list):
        raise SettingsError("permissions.allow in settings.json isn't a list")
    if rule in allow:
        return False
    allow.append(rule)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return True


def remove_allow_rule(path: Path, rule: str) -> bool:
    data = _load(path)
    allow = ((data.get("permissions") or {}).get("allow")) or []
    if rule not in allow:
        return False
    allow.remove(rule)
    shutil.copy2(path, path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return True
