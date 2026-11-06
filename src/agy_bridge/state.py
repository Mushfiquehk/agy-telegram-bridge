"""Tiny JSON-backed store for per-chat settings (survives bot restarts)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ChatState:
    conversation_id: str = ""
    cwd: str = ""
    model: str = ""
    effort: str = ""
    mode: str = ""
    yolo: bool | None = None  # None = use config default
    history: list[str] = field(default_factory=list)  # recent conversation ids
    conversations: dict[str, str] = field(default_factory=dict)  # workspace -> last conversation id
    test_commands: dict[str, str] = field(default_factory=dict)  # workspace -> test command
    recent_projects: list[str] = field(default_factory=list)

    def remember_project(self, path: str, keep: int = 8) -> None:
        if path in self.recent_projects:
            self.recent_projects.remove(path)
        self.recent_projects.insert(0, path)
        del self.recent_projects[keep:]

    def remember_conversation(self, conv_id: str, keep: int = 10) -> None:
        if not conv_id:
            return
        if conv_id in self.history:
            self.history.remove(conv_id)
        self.history.insert(0, conv_id)
        del self.history[keep:]


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._chats: dict[str, ChatState] = {}
        self.extras: dict[str, list] = {"tasks": [], "schedules": []}  # raw dicts owned by bot.py
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("could not read %s; starting fresh", self.path)
            return
        known = set(ChatState.__dataclass_fields__)
        for name in ("tasks", "schedules"):
            if isinstance(data.get(name), list):
                self.extras[name] = data[name]
        for chat_id, raw in (data.get("chats") or {}).items():
            if isinstance(raw, dict):
                self._chats[chat_id] = ChatState(**{k: v for k, v in raw.items() if k in known})

    def get(self, chat_id: int | str) -> ChatState:
        key = str(chat_id)
        with self._lock:
            if key not in self._chats:
                self._chats[key] = ChatState()
            return self._chats[key]

    def save(self) -> None:
        with self._lock:
            payload = {"version": 1, "chats": {k: asdict(v) for k, v in self._chats.items()}, **self.extras}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("could not save state to %s", self.path)
            try:
                os.unlink(tmp)
            except OSError:
                pass
