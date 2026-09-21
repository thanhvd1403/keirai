"""Markdown -> Telegram HTML conversion and message splitting.

Telegram's HTML parse mode supports a fixed tag set:
  <b> <i> <u> <s> <a href> <code> <pre> <blockquote [expandable]> <tg-spoiler>
We convert a practical markdown subset onto that tag set, and split long
messages at Telegram's 4096 char limit without breaking code fences.
"""
import html as _html
import re

TELEGRAM_LIMIT = 4096
QUOTE_OVERHEAD = len("<blockquote expandable></blockquote>")

_FENCE_RE = re.compile(r"^(```|~~~)\s*([^\s`]*)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


# ---------------------------------------------------------------- blocks

def split_blocks(md):
    """Split markdown into blocks: (kind, meta, data).
    kinds: "code" (meta=lang, data=body str), "quote" (data=[lines]),
    "text" (data=[lines]), "blank"."""
    blocks = []
    lines = md.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        m = _FENCE_RE.match(line)
        if m:
            closer, lang = m.group(1), m.group(2)
            j = i + 1
            body = []
            while j < n and not lines[j].startswith(closer):
                body.append(lines[j])
                j += 1
            blocks.append(("code", lang, "\n".join(body)))
            i = j + 1  # skip closing fence (or EOF)
            continue
        if line.startswith(">"):
            qlines = []
            while i < n and lines[i].startswith(">"):
                qlines.append(lines[i].lstrip(">").strip())
                i += 1
            blocks.append(("quote", None, qlines))
            continue
        if line.strip() == "":
            blocks.append(("blank", None, None))
            i += 1
            continue
        plines = []
        while (i < n and lines[i].strip() != ""
               and not _FENCE_RE.match(lines[i]) and not lines[i].startswith(">")):
            plines.append(lines[i])
            i += 1
        blocks.append(("text", None, plines))
    return blocks


# ---------------------------------------------------------------- inline

def _inline_text(text):
    """Convert inline markdown (no code spans) in raw text to Telegram HTML."""
    t = _html.escape(text, quote=False)
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', t)
    t = re.sub(r'(?<!["\'>])\b(https?://[^\s<]+)', r'<a href="\1">\1</a>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"(?<!\w)__([^_]+?)__(?!\w)", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", t)
    t = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", t)
    t = re.sub(r"~~(.+?)~~", r"<s>\1</s>", t, flags=re.S)
    t = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", t, flags=re.S)
    return t


def _inline(seg):
    """Inline conversion with `code` spans protected from formatting."""
    out = []
    pos = 0
    for m in re.finditer(r"`([^`\n]+)`", seg):
        out.append(_inline_text(seg[pos:m.start()]))
        out.append("<code>" + _html.escape(m.group(1), quote=False) + "</code>")
        pos = m.end()
    out.append(_inline_text(seg[pos:]))
    return "".join(out)


def _convert_lines(lines, headings=True):
    parts = []
    for line in lines:
        if headings:
            m = _HEADING_RE.match(line)
            if m:
                parts.append("<b>" + _inline(m.group(2)) + "</b>")
                continue
        parts.append(_inline(line))
    return "\n".join(parts)


# ---------------------------------------------------------------- convert

def convert(md):
    """Convert a complete markdown document to Telegram HTML."""
    out = []
    for kind, meta, data in split_blocks(md):
        if kind == "code":
            inner = _html.escape(data, quote=False)
            out.append(_code_tags(meta, inner))
        elif kind == "quote":
            out.append("<blockquote>" + _convert_lines(data, headings=False) + "</blockquote>")
        elif kind == "text":
            out.append(_convert_lines(data))
    return "\n".join(x for x in out if x)


# ---------------------------------------------------------------- split

def _block_source(block):
    kind, meta, data = block
    if kind == "code":
        return "```%s\n%s\n```" % (meta or "", data)
    if kind == "quote":
        return "\n".join("> " + q for q in data)
    if kind == "blank":
        return ""
    return "\n".join(data)


def convert_block(block):
    return convert(_block_source(block))


def _inline_chunk(source):
    return "\n".join(_inline(l) for l in source.split("\n"))


def _max_prefix(line, limit):
    """Largest k such that _inline(line[:k]) fits in limit."""
    lo, hi = 1, len(line)
    if len(_inline(line[:hi])) <= limit:
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(_inline(line[:mid])) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _code_tags(lang, inner):
    if lang:
        return '<pre><code class="language-%s">%s</code></pre>' % (lang, inner)
    return "<pre><code>%s</code></pre>" % inner


def _hard_split_code(lang, body, limit):
    """Split an oversized code block into fenced pieces that fit.

    Works in escaped space: each line is pre-escaped, so measured
    lengths match the final HTML exactly.
    """
    lines = [_html.escape(l, quote=False) for l in body.split("\n")]
    overhead = len(_code_tags(lang, ""))
    max_inner = max(limit - overhead, 32)
    pieces, cur, cur_len = [], [], 0
    for line in lines:
        while len(line) + 1 > max_inner:  # single monster line
            if cur:
                pieces.append(cur)
                cur, cur_len = [], 0
            pieces.append(line[:max_inner - 1])
            line = line[max_inner - 1:]
        if cur and cur_len + len(line) + 1 > max_inner:
            pieces.append(cur)
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        pieces.append(cur)
    return [_code_tags(lang, "\n".join(p)) for p in pieces]


def _hard_split_text(lines, limit):
    """Split oversized text into source chunks whose conversion fits."""
    out, cur = [], ""
    for line in lines:
        cand = (cur + "\n" + line) if cur else line
        if len(_inline_chunk(cand)) <= limit:
            cur = cand
            continue
        if cur:
            out.append(cur)
            cur = ""
        while len(_inline_chunk(line)) > limit:
            k = _max_prefix(line, limit)
            out.append(line[:k])
            line = line[k:]
        cur = line
    if cur:
        out.append(cur)
    return [_inline_chunk(c) for c in out]


def split(md, limit=TELEGRAM_LIMIT):
    """Split markdown into HTML chunks, each <= limit chars."""
    chunks = []
    cur, cur_len = [], 0
    for block in split_blocks(md):
        if block[0] == "blank":
            continue
        html_text = convert_block(block)
        need = len(html_text) + (1 if cur else 0)
        if cur_len + need <= limit:
            cur.append(html_text)
            cur_len += need
            continue
        if cur:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        if len(html_text) <= limit:
            chunks.append(html_text)
        elif block[0] == "code":
            chunks.extend(_hard_split_code(block[1], block[2], limit))
        else:
            chunks.extend(_hard_split_text(block[2], limit))
    if cur:
        chunks.append("\n".join(cur))
    return chunks if chunks else [""]


def send_parts(answer_md, thinking_md=None, limit=TELEGRAM_LIMIT):
    """Message list for an answer with optional thinking display.

    Thinking goes first, wrapped in expandable blockquotes.
    """
    parts = []
    if thinking_md and thinking_md.strip():
        for c in split(thinking_md, limit - QUOTE_OVERHEAD):
            parts.append("<blockquote expandable>%s</blockquote>" % c)
    parts.extend(split(answer_md, limit))
    return parts
