"""Tiny Markdown parser for the chat panel (no bpy dependency).

Blender's UI can only draw plain labels, so this turns Markdown into a flat
list of blocks that ``ui.py`` lays out with boxes, splits and separators.
Inline styling (bold, italics, code) is reduced to plain text; links are
collected so the panel can show them as clickable buttons.

Block tuples:
    ("heading", level, text)
    ("para", text)
    ("item", depth, marker, text)      # bullet or numbered list item
    ("code", lang, text)
    ("quote", text)
    ("table", header_cells, rows)      # rows: list of cell lists
    ("rule",)
"""

import re

_LINK = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITAL_STAR = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])")
_ITAL_UND = re.compile(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_CODE = re.compile(r"(`+)(.+?)\1")
_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~>])")

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
_TASK = re.compile(r"^\[([ xX])\]\s+")
_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+#.-]*)\s*$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def inline(text):
    """Return (plain_text, links) for one line/paragraph of Markdown."""
    links = []
    codes = []

    def keep_code(m):  # protect code spans from the other rules
        codes.append(m.group(2).strip())
        return f"\x00{len(codes) - 1}\x00"

    def link(m):
        label, url = m.group(1).strip(), m.group(2)
        if url.startswith(("http://", "https://")):
            links.append((label or url, url))
        return label or url

    def autolink(m):
        links.append((m.group(1), m.group(1)))
        return m.group(1)

    s = _CODE.sub(keep_code, text)
    s = _ESCAPE.sub(lambda m: f"\x01{ord(m.group(1))}\x01", s)
    s = _LINK.sub(link, s)
    s = _AUTOLINK.sub(autolink, s)
    s = _BOLD.sub(r"\2", s)
    s = _STRIKE.sub(r"\1", s)
    s = _ITAL_STAR.sub(r"\1", s)
    s = _ITAL_UND.sub(r"\1", s)
    s = re.sub(r"\x01(\d+)\x01", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], s)
    return s, links


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [inline(c.strip())[0] for c in re.split(r"(?<!\\)\|", line)]


def parse(text):
    lines = str(text).replace("\r\n", "\n").replace("\t", "    ").split("\n")
    blocks = []
    para = []

    def flush_para():
        if para:
            blocks.append(("para", inline(" ".join(p.strip() for p in para))[0]))
            para.clear()

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        fence = _FENCE.match(line)
        if fence:
            flush_para()
            marker, lang = fence.group(1), fence.group(2)
            body = []
            i += 1
            while i < n and not lines[i].strip().startswith(marker[:3]):
                body.append(lines[i])
                i += 1
            blocks.append(("code", lang, "\n".join(body)))
            i += 1  # skip closing fence (or run off the end if unclosed)
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        if _RULE.match(line) and not para:
            blocks.append(("rule",))
            i += 1
            continue

        m = _HEADING.match(stripped)
        if m:
            flush_para()
            blocks.append(("heading", len(m.group(1)), inline(m.group(2))[0]))
            i += 1
            continue

        # Setext headings: "Title\n====" / "Title\n----"
        if para and re.match(r"^\s*(=+|-+)\s*$", line) and len(para) == 1:
            level = 1 if stripped.startswith("=") else 2
            blocks.append(("heading", level, inline(para[0].strip())[0]))
            para.clear()
            i += 1
            continue

        if "|" in stripped and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            flush_para()
            header = _cells(stripped)
            rows = []
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            blocks.append(("table", header, rows))
            continue

        if stripped.startswith(">"):
            flush_para()
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            blocks.append(("quote", inline(" ".join(q for q in quote if q))[0]))
            continue

        m = _ITEM.match(line)
        if m:
            flush_para()
            indent, marker, body = m.groups()
            depth = len(indent) // 2
            item_lines = [body]
            i += 1
            # lazy continuation lines (indented, not a new item/block)
            while i < n and lines[i].strip() and lines[i].startswith(" ") \
                    and not _ITEM.match(lines[i]) and not _FENCE.match(lines[i]):
                item_lines.append(lines[i].strip())
                i += 1
            body = " ".join(item_lines)
            task = _TASK.match(body)
            if task:
                marker = "☑" if task.group(1).lower() == "x" else "☐"
                body = body[task.end():]
            elif not marker[0].isdigit():
                marker = "•" if depth == 0 else "◦"
            else:
                marker = marker.rstrip(")").rstrip(".") + "."
            blocks.append(("item", depth, marker, inline(body)[0]))
            continue

        para.append(line)
        i += 1

    flush_para()
    return blocks


def links_in(text):
    """All http(s) links in the text, in order, de-duplicated."""
    seen, out = set(), []
    for line in str(text).splitlines():
        for label, url in inline(line)[1]:
            if url not in seen:
                seen.add(url)
                out.append((label, url))
    return out
