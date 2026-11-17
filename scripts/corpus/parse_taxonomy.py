#!/usr/bin/env python3
"""Shared parser-outcome taxonomy for the corpus tooling (issue #4830).

Extracted from ``probe_open_schematics.py`` (slice 1) so that the manifest
tooling (slice 2) classifies parse outcomes *identically* instead of growing a
second, drifting copy of the same buckets.

This module is **pure and offline**: no network, no CLI, no repo writes outside
the caller-supplied scratch directory. That is deliberate — it is the one piece
of ``scripts/corpus/`` that ``tests/`` is allowed to import (the network scripts
are not).

Buckets are chosen so that each one maps to a *different engineering action*.
Resist the temptation to add a bucket that cannot be acted on differently from
an existing one -- a taxonomy whose classes all mean "fix the parser" is just a
catch-all with extra steps.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from kicad_tools.exceptions import FileFormatError, KiCadToolsError
from kicad_tools.schema import PCB, Schematic
from kicad_tools.sexp import ParseError as SexpParseError

OK = "ok"

# Parser defects / gaps -- these are the actionable ones.
FAIL_SEXPR_SYNTAX = "sexpr-syntax-error"  # tokenizer/grammar rejected the file
FAIL_WRONG_ROOT = "wrong-root-token"  # parsed, but root tag is not what we expect
FAIL_ENCODING = "encoding-error"  # not decodable as UTF-8
FAIL_MISSING_NODE = "missing-node-or-atom"  # KeyError/IndexError walking the tree
FAIL_BAD_VALUE = "unexpected-value"  # ValueError/TypeError converting an atom
FAIL_TREE_SHAPE = "unexpected-tree-shape"  # AttributeError on an unexpected node
FAIL_RECURSION = "recursion-limit"  # nesting deeper than CPython's stack
FAIL_MEMORY = "out-of-memory"
FAIL_KCT = "kicad-tools-error"  # any other KiCadToolsError subclass
FAIL_OTHER = "uncaught-internal-error"  # everything else: triage individually

# Not parser defects -- excluded from the failure rate, counted separately.
SKIP_NON_KICAD = "skipped-non-kicad-format"  # legacy .sch / Altium / unknown blob
SKIP_EMPTY = "skipped-empty-payload"
SKIP_OVERSIZE = "skipped-oversize-payload"
SKIP_TRUNCATED = "skipped-dataset-truncated-cell"  # dataset API truncated the row
NET_ERROR = "network-error"  # record could not be fetched at all

# Manifest-runner specific outcomes (slice 2). Neither is a parser defect: the
# first says the payload was never fetched, the second says the bytes we got
# are not the bytes the manifest pinned -- both are integrity signals, and
# scoring parser health on them would be a lie.
MISSING_PAYLOAD = "missing-cached-payload"
HASH_MISMATCH = "payload-hash-mismatch"

FAILURE_CLASSES = frozenset(
    {
        FAIL_SEXPR_SYNTAX,
        FAIL_WRONG_ROOT,
        FAIL_ENCODING,
        FAIL_MISSING_NODE,
        FAIL_BAD_VALUE,
        FAIL_TREE_SHAPE,
        FAIL_RECURSION,
        FAIL_MEMORY,
        FAIL_KCT,
        FAIL_OTHER,
    }
)

SKIP_CLASSES = frozenset(
    {
        SKIP_NON_KICAD,
        SKIP_EMPTY,
        SKIP_OVERSIZE,
        SKIP_TRUNCATED,
        NET_ERROR,
        MISSING_PAYLOAD,
        HASH_MISMATCH,
    }
)

_VERSION_RE = re.compile(r"\(\s*version\s+(\d+)\s*\)")
_DIGITS_RE = re.compile(r"\d+")

SUFFIX_FOR_KIND = {"schematic": ".kicad_sch", "pcb": ".kicad_pcb"}
FORMAT_FOR_KIND = {"schematic": "kicad_sch", "pcb": "kicad_pcb"}


def detect_format(text: str) -> str:
    """Sniff what kind of file a payload actually is, from its first bytes.

    The dataset's ``extensions_used`` field is per-record, not per-file, so the
    only reliable per-payload signal is the content itself.
    """
    head = text[:4096].lstrip()
    if head.startswith("(kicad_sch"):
        return "kicad_sch"
    if head.startswith("(kicad_pcb"):
        return "kicad_pcb"
    if head.startswith("(kicad_"):
        token = head[1:].split(None, 1)[0].strip("()")
        return f"kicad-other:{token}"
    if head.startswith("EESchema"):
        return "legacy-eeschema"
    if head.startswith("PCBNEW-BOARD") or head.startswith("(module"):
        return "legacy-pcbnew"
    if "|RECORD=" in head or head.startswith("\x00") or "Altium" in head[:512]:
        return "altium"
    if not head:
        return "empty"
    return "unknown"


def declared_version(text: str) -> str | None:
    """Extract the KiCad file-format version token (e.g. ``20231120``)."""
    match = _VERSION_RE.search(text[:4096])
    return match.group(1) if match else None


def classify_exception(exc: BaseException) -> str:
    """Map a parse exception onto a taxonomy bucket."""
    if isinstance(exc, RecursionError):
        return FAIL_RECURSION
    if isinstance(exc, MemoryError):
        return FAIL_MEMORY
    if isinstance(exc, UnicodeError):
        return FAIL_ENCODING
    if isinstance(exc, FileFormatError):
        return FAIL_WRONG_ROOT
    if isinstance(exc, SexpParseError):
        return FAIL_SEXPR_SYNTAX
    if isinstance(exc, KiCadToolsError):
        return FAIL_KCT
    if isinstance(exc, KeyError | IndexError):
        return FAIL_MISSING_NODE
    if isinstance(exc, AttributeError):
        return FAIL_TREE_SHAPE
    if isinstance(exc, ValueError | TypeError):
        return FAIL_BAD_VALUE
    return FAIL_OTHER


def signature_of(exc: BaseException) -> str:
    """Digit-normalised one-line exception signature, for grouping offenders.

    ``Expected atom at position 4172`` and ``Expected atom at position 91``
    are the same defect; collapsing the digits makes the "worst offenders"
    table count them together.
    """
    first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    normalised = _DIGITS_RE.sub("N", first_line)
    return f"{exc.__class__.__name__}: {normalised[:160]}"


def load_kicad_file(path: Path, kind: str) -> None:
    """Load ``path`` with this repo's parser for ``kind`` (raises on failure)."""
    if kind == "schematic":
        Schematic.load(path)
    else:
        PCB.load(path)


def parse_payload(
    text: str,
    kind: str,
    scratch_dir: Path,
    stem: str,
    max_bytes: int,
) -> tuple[str, BaseException | None, float]:
    """Write ``text`` to a scratch file and load it with this repo's parser.

    Returns ``(outcome, exception_or_None, elapsed_ms)``. The scratch file is
    always removed; nothing is left behind outside ``scratch_dir``.
    """
    fmt = detect_format(text)
    size = len(text.encode("utf-8", errors="replace"))
    if fmt == "empty" or not text.strip():
        return SKIP_EMPTY, None, 0.0
    if size > max_bytes:
        return SKIP_OVERSIZE, None, 0.0

    if fmt != FORMAT_FOR_KIND[kind]:
        # Legacy Eeschema, Altium, or a KiCad file of a different type: this
        # repo has no parser for these, so it is not a parser defect.
        return SKIP_NON_KICAD, None, 0.0

    path = scratch_dir / f"{stem}{SUFFIX_FOR_KIND[kind]}"
    started = time.perf_counter()
    try:
        path.write_text(text, encoding="utf-8")
        load_kicad_file(path, kind)
    except BaseException as exc:  # the taxonomy IS the point: bucket, do not propagate
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        return classify_exception(exc), exc, (time.perf_counter() - started) * 1000.0
    finally:
        path.unlink(missing_ok=True)
    return OK, None, (time.perf_counter() - started) * 1000.0


def parse_file(path: Path, kind: str) -> tuple[str, BaseException | None, float]:
    """Parse an on-disk KiCad file (already fetched) and bucket the outcome.

    Used by the manifest runner, where the payload is already sitting in the
    local cache and there is no reason to round-trip it through a scratch copy.
    """
    started = time.perf_counter()
    try:
        load_kicad_file(path, kind)
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        return classify_exception(exc), exc, (time.perf_counter() - started) * 1000.0
    return OK, None, (time.perf_counter() - started) * 1000.0
