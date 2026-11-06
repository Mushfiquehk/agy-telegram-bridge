"""Find the codebases under ALLOWED_ROOTS so you can switch with one tap."""

from __future__ import annotations

import os
from pathlib import Path

SKIP_DIRS = {
    "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "target", ".git",
    ".idea", ".vscode", "AppData", "site-packages", ".cache", ".gradle", "Library", "vendor",
}
MARKERS = (".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "*.sln", "CMakeLists.txt")


def _is_project(path: Path) -> bool:
    for m in MARKERS:
        if "*" in m:
            try:
                if any(path.glob(m)):
                    return True
            except OSError:
                pass
        elif (path / m).exists():
            return True
    return False


def discover(roots: list[Path], max_depth: int = 3, limit: int = 60) -> list[Path]:
    """Project folders (a repo or a folder with a build manifest) under the roots.

    Stops descending at the first project found below a root, so a monorepo
    shows once rather than once per package.
    """
    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            root = root.resolve()
        except OSError:
            continue
        if not root.is_dir():
            continue
        stack = [(root, 0)]
        while stack and len(found) < limit:
            path, depth = stack.pop()
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            if _is_project(path):
                found.append(path)
                if depth > 0:
                    continue  # the root itself may be a repo; still look inside it
            if depth >= max_depth:
                continue
            try:
                children = sorted(
                    (c for c in path.iterdir() if c.is_dir() and not c.name.startswith(".") and c.name not in SKIP_DIRS),
                    key=lambda c: c.name.lower(),
                    reverse=True,
                )
            except OSError:
                continue
            stack.extend((c, depth + 1) for c in children)
    return sorted(found, key=lambda p: p.name.lower())


def match(projects: list[Path], query: str) -> list[Path]:
    """Exact name match first, then prefix, then substring (case-insensitive)."""
    q = query.strip().lower()
    if not q:
        return []
    exact = [p for p in projects if p.name.lower() == q]
    if exact:
        return exact
    pre = [p for p in projects if p.name.lower().startswith(q)]
    if pre:
        return pre
    return [p for p in projects if q in p.name.lower() or q in str(p).lower()]


def detect_test_command(path: Path) -> str:
    """A sensible default test command for the project, or ''."""
    if (path / "package.json").exists():
        try:
            import json

            scripts = json.loads((path / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        if "test" in scripts:
            if (path / "pnpm-lock.yaml").exists():
                return "pnpm test"
            if (path / "yarn.lock").exists():
                return "yarn test"
            if (path / "bun.lockb").exists() or (path / "bun.lock").exists():
                return "bun test"
            return "npm test"
    if (path / "Cargo.toml").exists():
        return "cargo test"
    if (path / "go.mod").exists():
        return "go test ./..."
    if (path / "pyproject.toml").exists() or (path / "pytest.ini").exists() or (path / "setup.py").exists() or (path / "tests").is_dir():
        venv_py = path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        py = f'"{venv_py}"' if venv_py.exists() else ("python" if os.name == "nt" else "python3")
        return f"{py} -m pytest -q"
    if any(path.glob("*.sln")):
        return "dotnet test"
    if (path / "pom.xml").exists():
        return "mvn -q test"
    if (path / "gradlew").exists() or (path / "gradlew.bat").exists():
        return "gradlew.bat test" if os.name == "nt" else "./gradlew test"
    if (path / "Makefile").exists():
        return "make test"
    return ""
