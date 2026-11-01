"""Turn agy's Markdown output into Telegram-safe HTML messages."""

from __future__ import annotations

import html
import re
from typing import Any, Iterable

TELEGRAM_LIMIT = 4096
# Leave headroom: HTML tags + entity escaping make the rendered text longer
# than the Markdown source.
CHUNK_SOURCE_LIMIT = 3300

_FENCE_RE = re.compile(r"^\s*(```|~~~)\s*([\w+#.-]*)\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")


def _inline(text: str) -> str:
    """Convert inline Markdown in a single non-code line to Telegram HTML."""
    out: list[str] = []
    pos = 0
    # Inline code spans are protected from other formatting.
    for m in _INLINE_CODE_RE.finditer(text):
        out.append(_format_plain(text[pos : m.start()]))
        out.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        pos = m.end()
    out.append(_format_plain(text[pos:]))
    return "".join(out)


def _format_plain(text: str) -> str:
    links: list[str] = []

    def stash_link(m: re.Match[str]) -> str:
        label = html.escape(m.group(1), quote=False)
        url = html.escape(m.group(2), quote=True)
        links.append(f'<a href="{url}">{label}</a>')
        return f"\x00{len(links) - 1}\x00"

    text = _LINK_RE.sub(stash_link, text)
    text = html.escape(text, quote=False)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], text)


def md_to_html(md: str) -> str:
    """Best-effort Markdown -> Telegram HTML (supports the subset Telegram renders)."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_code = False
    fence = ""
    lang = ""
    code: list[str] = []
    for line in lines:
        m = _FENCE_RE.match(line)
        if in_code:
            if m and m.group(1) == fence and not m.group(2):
                body = html.escape("\n".join(code), quote=False)
                cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
                out.append(f"<pre><code{cls}>{body}</code></pre>")
                in_code, code, lang = False, [], ""
            else:
                code.append(line)
            continue
        if m:
            in_code, fence, lang = True, m.group(1), m.group(2)
            continue
        h = _HEADER_RE.match(line)
        if h:
            out.append(f"<b>{_inline(h.group(1))}</b>")
            continue
        b = _BULLET_RE.match(line)
        if b:
            out.append(f"{b.group(1)}• {_inline(b.group(2))}")
            continue
        out.append(_inline(line))
    if in_code:  # unterminated fence
        body = html.escape("\n".join(code), quote=False)
        cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
        out.append(f"<pre><code{cls}>{body}</code></pre>")
    return "\n".join(out).strip()


def chunk_markdown(md: str, limit: int = CHUNK_SOURCE_LIMIT) -> list[str]:
    """Split Markdown into pieces <= limit chars on line boundaries.

    A code fence that spans a split is closed at the end of one chunk and
    reopened (with the same language) at the start of the next, so every chunk
    renders on its own.
    """
    md = md.replace("\r\n", "\n").strip("\n")
    if not md:
        return []
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    open_fence: tuple[str, str] | None = None  # (fence, lang)

    def flush() -> None:
        nonlocal cur, cur_len
        if not cur:
            return
        body = "\n".join(cur)
        if open_fence:
            body += "\n" + open_fence[0]
        chunks.append(body)
        cur = [f"{open_fence[0]}{open_fence[1]}"] if open_fence else []
        cur_len = len(cur[0]) + 1 if cur else 0

    for line in md.split("\n"):
        # Hard-wrap absurdly long single lines.
        pieces = [line[i : i + limit - 20] for i in range(0, len(line), limit - 20)] or [""]
        for piece in pieces:
            extra = len(piece) + 1
            reserve = len(open_fence[0]) + 1 if open_fence else 0
            if cur_len + extra + reserve > limit and cur:
                flush()
            cur.append(piece)
            cur_len += extra
            m = _FENCE_RE.match(piece)
            if m:
                if open_fence is None:
                    open_fence = (m.group(1), m.group(2))
                elif m.group(1) == open_fence[0] and not m.group(2):
                    open_fence = None
    if cur and not (len(cur) == 1 and open_fence and cur[0] == f"{open_fence[0]}{open_fence[1]}"):
        body = "\n".join(cur)
        if open_fence:
            body += "\n" + open_fence[0]
        chunks.append(body)
    return [c for c in chunks if c.strip()]


def render_chunks(md: str) -> list[tuple[str, str]]:
    """Return [(html, plain_fallback), ...] ready to send."""
    return [(md_to_html(c), c) for c in chunk_markdown(md)]


# ------------------------------------------------------------------ tool lines
_PARAM_PREFERENCE = (
    "CommandLine", "command", "Command", "cmd",
    "AbsolutePath", "TargetFile", "FilePath", "file_path", "path", "Path",
    "SearchDirectory", "DirectoryPath", "Query", "query", "Pattern", "pattern",
    "Url", "url", "Uri",
)

_TOOL_ICONS = {
    "run_command": "⚡",
    "write_to_file": "✏️",
    "replace_file_content": "✏️",
    "multi_replace_file_content": "✏️",
    "edit_file": "✏️",
    "view_file": "📄",
    "read_file": "📄",
    "list_dir": "📁",
    "grep_search": "🔎",
    "find_by_name": "🔎",
    "codebase_search": "🔎",
    "search_web": "🌐",
    "read_url_content": "🌐",
    "browser": "🌐",
}


def tool_summary(name: str, params: dict[str, Any], max_len: int = 90) -> str:
    """One compact line describing a tool call."""
    detail = ""
    for key in _PARAM_PREFERENCE:
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            detail = val.strip()
            break
    if not detail:
        for val in params.values():
            if isinstance(val, str) and val.strip():
                detail = val.strip()
                break
    detail = " ".join(detail.split())
    if len(detail) > max_len:
        detail = detail[: max_len - 1] + "…"
    icon = _TOOL_ICONS.get(name, "🔧")
    return f"{icon} {name}" + (f": {detail}" if detail else "")


def status_html(header: str, tool_lines: Iterable[str], preview: str = "", max_tools: int = 12) -> str:
    """The live 'working…' message shown while a turn runs."""
    lines = list(tool_lines)
    parts = [f"<b>{html.escape(header, quote=False)}</b>"]
    if lines:
        hidden = len(lines) - max_tools
        shown = lines[-max_tools:]
        if hidden > 0:
            parts.append(f"<i>… {hidden} earlier step(s)</i>")
        parts.append("\n".join(html.escape(l, quote=False) for l in shown))
    if preview.strip():
        tail = preview.strip()[-600:]
        parts.append(f"<i>{html.escape(tail, quote=False)}</i>")
    text = "\n\n".join(parts)
    return text[: TELEGRAM_LIMIT - 10]


def format_usage(usage: dict[str, Any]) -> str:
    if not usage:
        return ""
    total = usage.get("total_tokens")
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    bits = []
    if total is not None:
        bits.append(f"{total:,} tok")
    if inp is not None and out is not None:
        bits.append(f"in {inp:,} / out {out:,}")
    return " · ".join(bits)
