"""Background tasks in isolated git worktrees (like Codex cloud tasks).

/task <prompt> creates branch ``agy/<slug>-<id>`` in a new worktree next to the
repo, runs a separate agy session there, commits the result, and offers
Diff / Merge / PR / Discard. Your main checkout is never touched while it runs.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import gitops

SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, n: int = 32) -> str:
    s = SLUG_RE.sub("-", text.lower()).strip("-")
    return (s[:n].rstrip("-") or "task")


@dataclass
class Task:
    id: str
    prompt: str
    repo: str  # main repo root
    base_branch: str
    base_commit: str
    branch: str
    path: str  # worktree folder
    chat_key: str
    status: str = "running"  # running | done | failed | merged | pr | discarded
    created: float = field(default_factory=time.time)
    pr_url: str = ""
    summary: str = ""

    @property
    def name(self) -> str:
        return f"#{self.id} {slugify(self.prompt, 24)}"

    def to_dict(self) -> dict:
        return asdict(self)


class TaskError(RuntimeError):
    pass


def worktree_root(repo: Path) -> Path:
    return repo.parent / ".agy-worktrees" / repo.name


async def create(repo_cwd: Path, prompt: str, task_id: str, chat_key: str) -> Task:
    root = await gitops.repo_root(repo_cwd)
    if root is None:
        raise TaskError("Background tasks need a git repository (they run on their own branch).")
    base_commit = await gitops.head(root)
    if not base_commit:
        raise TaskError("The repository has no commits yet.")
    br = await gitops.git(root, "rev-parse", "--abbrev-ref", "HEAD")
    base_branch = br.out.strip() if br.ok else "HEAD"
    branch = f"agy/{slugify(prompt)}-{task_id}"
    path = worktree_root(root) / f"{task_id}-{slugify(prompt, 20)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    r = await gitops.git(root, "worktree", "add", "-b", branch, str(path), base_commit, timeout=180)
    if not r.ok:
        raise TaskError(r.out.strip() or "git worktree add failed")
    return Task(id=task_id, prompt=prompt, repo=str(root), base_branch=base_branch, base_commit=base_commit,
                branch=branch, path=str(path), chat_key=chat_key)


async def commit_all(task: Task, message: str) -> tuple[bool, str]:
    wt = Path(task.path)
    st = await gitops.status(wt)
    if st is None:
        return False, "worktree is gone"
    if st.clean:
        return True, "nothing new to commit"
    r = await gitops.git(wt, "add", "-A")
    if r.ok:
        r = await gitops.git(wt, "-c", "user.useConfigOnly=false", "commit", "-m", message)
    return r.ok, r.out


async def diff(task: Task) -> tuple[str, str]:
    """(stat, full diff) of the task branch against where it started."""
    wt = Path(task.path)
    stat = await gitops.git(wt, "diff", "--stat", task.base_commit, "HEAD")
    full = await gitops.git(wt, "diff", task.base_commit, "HEAD")
    return stat.out, full.out


async def merge(task: Task) -> tuple[bool, str]:
    """Merge the task branch into whatever is checked out in the main repo."""
    root = Path(task.repo)
    st = await gitops.status(root)
    if st is None:
        return False, "main repo not found"
    if st.conflicts:
        return False, "the main checkout has unresolved conflicts"
    r = await gitops.git(root, "merge", "--no-ff", "--no-edit", task.branch, timeout=120)
    if not r.ok:
        await gitops.git(root, "merge", "--abort")
        return False, r.out + "\n(merge aborted; nothing changed. Ask agy to rebase the task branch, or merge by hand.)"
    return True, r.out


async def remove(task: Task, delete_branch: bool = True) -> str:
    root = Path(task.repo)
    out = []
    r = await gitops.git(root, "worktree", "remove", "--force", task.path, timeout=120)
    out.append(r.out.strip())
    await gitops.git(root, "worktree", "prune")
    if delete_branch:
        r = await gitops.git(root, "branch", "-D", task.branch)
        out.append(r.out.strip())
    return "\n".join(x for x in out if x)
