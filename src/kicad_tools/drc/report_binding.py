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
* (readiness provenance fallback) the board's ``output/readiness.json``
  ``inputs`` pin *both* the report's current hash and the source file's
  current hash, **and** its ``report_sources`` map records that the
  report was produced from the source bytes now on disk
  (``report_sources[<report>] == <source hash>``).  Co-pinning alone proves
  only that the two files sat side by side when readiness hashed them, not
  that the report describes that source (issue #7277), so it no longer
  suffices.  This keeps kicad-cli-native reports (which cannot carry our
  key) bound when the producer records their provenance.

An ERC source is the whole schematic hierarchy: its "hash" is
:func:`schematic_digest`, which equals the plain file hash for a flat
schematic and a combined digest over every sheet otherwise.

Consumers apply one rule: *an auto-discovered report that is not bound to
the current file's content can fail a board, but can never pass it.*
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path

__all__ = [
    "DRC_HASH_KEY",
    "ERC_HASH_KEY",
    "BindingStatus",
    "binding_status",
    "sha256_file",
    "schematic_digest",
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


def schematic_digest(path: Path | str) -> str:
    """Digest of a schematic and every sub-sheet it references (issue #7277).

    A flat schematic digests to its own :func:`sha256_file`, so hashes
    recorded before hierarchy support stay valid.  A hierarchical one digests
    the sorted ``<relpath>\\0<sha256>`` lines of all its sheets, so an edit to
    any sheet changes the value.  Raises ``OSError`` when a referenced sheet is
    missing or lies outside the top sheet's directory.
    """
    from kicad_tools.cli.readiness_cmd import _schematic_hierarchy

    top = Path(path).resolve()
    try:
        sheets = _schematic_hierarchy(top, top.parent)
    except ValueError as exc:
        raise OSError(str(exc)) from exc
    if sheets == [top]:
        return sha256_file(top)
    lines = [
        f"{sheet.relative_to(top.parent).as_posix()}\0{sha256_file(sheet)}" for sheet in sheets
    ]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _source_hash(path: Path, key: str | None) -> str:
    return schematic_digest(path) if key == ERC_HASH_KEY else sha256_file(path)


def _recorded(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None


def _readiness_input_hash(inputs: dict, rel: str) -> str | None:
    value = inputs.get(rel)
    if isinstance(value, dict):
        value = value.get("sha256")
    return _recorded(value)


def _readiness_co_bound(
    report_path: Path, source_path: Path, file_hash: str, source_hash: str
) -> bool:
    """True when a board ``readiness.json`` pins both files and records provenance.

    ``inputs`` must pin the report's and the source file's (*file_hash*)
    current bytes, and ``report_sources`` must record that the report was
    produced from the source's current *source_hash* (issue #7277).

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
            if pinned_source != file_hash:
                continue
            sources = data.get("report_sources")
            produced_from = sources.get(report_rel) if isinstance(sources, dict) else None
            if _recorded(produced_from) != source_hash:
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
    *,
    hierarchical: bool = False,
) -> BindingStatus:
    """Classify how *report_path* is bound to *source_path*'s current bytes.

    Args:
        report_path: The DRC/ERC report file on disk.
        source_path: The PCB (DRC) or schematic (ERC) the report should
            describe.  ``None`` or a missing file is ``UNHASHED`` (nothing to
            bind to).
        recorded_hash: The report's top-level ``pcb_sha256`` /
            ``schematic_sha256`` value, or ``None`` when absent.
        hierarchical: Bind to the whole schematic hierarchy
            (:func:`schematic_digest`) instead of the single file.

    Returns:
        ``BOUND`` when the recorded hash matches (or, with no recorded
        hash, readiness records its provenance), ``MISMATCH`` when a recorded
        hash differs, else ``UNHASHED``.
    """
    if source_path is None:
        return BindingStatus.UNHASHED
    source = Path(source_path)
    try:
        file_hash = sha256_file(source)
        current = _source_hash(source, ERC_HASH_KEY if hierarchical else None)
    except OSError:
        return BindingStatus.UNHASHED

    recorded = _recorded(recorded_hash)
    if recorded is not None:
        return BindingStatus.BOUND if recorded == current else BindingStatus.MISMATCH

    if _readiness_co_bound(Path(report_path), source, file_hash, current):
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
        data[key] = _source_hash(Path(source_path), key)
        report_path.write_text(json.dumps(data, indent=2) + "\n")
    except (OSError, ValueError):
        return False
    return True
