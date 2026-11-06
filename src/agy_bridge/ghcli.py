"""GitHub helpers through the `gh` CLI (optional), with a plain-git fallback.

`gh` uses its own login (`gh auth login` once on the PC), so the bridge never
holds a GitHub token.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import gitops
from .procs import run_argv


def available() -> bool:
    return shutil.which("gh") is not None


async def gh(cwd: Path, *args: str, timeout: float = 90):
    return await run_argv(["gh", *args], cwd, timeout=timeout)


async def remote_web_url(root: Path) -> str:
    """https URL of origin on GitHub, or ''."""
    r = await gitops.git(root, "remote", "get-url", "origin")
    url = r.out.strip() if r.ok else ""
    m = re.match(r"(?:git@github\.com:|https://(?:[^@/]+@)?github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return f"https://github.com/{m.group(1)}" if m else ""


async def compare_url(root: Path, branch: str) -> str:
    base = await remote_web_url(root)
    return f"{base}/compare/{branch}?expand=1" if base else ""


@dataclass
class PrResult:
    ok: bool
    url: str
    output: str


async def create_pr(root: Path, branch: str, title: str = "", body: str = "", draft: bool = True) -> PrResult:
    """Push the branch and open a PR. Falls back to a compare link without gh."""
    push = await gitops.git(root, "push", "-u", "origin", branch, timeout=180)
    if not push.ok:
        return PrResult(False, "", push.out)
    if not available():
        url = await compare_url(root, branch)
        return PrResult(bool(url), url, push.out + "\n(gh CLI not installed; open the link to create the PR)")
    args = ["pr", "create", "--head", branch]
    if title:
        args += ["--title", title, "--body", body or "Opened from Telegram via agy-telegram-bridge."]
    else:
        args += ["--fill"]
    if draft:
        args.append("--draft")
    r = await gh(root, *args)
    m = re.search(r"https://github\.com/\S+/pull/\d+", r.out)
    if not r.ok and "already exists" in r.out:
        view = await gh(root, "pr", "view", branch, "--json", "url", "-q", ".url")
        return PrResult(True, view.out.strip(), r.out)
    return PrResult(r.ok, m.group(0) if m else "", r.out)


@dataclass
class RunInfo:
    name: str
    status: str
    conclusion: str
    url: str
    branch: str
    title: str

    @property
    def done(self) -> bool:
        return self.status == "completed"

    @property
    def icon(self) -> str:
        if not self.done:
            return "🟡"
        return {"success": "✅", "failure": "❌", "cancelled": "⏹", "skipped": "⏭"}.get(self.conclusion, "⚠️")


async def runs(root: Path, branch: str = "", commit: str = "", limit: int = 5) -> list[RunInfo] | None:
    if not available():
        return None
    args = ["run", "list", "--limit", str(limit), "--json", "name,status,conclusion,url,headBranch,displayTitle,headSha"]
    if branch:
        args += ["--branch", branch]
    if commit:
        args += ["--commit", commit]
    r = await gh(root, *args)
    if not r.ok:
        return None
    try:
        data = json.loads(r.out)
    except ValueError:
        return None
    return [RunInfo(d.get("name", ""), d.get("status", ""), d.get("conclusion", "") or "", d.get("url", ""),
                    d.get("headBranch", ""), d.get("displayTitle", "")) for d in data]


async def failed_log(root: Path, url: str, max_chars: int = 8000) -> str:
    m = re.search(r"/runs/(\d+)", url)
    if not m or not available():
        return ""
    r = await gh(root, "run", "view", m.group(1), "--log-failed", timeout=120)
    return r.out[-max_chars:] if r.ok else ""
