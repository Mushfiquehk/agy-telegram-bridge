from agy_bridge.render import (
    TELEGRAM_LIMIT, chunk_markdown, format_usage, md_to_html, render_chunks, status_html, tool_summary,
)


def test_md_to_html_basics():
    out = md_to_html("# Title\nSome **bold** and *it* and `x<y` and [link](https://e.com/a?b=1&c=2)\n- item")
    assert "<b>Title</b>" in out
    assert "<b>bold</b>" in out and "<i>it</i>" in out
    assert "<code>x&lt;y</code>" in out
    assert '<a href="https://e.com/a?b=1&amp;c=2">link</a>' in out
    assert "• item" in out


def test_code_block_escaped_and_untouched():
    out = md_to_html("```python\nif a < b and **c**:\n    pass\n```")
    assert out.startswith('<pre><code class="language-python">')
    assert "&lt;" in out and "**c**" in out  # no formatting inside code


def test_unterminated_fence():
    assert md_to_html("```\nopen").endswith("</code></pre>")


def test_html_injection_escaped():
    out = md_to_html("<script>alert(1)</script> & stuff")
    assert "<script>" not in out and "&lt;script&gt;" in out and "&amp;" in out


def test_snake_case_not_italicized():
    assert "<i>" not in md_to_html("call my_func_name and 2*3*4")


def test_chunking_respects_limit_and_fences():
    md = "intro\n```js\n" + "\n".join(f"console.log({i});" for i in range(800)) + "\n```\noutro"
    chunks = chunk_markdown(md, limit=1000)
    assert len(chunks) > 5
    for c in chunks:
        assert len(c) <= 1000
        assert c.count("```") % 2 == 0, c[:80]
    assert chunks[1].startswith("```js")
    for html_text, _ in render_chunks(md):
        assert len(html_text) < TELEGRAM_LIMIT


def test_chunk_very_long_line():
    chunks = chunk_markdown("x" * 10000, limit=1000)
    assert all(len(c) <= 1000 for c in chunks) and sum(len(c) for c in chunks) == 10000


def test_tool_summary():
    assert tool_summary("run_command", {"CommandLine": "git status"}) == "⚡ run_command: git status"
    assert tool_summary("view_file", {"AbsolutePath": "C:\\a\\b.py"}).startswith("📄 view_file: C:\\a\\b.py")
    assert tool_summary("weird", {}) == "🔧 weird"
    assert len(tool_summary("x", {"a": "y" * 500})) < 110


def test_status_html_truncates():
    s = status_html("Working", [f"line {i}" for i in range(30)], preview="<b>p</b>" * 500)
    assert "18 earlier step(s)" in s and "&lt;b&gt;" in s and len(s) < TELEGRAM_LIMIT


def test_format_usage():
    assert format_usage({"total_tokens": 11072, "input_tokens": 10415, "output_tokens": 657}) == "11,072 tok · in 10,415 / out 657"
    assert format_usage({}) == ""
