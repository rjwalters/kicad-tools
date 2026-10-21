"""Resolve LCSC C-numbers for the LCSC 3D-model tier from the board's own data.

Before issue #7686 the tier needed a hand-written, per-board, ``lib_id``-keyed
``lcsc_models.json`` sidecar.  Both committed sidecars went stale, while the
board's BOM and procurement review already carry the C-numbers.  This module
derives the ``lib_id -> C-number`` map from, per footprint reference (first
hit wins):

1. an LCSC/JLCPCB property on the footprint (``LCSC``, ``LCSC Part #``,
   ``JLCPCB Part``, ...);
2. an LCSC/JLCPCB column of the assembly BOM CSV (matched by reference);
3. the BOM row's manufacturer part number looked up in the board's
   ``procurement-review.json`` (``parts[<MPN>].lcsc``).

A ``lib_id`` whose references disagree on the C-number (a shared ``R_0603``
footprint carrying different resistors) is a **conflict**: it is not mapped and
is reported, because a model keyed by ``lib_id`` cannot be right for all of
them.  The sidecar is now an *override only*; :func:`merge_with_sidecar`
layers it over the BOM-derived map and reports stale entries.

Nothing here touches the network.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.pcb.lcsc_models import LcscModelEntry
from kicad_tools.pcb.models3d import iter_footprint_blocks

__all__ = [
    "BOM_FILENAMES",
    "PROCUREMENT_FILENAMES",
    "BomLcscResult",
    "LcscMappingPlan",
    "build_lcsc_mapping",
    "discover_board_inputs",
    "merge_with_sidecar",
    "resolve_bom_lcsc",
]

BOM_FILENAMES: tuple[str, ...] = ("assembly-bom.csv", "bom.csv")
PROCUREMENT_FILENAMES: tuple[str, ...] = ("procurement-review.json",)

_C_NUMBER = re.compile(r"^C\d+$")
_PROPERTY = re.compile(r'\(property\s+"((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"')
_REFERENCE_FP = re.compile(r'\(fp_text\s+reference\s+"((?:[^"\\]|\\.)*)"')
_LCSC_PROPERTY_NAMES = frozenset(
    re.sub(r"[^a-z0-9]", "", n.lower())
    for n in (
        "LCSC",
        "LCSC Part",
        "LCSC Part #",
        "LCSC#",
        "LCSC Part Number",
        "JLCPCB Part",
        "JLCPCB Part #",
        "JLCPCB",
    )
)
_REF_HEADERS = ("reference", "designator", "ref", "refdes")
_MPN_HEADERS = ("mpn", "manufacturerpartnumber", "manufacturerpart", "mfrpartnumber", "mfrpart")


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def _c_number(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    return candidate if _C_NUMBER.match(candidate) else None


@dataclass
class BomLcscResult:
    """Outcome of :func:`resolve_bom_lcsc`."""

    mapping: dict[str, LcscModelEntry] = field(default_factory=dict)
    """``lib_id -> entry`` for every unambiguous lib id."""
    sources: dict[str, str] = field(default_factory=dict)
    """``lib_id -> where the C-number came from`` (provenance for reports)."""
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    """``lib_id -> sorted C-numbers`` for lib ids whose references disagree."""
    inputs: list[str] = field(default_factory=list)
    """Files consulted (BOM CSV, procurement review)."""


def discover_board_inputs(pcb_path: Path) -> tuple[Path | None, Path | None]:
    """Find the BOM CSV and procurement review for a board PCB.

    Looks in the PCB's directory, then its parent (a board's ``output/``
    directory sits one level below the board directory that owns the BOM).
    """
    bom: Path | None = None
    procurement: Path | None = None
    pcb_dir = pcb_path.resolve().parent
    for directory in (pcb_dir, pcb_dir.parent):
        if bom is None:
            bom = next((directory / n for n in BOM_FILENAMES if (directory / n).is_file()), None)
        if procurement is None:
            procurement = next(
                (directory / n for n in PROCUREMENT_FILENAMES if (directory / n).is_file()),
                None,
            )
    return bom, procurement


def _load_procurement(path: Path | None) -> dict[str, str]:
    """``MPN -> C-number`` from a ``procurement-review.json``."""
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    parts = raw.get("parts") if isinstance(raw, dict) else None
    if not isinstance(parts, dict):
        return {}
    out: dict[str, str] = {}
    for mpn, info in parts.items():
        c_number = _c_number(info.get("lcsc")) if isinstance(info, dict) else None
        if c_number:
            out[str(mpn).strip()] = c_number
    return out


@dataclass
class _BomRow:
    lcsc: str | None
    mpn: str | None


def _load_bom(path: Path | None) -> dict[str, _BomRow]:
    """``reference -> row`` from a BOM CSV (comma-separated reference cells expanded)."""
    if path is None:
        return {}
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = {_norm(h): h for h in (reader.fieldnames or [])}
            ref_col = next((headers[h] for h in _REF_HEADERS if h in headers), None)
            mpn_col = next((headers[h] for h in _MPN_HEADERS if h in headers), None)
            lcsc_col = next(
                (orig for norm, orig in headers.items() if "lcsc" in norm or "jlcpcbpart" in norm),
                None,
            )
            rows: dict[str, _BomRow] = {}
            if ref_col is None:
                return rows
            for row in reader:
                lcsc = _c_number(row.get(lcsc_col or "")) if lcsc_col else None
                mpn = (row.get(mpn_col or "") or "").strip() or None if mpn_col else None
                for ref in re.split(r"[,\s]+", row.get(ref_col) or ""):
                    if ref:
                        rows[ref] = _BomRow(lcsc=lcsc, mpn=mpn)
            return rows
    except (OSError, csv.Error, UnicodeDecodeError):
        return {}


def _footprint_facts(pcb_text: str) -> list[tuple[str, str, str | None]]:
    """``(lib_id, reference, C-number-from-footprint-property)`` per footprint."""
    facts: list[tuple[str, str, str | None]] = []
    for block in iter_footprint_blocks(pcb_text):
        body = pcb_text[block.start : block.end + 1]
        reference = ""
        c_number: str | None = None
        for name, value in _PROPERTY.findall(body):
            if name == "Reference":
                reference = value
            elif _norm(name) in _LCSC_PROPERTY_NAMES and c_number is None:
                c_number = _c_number(value)
        if not reference:
            legacy = _REFERENCE_FP.search(body)
            reference = legacy.group(1) if legacy else ""
        facts.append((block.lib_id, reference, c_number))
    return facts


def resolve_bom_lcsc(
    pcb_text: str,
    *,
    bom_csv: Path | None = None,
    procurement: Path | None = None,
) -> BomLcscResult:
    """Derive ``lib_id -> C-number`` for every footprint in *pcb_text*.

    See the module docstring for the per-reference precedence and the conflict
    rule.  Footprints with no resolvable C-number are simply absent.
    """
    bom = _load_bom(bom_csv)
    mpn_to_lcsc = _load_procurement(procurement)
    result = BomLcscResult(inputs=[str(p) for p in (bom_csv, procurement) if p is not None])
    found: dict[str, dict[str, str]] = {}  # lib_id -> {C-number: provenance}
    for lib_id, reference, fp_lcsc in _footprint_facts(pcb_text):
        c_number: str | None = None
        origin = ""
        if fp_lcsc:
            c_number, origin = fp_lcsc, "footprint property"
        else:
            row = bom.get(reference)
            if row is not None and row.lcsc:
                c_number, origin = row.lcsc, f"BOM LCSC column ({reference})"
            elif row is not None and row.mpn and row.mpn in mpn_to_lcsc:
                c_number = mpn_to_lcsc[row.mpn]
                origin = f"procurement review (MPN {row.mpn}, {reference})"
        if c_number:
            found.setdefault(lib_id, {}).setdefault(c_number, origin)
    for lib_id, by_number in found.items():
        if len(by_number) == 1:
            ((c_number, origin),) = by_number.items()
            result.mapping[lib_id] = LcscModelEntry(lcsc=c_number)
            result.sources[lib_id] = origin
        else:
            result.conflicts[lib_id] = sorted(by_number)
    return result


def merge_with_sidecar(
    bom: BomLcscResult,
    sidecar: Mapping[str, LcscModelEntry] | None,
    pcb_text: str,
) -> tuple[dict[str, LcscModelEntry], dict[str, str]]:
    """Layer a sidecar over the BOM-derived map; report stale sidecar entries.

    The sidecar wins per ``lib_id`` (it is an override), except that its
    ``rotate``/``offset``/``sha256`` are also kept when the BOM supplies the
    C-number.  Returns ``(merged mapping, stale)`` where *stale* maps each
    sidecar ``lib_id`` to why it is stale: not on the board at all, or naming a
    different C-number than the BOM does for that footprint.
    """
    merged = dict(bom.mapping)
    stale: dict[str, str] = {}
    if not sidecar:
        return merged, stale
    present = {lib_id for lib_id, _, _ in _footprint_facts(pcb_text)}
    for lib_id, entry in sidecar.items():
        if lib_id not in present:
            stale[lib_id] = "no footprint with this lib_id on the board"
            continue
        derived = bom.mapping.get(lib_id)
        if derived is not None and derived.lcsc != entry.lcsc:
            stale[lib_id] = (
                f"sidecar says {entry.lcsc} but the board data says {derived.lcsc} "
                f"({bom.sources.get(lib_id, 'BOM')})"
            )
        merged[lib_id] = entry
    return merged, stale


@dataclass
class LcscMappingPlan:
    """The effective LCSC mapping for one board plus what was learned building it."""

    mapping: dict[str, LcscModelEntry]
    bom: BomLcscResult
    stale_sidecar: dict[str, str]


def build_lcsc_mapping(
    pcb_path: Path,
    *,
    sidecar: Path | str | None = None,
    use_bom: bool = True,
    bom_csv: Path | None = None,
    procurement: Path | None = None,
) -> LcscMappingPlan:
    """Effective ``lib_id -> entry`` map: BOM-derived, overridden by the sidecar.

    *bom_csv* / *procurement* default to the files discovered next to the PCB.
    Raises ``ValueError`` for a malformed sidecar (see ``load_lcsc_mapping``).
    """
    from kicad_tools.pcb.lcsc_models import load_lcsc_mapping

    pcb_text = pcb_path.read_text()
    override = load_lcsc_mapping(Path(sidecar)) if sidecar is not None else None
    bom = BomLcscResult()
    if use_bom:
        found_bom, found_proc = discover_board_inputs(pcb_path)
        bom = resolve_bom_lcsc(
            pcb_text,
            bom_csv=bom_csv if bom_csv is not None else found_bom,
            procurement=procurement if procurement is not None else found_proc,
        )
    mapping, stale = merge_with_sidecar(bom, override, pcb_text)
    return LcscMappingPlan(mapping=mapping, bom=bom, stale_sidecar=stale)
