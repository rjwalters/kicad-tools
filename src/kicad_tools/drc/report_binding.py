"""Content binding between a DRC/ERC report and the file it checked (issue #7190).

A ``drc_report.json`` / ``erc_report.json`` sitting beside a board says
nothing about *which version* of the board it was generated for.  File
names (#7091) and mtimes cannot tell: a git checkout gives every file the
checkout time, so a months-old committed report looks as fresh as the PCB.

A report is **bound** to its source file when either:

* it records the sha256 of the source file's raw bytes under a top-level
  key (``pcb_sha256`` for DRC, ``schematic_sha256`` for ERC) and that hash
  equals the current file's hash -- ``kct check --output`` and ``kct
  build``'s ERC step write it; or
* (readiness co-binding fallback) the board's ``output/readiness.json``
  ``inputs`` pin *both* the report's current hash and the source file's
  current hash.  Readiness hashes its inputs together, so a report pinned
  alongside the exact PCB bytes was produced for those bytes.  This keeps
  kicad-cli-native reports (which cannot carry our key) bound without
  restamping them.

Consumers apply one rule: *an auto-discovered report that is not bound to
the current file's content can fail a board, but can never pass it.*
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

__all__ = [
    "DRC_HASH_KEY",
    "ERC_HASH_KEY",
    "BindingStatus",
    "binding_status",
    "sha256_file",
    "stamp_report_hash",
]

#: Top-level report key carrying the checked PCB's sha256.
DRC_HASH_KEY = "pcb_sha256"
#: Top-level report key carrying the checked schematic's sha256.
ERC_HASH_KEY = "schematic_sha256"

_READINESS_NAME = "readiness.json"


class BindingStatus(str, Enum):
    """How a report relates to the current content of its source file."""

    #: The report was generated for exactly the current source bytes.
    BOUND = "bound"
    #: The report records a hash, and it is not the current file's hash.
    MISMATCH = "mismatch"
    #: The report records no hash and no readiness record co-binds it.
    UNHASHED = "unhashed"


def sha256_file(path: Path | str) -> str:
    """Return the sha256 hex digest of *path*'s raw bytes.

    Same convention as readiness ``inputs`` (delegates to the readiness
    producer's hasher so the two can never disagree).
    """
    from kicad_tools.cli.readiness_cmd import _sha256_file

    return _sha256_file(Path(path))


def _recorded(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None


def _readiness_input_hash(inputs: dict, rel: str) -> str | None:
    value = inputs.get(rel)
    if isinstance(value, dict):
        value = value.get("sha256")
    return _recorded(value)


def _readiness_co_bound(report_path: Path, source_path: Path, source_hash: str) -> bool:
    """True when a board ``readiness.json`` pins both files' current hashes.

    The readiness record lives at ``<board>/output/readiness.json`` and its
    ``inputs`` keys are relative to ``<board>``.  Both the report's and the
    source's directory ancestry are searched (nearest first).  Any read or
    shape problem is "not co-bound", never an exception.
    """
    report_path = report_path.resolve()
    source_path = source_path.resolve()
    seen: set[Path] = set()
    for start in (report_path.parent, source_path.parent):
        for board_dir in (start, start.parent, start.parent.parent):
            readiness = board_dir / "output" / _READINESS_NAME
            if readiness in seen:
                continue
            seen.add(readiness)
            if not readiness.is_file():
                continue
            try:
                data = json.loads(readiness.read_text())
            except (OSError, ValueError):
                continue
            inputs = data.get("inputs") if isinstance(data, dict) else None
            if not isinstance(inputs, dict):
                continue
            try:
                report_rel = report_path.relative_to(board_dir).as_posix()
                source_rel = source_path.relative_to(board_dir).as_posix()
            except ValueError:
                continue
            pinned_report = _readiness_input_hash(inputs, report_rel)
            pinned_source = _readiness_input_hash(inputs, source_rel)
            if pinned_report is None or pinned_source is None:
                continue
            if pinned_source != source_hash:
                continue
            try:
                report_hash = sha256_file(report_path)
            except OSError:
                return False
            if pinned_report == report_hash:
                return True
    return False


def binding_status(
    report_path: Path | str,
    source_path: Path | str | None,
    recorded_hash: str | None,
) -> BindingStatus:
    """Classify how *report_path* is bound to *source_path*'s current bytes.

    Args:
        report_path: The DRC/ERC report file on disk.
        source_path: The PCB (DRC) or schematic (ERC) the report should
            describe.  ``None`` or a missing file is ``UNHASHED`` (nothing to
            bind to).
        recorded_hash: The report's top-level ``pcb_sha256`` /
            ``schematic_sha256`` value, or ``None`` when absent.

    Returns:
        ``BOUND`` when the recorded hash matches (or, with no recorded
        hash, readiness co-binds both files), ``MISMATCH`` when a recorded
        hash differs, else ``UNHASHED``.
    """
    if source_path is None:
        return BindingStatus.UNHASHED
    source = Path(source_path)
    try:
        current = sha256_file(source)
    except OSError:
        return BindingStatus.UNHASHED

    recorded = _recorded(recorded_hash)
    if recorded is not None:
        return BindingStatus.BOUND if recorded == current else BindingStatus.MISMATCH

    if _readiness_co_bound(Path(report_path), source, current):
        return BindingStatus.BOUND
    return BindingStatus.UNHASHED


def stamp_report_hash(report_path: Path | str, source_path: Path | str, key: str) -> bool:
    """Add ``key: sha256(source_path)`` to a JSON report in place.

    Used by producers that write a third-party report (kicad-cli's ERC
    JSON) and want it bound.  kicad-cli ignores the extra top-level key;
    our parsers read it.  Returns ``False`` (leaving the file untouched)
    when the report is not a JSON object or either file is unreadable.
    """
    report_path = Path(report_path)
    try:
        data = json.loads(report_path.read_text())
        if not isinstance(data, dict):
            return False
        data[key] = sha256_file(source_path)
        report_path.write_text(json.dumps(data, indent=2) + "\n")
    except (OSError, ValueError):
        return False
    return True
