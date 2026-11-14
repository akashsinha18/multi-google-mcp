"""Text helpers: HTML -> text conversion and truncation."""

from __future__ import annotations

import html as htmllib
import re

import html2text

_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+\n")


def html_to_text(html: str) -> str:
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_images = True
    h.ignore_emphasis = True
    h.ignore_tables = False
    h.unicode_snob = True
    h.protect_links = True
    return normalize_whitespace(h.handle(html))


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_WS.sub("\n", text)
    return _BLANK_LINES.sub("\n\n", text).strip()


# Invisible "preheader padding" that marketing emails use to pad snippets:
# combining grapheme joiner, zero-width space/non-joiner, word joiner, BOM, soft hyphen.
# U+200D (zero-width joiner) is kept: it glues emoji sequences together.
_FILLER = dict.fromkeys(map(ord, "͏​‌⁠﻿­"))
_SPACES = re.compile(r"\s+")


def clean_snippet(snippet: str | None) -> str | None:
    """Gmail snippets are HTML-escaped and often padded with invisible chars."""
    if snippet is None:
        return None
    text = htmllib.unescape(snippet).translate(_FILLER)
    return _SPACES.sub(" ", text).strip()


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Return (text, truncated). Cuts at the last whitespace near the limit."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    cut = text[:limit]
    ws = cut.rfind(" ", int(limit * 0.9))
    nl = cut.rfind("\n", int(limit * 0.9))
    pos = max(ws, nl)
    if pos > 0:
        cut = cut[:pos]
    return cut.rstrip() + "\n…[truncated]", True
