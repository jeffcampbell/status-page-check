"""Self-contained HTML report rendering.

Converts the markdown report — a known, well-formed subset emitted by
report.py plus LLM-written sections — into a single HTML file with inline
CSS and a table of contents, shareable with people who won't read raw
markdown. Deliberately not a general markdown engine: it supports exactly
the constructs the report uses (headings, tables, lists, code fences,
blockquotes, bold/italic/links/inline code) and degrades to paragraphs
for anything else.
"""

import html as html_lib
import re

_CSS = """
body { font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
       Helvetica, Arial, sans-serif; color: #1f2328; background: #fff;
       max-width: 920px; margin: 2rem auto; padding: 0 1.5rem; }
h1 { font-size: 1.9em; line-height: 1.25; }
h2 { border-bottom: 1px solid #d1d9e0; padding-bottom: .3em; margin-top: 2em; }
h3 { margin-top: 1.6em; }
table { border-collapse: collapse; margin: 1em 0; display: block;
        overflow-x: auto; }
th, td { border: 1px solid #d1d9e0; padding: 6px 13px; text-align: left; }
thead th { background: #f6f8fa; }
tbody tr:nth-child(even) { background: #f6f8fa; }
pre { background: #f6f8fa; padding: 12px 16px; overflow-x: auto;
      border-radius: 6px; font-size: 13px; line-height: 1.45; }
code { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
       monospace; }
p > code, li > code, td > code { background: #f6f8fa; padding: .1em .35em;
       border-radius: 4px; font-size: .9em; }
blockquote { border-left: 4px solid #d1d9e0; color: #59636e; padding: 0 1em;
       margin-left: 0; }
a { color: #0969da; text-decoration: none; }
a:hover { text-decoration: underline; }
.toc { background: #f6f8fa; border-radius: 6px; padding: 1em 1.5em 1.2em;
       font-size: 14px; margin: 1.5em 0; }
.toc ul { margin: .5em 0 0; padding-left: 1.2em; list-style: none; }
.toc li { margin: .15em 0; }
.toc li.toc-sub { margin-left: 1.2em; }
"""

_LIST_ITEM_RE = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_STRUCTURAL_RE = re.compile(r"^(#{1,4}\s|\||```|>|\s*[-*]\s|\s*\d+\.\s)")


def _inline(text):
    """Escape HTML, then apply the report's inline markdown constructs."""
    text = html_lib.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return text


def _table_html(rows):
    parsed = [
        [c.strip() for c in row.strip().strip("|").split("|")] for row in rows
    ]
    header, body = None, parsed
    if len(parsed) >= 2 and all(re.fullmatch(r":?-{3,}:?", c) for c in parsed[1]):
        header, body = parsed[0], parsed[2:]
    parts = ["<table>"]
    if header:
        parts.append(
            "<thead><tr>"
            + "".join(f"<th>{_inline(c)}</th>" for c in header)
            + "</tr></thead>"
        )
    parts.append("<tbody>")
    for row in body:
        parts.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _list_html(lines, i):
    """Consume list lines starting at index i. Supports ul/ol and one
    nesting level by indentation. Returns (next_index, html)."""
    parts = []
    stack = []  # (tag, indent)
    while i < len(lines):
        m = _LIST_ITEM_RE.match(lines[i])
        if not m:
            break
        indent = len(m.group(1))
        tag = "ol" if m.group(2)[0].isdigit() else "ul"
        while stack and indent < stack[-1][1]:
            parts.append(f"</{stack.pop()[0]}>")
        if not stack or indent > stack[-1][1]:
            stack.append((tag, indent))
            parts.append(f"<{tag}>")
        elif stack[-1][0] != tag:
            parts.append(f"</{stack.pop()[0]}>")
            stack.append((tag, indent))
            parts.append(f"<{tag}>")
        parts.append(f"<li>{_inline(m.group(3))}</li>")
        i += 1
    while stack:
        parts.append(f"</{stack.pop()[0]}>")
    return i, "".join(parts)


def _blocks(md):
    """Convert markdown to a list of HTML block strings plus a TOC."""
    lines = md.split("\n")
    out = []
    toc = []  # (level, text, id)
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]

        if line.startswith("```"):
            i += 1
            code = []
            while i < n and not lines[i].startswith("```"):
                code.append(html_lib.escape(lines[i]))
                i += 1
            i += 1  # closing fence
            out.append("<pre>" + "\n".join(code) + "</pre>")
            continue

        m = _HEADING_RE.match(line)
        if m:
            level, text = len(m.group(1)), m.group(2)
            if level in (2, 3):
                hid = f"s{len(toc) + 1}"
                toc.append((level, re.sub(r"[*`]", "", text), hid))
                out.append(f'<h{level} id="{hid}">{_inline(text)}</h{level}>')
            else:
                out.append(f"<h{level}>{_inline(text)}</h{level}>")
            i += 1
            continue

        if line.startswith("|"):
            rows = []
            while i < n and lines[i].startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table_html(rows))
            continue

        if line.startswith(">"):
            quote = []
            while i < n and lines[i].startswith(">"):
                quote.append(lines[i].lstrip(">").strip())
                i += 1
            out.append(
                "<blockquote><p>" + _inline(" ".join(quote)) + "</p></blockquote>"
            )
            continue

        if _LIST_ITEM_RE.match(line):
            i, block = _list_html(lines, i)
            out.append(block)
            continue

        if not line.strip():
            i += 1
            continue

        para = [line]
        i += 1
        while i < n and lines[i].strip() and not _STRUCTURAL_RE.match(lines[i]):
            para.append(lines[i])
            i += 1
        out.append("<p>" + _inline(" ".join(para)) + "</p>")
    return out, toc


def render_html(markdown_text, title):
    """Render the markdown report as a self-contained HTML document."""
    blocks, toc = _blocks(markdown_text)

    if toc:
        items = []
        for level, text, hid in toc:
            cls = ' class="toc-sub"' if level == 3 else ""
            items.append(f'<li{cls}><a href="#{hid}">{html_lib.escape(text)}</a></li>')
        toc_html = (
            '<nav class="toc"><strong>Contents</strong><ul>'
            + "".join(items)
            + "</ul></nav>"
        )
        # Place the TOC after the title block (first paragraph if present)
        insert_at = 1
        for idx, block in enumerate(blocks):
            if block.startswith("<p>"):
                insert_at = idx + 1
                break
        blocks.insert(insert_at, toc_html)

    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html_lib.escape(title)}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n" + "\n".join(blocks) + "\n</body>\n</html>\n"
    )
