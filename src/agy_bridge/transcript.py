"""Append-only log of each chat's prompts and replies, for /export and /last."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

SAFE = re.compile(r"[^A-Za-z0-9_-]+")


class Transcript:
    def __init__(self, folder: Path, key: str) -> None:
        self.path = Path(folder) / f"{SAFE.sub('_', key)}.jsonl"

    def add(self, role: str, text: str, **meta) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "role": role, "text": text, **meta}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def records(self, limit: int | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out[-limit:] if limit else out

    def last_reply(self) -> str:
        for rec in reversed(self.records(50)):
            if rec.get("role") == "agy":
                return rec.get("text", "")
        return ""

    def to_markdown(self, conversation: str | None = None) -> str:
        lines = ["# agy conversation export", ""]
        for rec in self.records():
            if conversation and rec.get("conversation") not in (None, "", conversation):
                continue
            who = "You" if rec.get("role") == "user" else "agy"
            where = f" · {rec['cwd']}" if rec.get("cwd") else ""
            lines += [f"## {who} · {rec.get('t', '')}{where}", "", rec.get("text", ""), ""]
        return "\n".join(lines)
