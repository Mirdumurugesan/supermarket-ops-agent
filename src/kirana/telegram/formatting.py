"""Render the model's Markdown into what Telegram actually understands.

The model writes Markdown because that is what models do. Telegram renders
nothing unless you ask for a `parse_mode`, so without this the owner reads
literal `**Bill Finalized**` — asterisks and all.

Telegram's own Markdown modes are the obvious fix and the wrong one. Legacy
`Markdown` breaks on a single unmatched `*` or `_`, and `MarkdownV2` requires
escaping fourteen characters including `.` `-` `(` `)` — every one of which
appears in a normal reply ("₹144.00", "Loose Sugar (2 kg)", "INV-20260907-001").
One unescaped hyphen and the whole message is rejected with a 400, so the owner
sees nothing at all. Trading unrendered asterisks for dropped bills is a bad
trade.

So: convert to Telegram's HTML mode, where exactly three characters are special
and escaping them is unconditional. Everything here is deliberately narrow —
this handles the markup the model actually emits (bold, bullets, code, the odd
heading), not all of CommonMark.
"""

from __future__ import annotations

import html
import re

# Fenced and inline code are lifted out first and put back last, so their
# contents are never treated as markup: `**qty**` inside a code span is text.
_CODE_BLOCK = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*$", re.M)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_UNDERSCORE = re.compile(r"__(.+?)__", re.DOTALL)
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
# Single-asterisk italics only, and only where the asterisk is not touching a
# word character. Single-*underscore* italics are deliberately not supported:
# `product_id` and `@mirdu_kirana_bot` are far more common in these replies
# than emphasis, and mangling them is worse than leaving emphasis unrendered.
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<![\s*])\*(?![\w*])")
_BULLET = re.compile(r"^(\s*)[-*+][ \t]+", re.M)
_HRULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$", re.M)
# "#INV-20260907-001" — Telegram turns the leading # into a blue hashtag link
# to a search for "#INV". The invoice number is not a topic; drop the hash.
_HASHTAG_ID = re.compile(r"#(?=[A-Z]{2,}[-–]?\d)")
_TAG = re.compile(r"<[^>]+>")
# Models sometimes leak a tail of their own JSON envelope onto the end of a
# reply — `Confirm to finalize? 📄"}`. Strip a trailing run of quote/brace/
# bracket characters, but only when the message has more closers than openers,
# so a reply that legitimately ends in `}` (a code span, a dict the owner asked
# about) is left alone.
_JSON_TAIL = re.compile(r"[\s\"'`]*[}\]]+[\s\"'`]*\Z")

_SENTINEL = "\x00{}\x00"


_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^[\s|:\-]+$")


def _render_tables(text: str, keep) -> str:
    """Markdown tables → aligned monospace blocks.

    Telegram has no table markup, so a table the model emits arrives as a wall
    of pipes and dashes — which is exactly what a bill of items looks like when
    the model decides to be tidy. Rendering the columns into a <pre> block gives
    the owner something readable on a phone, and drops the |---|---| rule that
    only ever meant "this is a table" to a Markdown renderer.
    """
    out, block = [], []

    def flush():
        if not block:
            return
        rows = [[c.strip() for c in r.strip().strip("|").split("|")]
                for r in block if not _TABLE_RULE.match(r.strip().strip("|"))]
        block.clear()
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        cols = [max(len(r[i]) for r in rows) for i in range(width)]
        lines = ["  ".join(c.ljust(cols[i]) for i, c in enumerate(r)).rstrip()
                 for r in rows]
        out.append(keep("<pre>" + html.escape("\n".join(lines), quote=False) + "</pre>"))

    for line in text.split("\n"):
        if _TABLE_ROW.match(line):
            block.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


def _strip_json_tail(text: str) -> str:
    if text.count("{") >= text.count("}") and text.count("[") >= text.count("]"):
        return text
    return _JSON_TAIL.sub("", text).rstrip()


def to_telegram_html(text: str) -> str:
    """Markdown as the model writes it → HTML as Telegram parses it."""
    text = _strip_json_tail(text)
    stash: list[str] = []

    def _keep(fragment: str) -> str:
        stash.append(fragment)
        return _SENTINEL.format(len(stash) - 1)

    text = _CODE_BLOCK.sub(
        lambda m: _keep("<pre>" + html.escape(m.group(1).strip("\n"), quote=False) + "</pre>"),
        text)
    text = _INLINE_CODE.sub(
        lambda m: _keep("<code>" + html.escape(m.group(1), quote=False) + "</code>"),
        text)
    text = _render_tables(text, _keep)

    # Only &, < and > are special in Telegram's HTML. Quotes are left alone:
    # escaping them would put &quot; in front of the owner for no reason.
    text = html.escape(text, quote=False)

    text = _HRULE.sub("", text)
    text = _HEADING.sub(r"<b>\1</b>", text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _BOLD_UNDERSCORE.sub(r"<b>\1</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _BULLET.sub("\\1• ", text)          # after bold, so "**x**" is safe
    text = _HASHTAG_ID.sub("", text)

    for i, fragment in enumerate(stash):
        text = text.replace(_SENTINEL.format(i), fragment)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def strip_tags(text: str) -> str:
    """Last-resort plain text: drop the tags, unescape the entities."""
    return html.unescape(_TAG.sub("", text))


def split_for_telegram(text: str, limit: int = 3500) -> list[str]:
    """Split on line boundaries, under Telegram's 4096-character cap.

    Splitting mid-tag would make Telegram reject the whole message, so the
    seams are always newlines; only a single line longer than the limit is cut
    by length, and that is rare enough to accept.
    """
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return [c for c in (c.strip("\n") for c in chunks) if c]
