"""Git helpers: repo status, diffs, and per-turn checkpoints with undo.

Checkpoints: before each agy turn the bridge records a snapshot of the working
tree with ``git stash create`` (which writes a commit object but touches
nothing: no stash entry, no index change, no file change) plus the list of
untracked files. After the turn it can show exactly what agy changed, and
"Undo" restores the tracked files from the snapshot and deletes only the
untracked files that agy created.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .procs import ProcResult, run_argv

GIT_TIMEOUT = 60


async def git(cwd: Path, *args: str, timeout: float = GIT_TIMEOUT, stdin: bytes | None = None) -> ProcResult:
    return await run_argv(["git", "-c", "core.quotepath=off", "-c", "color.ui=never", *args], cwd, timeout=timeout, stdin=stdin)


async def repo_root(cwd: Path) -> Path | None:
    r = await git(cwd, "rev-parse", "--show-toplevel")
    if not r.ok:
        return None
    line = r.out.strip().splitlines()[-1] if r.out.strip() else ""
    return Path(line) if line else None


async def head(cwd: Path) -> str:
    r = await git(cwd, "rev-parse", "--verify", "-q", "HEAD")
    return r.out.strip() if r.ok else ""


async def untracked(cwd: Path) -> set[str]:
    r = await git(cwd, "ls-files", "--others", "--exclude-standard", "-z")
    if not r.ok:
        return set()
    return {p for p in r.out.split("\0") if p}


@dataclass
class Checkpoint:
    root: Path
    head: str  # HEAD when the turn started ("" in a repo with no commits)
    tree: str  # commit holding the working tree at turn start
    untracked: set[str] = field(default_factory=set)


async def checkpoint(cwd: Path) -> Checkpoint | None:
    """Snapshot the working tree without modifying anything. None if not a repo."""
    root = await repo_root(cwd)
    if root is None:
        return None
    h = await head(root)
    snap = await git(root, "stash", "create")
    tree = snap.out.strip() if snap.ok and snap.out.strip() else h
    if not tree:
        return None  # empty repo with no commits: nothing to restore from
    return Checkpoint(root=root, head=h, tree=tree, untracked=await untracked(root))


@dataclass
class ChangeSummary:
    files: list[tuple[str, str]]  # (status letter, path)
    added: int = 0
    removed: int = 0
    head_moved: bool = False

    @property
    def empty(self) -> bool:
        return not self.files

    def headline(self) -> str:
        n = len(self.files)
        s = f"📝 {n} file{'s' if n != 1 else ''} changed (+{self.added} −{self.removed})"
        if self.head_moved:
            s += " · new commit(s)"
        return s


async def changes_since(cp: Checkpoint) -> ChangeSummary:
    """What changed in the working tree since the checkpoint (tracked + new files)."""
    root = cp.root
    files: dict[str, str] = {}
    added = removed = 0
    ns = await git(root, "diff", "--name-status", "--no-renames", cp.tree)
    for line in ns.out.splitlines() if ns.ok else []:
        parts = line.split("\t", 1)
        if len(parts) == 2:
            files[parts[1]] = parts[0][:1]
    st = await git(root, "diff", "--numstat", "--no-renames", cp.tree)
    for line in st.out.splitlines() if st.ok else []:
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            added += int(parts[0])
            removed += int(parts[1])
    new = await untracked(root) - cp.untracked
    for path in sorted(new):
        files.setdefault(path, "A")
        try:
            with open(root / path, "rb") as fh:
                data = fh.read(2_000_000)
            if b"\0" not in data:
                added += data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        except OSError:
            pass
    return ChangeSummary(
        files=sorted(((v, k) for k, v in files.items()), key=lambda t: t[1]),
        added=added,
        removed=removed,
        head_moved=(await head(root)) != cp.head,
    )


async def diff_since(cp: Checkpoint, max_new_file_bytes: int = 200_000) -> str:
    """Unified diff of the turn's changes, new untracked files included."""
    root = cp.root
    r = await git(root, "diff", "--no-renames", cp.tree)
    out = [r.out if r.ok else ""]
    for path in sorted(await untracked(root) - cp.untracked):
        nd = await git(root, "diff", "--no-index", "--", "/dev/null", path)  # git special-cases /dev/null on every OS
        text = nd.out
        if len(text) > max_new_file_bytes:
            text = text[:max_new_file_bytes] + "\n… (truncated)\n"
        out.append(text)
    return "".join(out)


async def undo(cp: Checkpoint) -> tuple[bool, str]:
    """Put the working tree back to the checkpoint.

    Refuses if HEAD moved (agy committed): rewriting history is the user's call.
    """
    root = cp.root
    if (await head(root)) != cp.head:
        return False, "HEAD moved since that turn (something was committed). Undo that with git yourself, e.g. /sh git reset --soft HEAD~1."
    created = sorted(await untracked(root) - cp.untracked)
    r = await git(root, "restore", "--source", cp.tree, "--worktree", "--", ".")
    if not r.ok:
        # git < 2.23 has no `restore`
        r = await git(root, "checkout", cp.tree, "--", ".")
        if not r.ok:
            return False, r.out.strip() or "git restore failed"
    removed = 0
    for rel in created:
        p = (root / rel).resolve()
        try:
            p.relative_to(root.resolve())
        except ValueError:
            continue
        try:
            if p.is_file() or p.is_symlink():
                p.unlink()
                removed += 1
        except OSError:
            pass
    msg = "Restored tracked files"
    if removed:
        msg += f" and removed {removed} new file{'s' if removed != 1 else ''}"
    return True, msg + "."


# ----------------------------------------------------------------- status
@dataclass
class RepoStatus:
    branch: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    staged: list[str] = field(default_factory=list)
    unstaged: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.staged or self.unstaged or self.untracked or self.conflicts)


_AB_RE = re.compile(r"\+(\d+) -(\d+)")


async def status(cwd: Path) -> RepoStatus | None:
    r = await git(cwd, "status", "--porcelain=v2", "--branch", "-z")
    if not r.ok:
        return None
    st = RepoStatus()
    entries = r.out.split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if not e:
            continue
        if e.startswith("# branch.head "):
            st.branch = e[len("# branch.head "):]
        elif e.startswith("# branch.upstream "):
            st.upstream = e[len("# branch.upstream "):]
        elif e.startswith("# branch.ab "):
            m = _AB_RE.search(e)
            if m:
                st.ahead, st.behind = int(m.group(1)), int(m.group(2))
        elif e.startswith("1 ") or e.startswith("2 "):
            fields = e.split(" ", 8 if e.startswith("1 ") else 9)
            xy, path = fields[1], fields[-1]
            if e.startswith("2 "):
                i += 1  # rename entries carry the original path as the next item
            if xy[0] != ".":
                st.staged.append(f"{xy[0]} {path}")
            if xy[1] != ".":
                st.unstaged.append(f"{xy[1]} {path}")
        elif e.startswith("u "):
            st.conflicts.append(e.split(" ", 10)[-1])
        elif e.startswith("? "):
            st.untracked.append(e[2:])
    return st


def format_status(st: RepoStatus, root: Path | None = None, limit: int = 25) -> str:
    """Plain-text status (caller escapes/wraps)."""
    lines = []
    br = st.branch or "?"
    if st.upstream:
        ab = []
        if st.ahead:
            ab.append(f"↑{st.ahead}")
        if st.behind:
            ab.append(f"↓{st.behind}")
        br += f" → {st.upstream}" + (f" ({' '.join(ab)})" if ab else " (up to date)")
    else:
        br += " (no upstream)"
    lines.append(f"🌿 {br}")
    if st.clean:
        lines.append("✨ working tree clean")
    for title, items in (("⚔️ conflicts", st.conflicts), ("🟢 staged", st.staged), ("🟡 modified", st.unstaged), ("⚪ untracked", st.untracked)):
        if items:
            lines.append(f"{title} ({len(items)})")
            lines += [f"  {x}" for x in items[:limit]]
            if len(items) > limit:
                lines.append(f"  … {len(items) - limit} more")
    return "\n".join(lines)
