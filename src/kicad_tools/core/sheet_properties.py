"""Spelling-tolerant access to ``(sheet ...)`` file/name properties.

KiCad 6 writes ``(property "Sheet file" ...)`` / ``(property "Sheet name" ...)``;
KiCad 7+ writes ``Sheetfile`` / ``Sheetname`` and still reads both.  Every
walker that follows or labels sub-sheets must accept both spellings, or the
sub-sheets of a KiCad 6 project are silently dropped (issue #7617).
"""

from __future__ import annotations

from kicad_tools.sexp import SExp

SHEET_FILE_KEYS: tuple[str, ...] = ("Sheetfile", "Sheet file")
SHEET_NAME_KEYS: tuple[str, ...] = ("Sheetname", "Sheet name")

# Regex alternations for text-level scanners, derived from the tuples above.
SHEET_FILE_KEY_RE = "(?:Sheetfile|Sheet file)"
SHEET_NAME_KEY_RE = "(?:Sheetname|Sheet name)"


def sheet_property(sheet: SExp, keys: tuple[str, ...]) -> str | None:
    """Value of the first ``(property <key> ...)`` of ``sheet`` whose key is in ``keys``."""
    for prop in sheet.find_all("property"):
        if prop.get_string(0) in keys:
            return prop.get_string(1)
    return None


def sheet_file(sheet: SExp) -> str | None:
    """Sub-sheet filename of a ``(sheet ...)`` node, either spelling."""
    return sheet_property(sheet, SHEET_FILE_KEYS)


def sheet_name(sheet: SExp) -> str | None:
    """Display name of a ``(sheet ...)`` node, either spelling."""
    return sheet_property(sheet, SHEET_NAME_KEYS)
