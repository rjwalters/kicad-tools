"""Find S-expression tokens in raw text without matching inside strings.

Several text-level fast paths (pad-block extraction, zone removal, "does
this board have zones?" checks) look for a token such as ``(pad `` or
``(zone`` with ``str.find`` or a regex.  Those also match inside quoted
strings -- a footprint ``descr`` of ``"Heatsink pins (pad 4, net HS)"`` or a
zone name of ``"pour (zone keepout, see note"`` -- and then misparse the
board (Issues #6740, #6873).

The helpers here scan with the same string rule as the parser: a string
runs from ``"`` to the next unescaped ``"``, and a backslash escapes the
character after it.  An unterminated string swallows the rest of the text.

Each scan must start *outside* a string (the start of a document or of a
complete S-expression always is).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from functools import lru_cache

# A quoted string, escapes honoured; unterminated runs to the end of text.
_STRING = r'"[^"\\]*(?:\\[\s\S][^"\\]*)*(?:"|\Z)'


@lru_cache(maxsize=64)
def _scanner(pattern: str) -> re.Pattern[str]:
    # Strings come first in the alternation, so a match that begins at a
    # quote always consumes the whole string and the token branch only ever
    # sees text outside strings.
    return re.compile(f"{_STRING}|(?P<tok>{pattern})")


def iter_unquoted(text: str, pattern: str, pos: int = 0) -> Iterator[re.Match[str]]:
    r"""Yield the matches of regex *pattern* in *text* that lie outside strings.

    Each match of *pattern* must start and end outside a string: it may
    consume whole quoted strings (``\(pad\s+"[^"]*"`` is fine), but it must
    not begin inside one or stop partway through one, which would desync the
    scanner's string tracking.  *pos* must be outside a string.
    The yielded matches are for the whole scanner, so use ``m.start("tok")``
    / ``m.end("tok")`` (equal to ``m.start()`` / ``m.end()``) or
    ``m.group("tok")``.
    """
    # Cheap rejection: no occurrence anywhere means none outside strings,
    # and avoids walking every string of a large board.
    if re.compile(pattern).search(text, pos) is None:
        return
    for m in _scanner(pattern).finditer(text, pos):
        if m.group("tok") is not None:
            yield m


def find_unquoted(text: str, token: str, start: int = 0) -> int:
    """Return the index of literal *token* outside strings, or -1.

    The ``str.find`` analogue of :func:`iter_unquoted`.
    """
    if text.find(token, start) == -1:
        return -1
    for m in iter_unquoted(text, re.escape(token), start):
        return m.start()
    return -1


def has_unquoted(text: str, pattern: str) -> bool:
    """Return True if regex *pattern* matches *text* outside every string."""
    return next(iter_unquoted(text, pattern), None) is not None
