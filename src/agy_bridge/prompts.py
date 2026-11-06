"""Prompt shortcuts: /review, /fix, /explain, ... plus your own in prompts.toml.

A template may use {args} (whatever follows the command) and {cwd}.
Custom file format (PROMPTS_FILE, default ./prompts.toml):

    [shortcuts.docs]
    description = "Update the README for recent changes"
    prompt = "Read the recent git log and diff and update README.md to match. {args}"
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass
class Shortcut:
    name: str
    description: str
    prompt: str
    mode: str = ""  # run this one turn in a specific agy mode (e.g. plan)

    def render(self, args: str, cwd: str) -> str:
        text = self.prompt.replace("{cwd}", cwd)
        if "{args}" in text:
            text = text.replace("{args}", args.strip())
        elif args.strip():
            text += "\n\n" + args.strip()
        return text.strip()


BUILTIN: dict[str, Shortcut] = {s.name: s for s in [
    Shortcut("review", "Code review of uncommitted changes",
             "Review my uncommitted changes (run `git status` and `git diff HEAD`). Point out bugs, risky changes, "
             "missing tests, and style problems, most important first, with file:line references. Don't change any files. {args}"),
    Shortcut("fix", "Run the tests and fix what fails",
             "Run the project's test suite, find the cause of any failures, fix them, and rerun until they pass. "
             "Summarize what was wrong and what you changed. {args}"),
    Shortcut("explain", "Explain a file, function, or the codebase",
             "Explain {args} in this codebase: what it does, how it fits with the rest, and anything surprising. "
             "If nothing specific is named, give me a tour of the repository's structure. Don't change any files."),
    Shortcut("plan", "Plan only, no edits (runs in plan mode)",
             "Make a step-by-step implementation plan for: {args}\nList the files you'd touch and any open questions. "
             "Don't change any files yet.", mode="plan"),
    Shortcut("commitmsg", "Write a commit message and commit",
             "Look at the staged and unstaged changes (`git status`, `git diff HEAD`), stage the ones that belong together, "
             "and commit them with a clear conventional commit message. Don't push. Show me the message you used. {args}"),
    Shortcut("prdesc", "Write a pull request description",
             "Compare this branch to the default branch (git log and git diff against origin's default branch) and write "
             "a pull request title and description: summary, changes, testing, risks. Don't push or open anything. {args}"),
    Shortcut("debug", "Investigate an error or stack trace",
             "Investigate this problem and find the root cause before changing anything. Reproduce it if you can, then fix it "
             "and explain the cause:\n{args}"),
    Shortcut("tests", "Write tests for something",
             "Write thorough tests for {args}, following the project's existing test conventions, then run them."),
    Shortcut("refactor", "Refactor without changing behavior",
             "Refactor {args} for clarity and maintainability without changing behavior. Run the tests before and after."),
    Shortcut("deps", "Check dependencies for updates and issues",
             "Check this project's dependencies for outdated or vulnerable packages using the ecosystem's own tools "
             "(npm outdated / pip list --outdated / cargo outdated etc.). Report; don't upgrade unless I ask. {args}"),
]}


def load_custom(path: Path) -> dict[str, Shortcut]:
    if not path or not path.exists():
        return {}
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.10
        log.warning("prompts.toml needs Python 3.11+ (tomllib); skipping")
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}
    out: dict[str, Shortcut] = {}
    for name, spec in (data.get("shortcuts") or {}).items():
        name = str(name).lower()
        if not NAME_RE.match(name) or not isinstance(spec, dict) or not isinstance(spec.get("prompt"), str):
            log.warning("skipping invalid shortcut %r in %s", name, path)
            continue
        out[name] = Shortcut(name, str(spec.get("description") or name), spec["prompt"], str(spec.get("mode") or ""))
    return out


def all_shortcuts(path: Path | None) -> dict[str, Shortcut]:
    merged = dict(BUILTIN)
    if path is not None:
        merged.update(load_custom(path))
    return merged
