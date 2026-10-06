"""Copy schematic pin electrical types onto PCB pads (issue #5985).

KiCad's "Update PCB from Schematic" writes two children onto every pad of a
footprint that has a schematic symbol::

    (pad "1" smd rect ... (net 3 "+3V3") (pinfunction "VDD") (pintype "power_in") ...)

``pinfunction`` is the symbol pin *name* and ``pintype`` its electrical type
(``power_in``, ``power_out``, ``passive``, ``input``, ...).  For a pin that
carries a schematic no-connect flag (the ``X`` marker) KiCad appends
``+no_connect`` to the type (``passive+no_connect``).

The suffix is a property of the *schematic*, not of the pad's PCB net: KiCad
gives every unconnected pin a single-pad ``unconnected-(REF-PIN-PadN)`` net
whether or not it is flagged, and writes the suffix only for flagged pins
(KiCad 10's Edgeberry and STM32 Nucleo-64 templates have unflagged
``unconnected-(...)`` pads typed plain ``passive``/``output``).  Deriving it
from the schematic alone also makes both entry points below agree regardless
of how -- or whether -- nets have been assigned yet.

The kct board generators build ``.kicad_pcb`` files directly rather than
through pcbnew, so until this module they never carried that data and the
pin-type rail evidence in :func:`kicad_tools.explain.mistakes.power_pin_nets`
(issue #5939) never ran on the project's own boards.

Two entry points share one schematic-derived map:

* :func:`annotate_pcb_pintypes` edits a loaded :class:`~kicad_tools.schema.pcb.PCB`
  (in-memory pads and the S-expression tree).  Used by ``kct pcb
  sync-netlist`` after it assigns nets.
* :func:`annotate_pcb_file_pintypes` rewrites a ``.kicad_pcb`` file **in
  place, preserving its existing text byte-for-byte** apart from the inserted
  or replaced ``pinfunction``/``pintype`` children.  Board generators call it
  right after writing the unrouted PCB; the router merges tracks into that
  text, so the routed PCB inherits the annotation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

logger = logging.getLogger(__name__)

__all__ = [
    "PadPinInfo",
    "PinTypeAnnotation",
    "annotate_pcb_file_pintypes",
    "annotate_pcb_pintypes",
    "pad_pin_info_map",
    "schematic_pin_info",
]

#: Suffix KiCad appends to the pin type of an unconnected pin.
NO_CONNECT_SUFFIX = "+no_connect"


@dataclass(frozen=True)
class PadPinInfo:
    """Schematic pin data destined for one PCB pad."""

    pinfunction: str
    pintype: str
    #: The schematic pin carries a no-connect flag (``+no_connect`` suffix).
    no_connect: bool = False

    @property
    def effective_pintype(self) -> str:
        """The ``pintype`` value KiCad writes on the pad."""
        # A pin already typed ``no_connect`` is not suffixed a second time.
        if (
            self.no_connect
            and self.pintype != "no_connect"
            and not self.pintype.endswith(NO_CONNECT_SUFFIX)
        ):
            return self.pintype + NO_CONNECT_SUFFIX
        return self.pintype


@dataclass
class PinTypeAnnotation:
    """Result of an annotation pass."""

    #: Pads whose ``pinfunction``/``pintype`` were written or changed.
    updated: int = 0
    #: Pads that already carried the expected values.
    unchanged: int = 0
    #: ``REF.PAD`` keys the schematic defines but the PCB has no pad for.
    missing_pads: list[str] = field(default_factory=list)

    @property
    def annotated(self) -> int:
        """Pads that carry schematic pin data after the pass."""
        return self.updated + self.unchanged


def _iter_schematic_symbols(sch_path: Path):
    """Yield ``(symbol, mirror, no_connect_points)`` across the hierarchy.

    ``mirror`` is the placed symbol's ``(mirror x|y)`` axis (``""`` if none);
    the :class:`~kicad_tools.schematic.models.Schematic` model does not keep
    it, so it is read from the raw sheet text by symbol UUID.
    ``no_connect_points`` is the set of rounded ``(x, y)`` positions of the
    no-connect flags on the symbol's own sheet.

    A sheet *file* is read once even when it is placed several times; the
    extra placements' references are not resolved (follow-up to #5985).
    """
    from kicad_tools.operations.netlist import _get_sheet_entries
    from kicad_tools.schematic.models import Schematic
    from kicad_tools.sexp import parse_file

    visited: set[Path] = set()
    stack = [sch_path]
    while stack:
        path = stack.pop()
        resolved = path.resolve()
        if resolved in visited or not path.exists():
            continue
        visited.add(resolved)
        sch = Schematic.load(str(path))
        nc_points = {(round(nc.x, 2), round(nc.y, 2)) for nc in sch.no_connects}
        mirrors: dict[str, str] = {}
        if nc_points:
            for node in parse_file(path).find_all("symbol"):
                uuid_node = node.find_child("uuid")
                mirror_node = node.find_child("mirror")
                if uuid_node is not None and mirror_node is not None:
                    mirrors[uuid_node.get_string(0) or ""] = mirror_node.get_string(0) or ""
        for sym in sch.symbols:
            yield sym, mirrors.get(getattr(sym, "uuid_str", ""), ""), nc_points
        for entry in _get_sheet_entries(path):
            stack.append(path.parent / entry.filename)


def _pinfunction_for(name: str) -> str:
    # KiCad omits ``pinfunction`` for unnamed pins (name "~" or empty).
    return "" if name in ("", "~") else name


def _placed_pin_xy(sym, pin, mirror: str) -> tuple[float, float]:
    """Schematic position of *pin*'s connection point on the placed *sym*.

    Same transform as :meth:`SymbolInstance.pin_position`, plus the symbol's
    mirror, which KiCad applies in library (Y-up) coordinates *before* the
    rotation: ``(mirror x)`` flips across the X axis (negates Y), ``(mirror
    y)`` across the Y axis (negates X).
    """
    import math

    x, y = pin.connection_point()
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    rad = math.radians(getattr(sym, "rotation", 0) or 0)
    rx = x * math.cos(rad) - y * math.sin(rad)
    ry = x * math.sin(rad) + y * math.cos(rad)
    return (round(sym.x + rx, 2), round(sym.y - ry, 2))


def _pin_has_no_connect_flag(sym, pin, mirror: str, nc_points: set[tuple[float, float]]) -> bool:
    """Whether a no-connect flag sits on *pin* of the placed symbol *sym*."""
    if not nc_points:
        return False
    # Only the placed unit's pins (and unit-0 commons) have a real position.
    pin_unit = getattr(pin, "unit", 0)
    if pin_unit not in (0, getattr(sym, "unit", 1)):
        return False
    try:
        return _placed_pin_xy(sym, pin, mirror) in nc_points
    except Exception:  # pragma: no cover - defensive: odd library geometry
        return False


def _merge(old: PadPinInfo | None, new: PadPinInfo) -> PadPinInfo:
    """Combine two schematic pins that land on one key (stacked/multi-unit)."""
    if old is None:
        return new
    flagged = old.no_connect or new.no_connect
    if old.pintype == "passive" and new.pintype != "passive":
        return PadPinInfo(new.pinfunction, new.pintype, flagged)
    return PadPinInfo(old.pinfunction, old.pintype, flagged)


def schematic_pin_info(sch_path: str | Path) -> dict[tuple[str, str], PadPinInfo]:
    """Map ``(reference, schematic pin number)`` to the pin's name and type.

    Walks the full sheet hierarchy and reads each symbol's embedded library
    definition.  Power symbols (``#PWR``/``#FLG`` references) never reach the
    PCB and are skipped.  When a symbol defines several pins with the same
    number (stacked pins), the first non-``passive`` type wins so a stacked
    ``power_in`` group is not masked by a passive twin.  ``no_connect`` is set
    when a no-connect flag sits on the pin.
    """
    info: dict[tuple[str, str], PadPinInfo] = {}
    for sym, mirror, nc_points in _iter_schematic_symbols(Path(sch_path)):
        ref = getattr(sym, "reference", "") or ""
        if not ref or ref.startswith("#"):
            continue
        symbol_def = getattr(sym, "symbol_def", None)
        if symbol_def is None:
            continue
        for pin in symbol_def.pins:
            if not pin.number:
                continue
            key = (ref, pin.number)
            new = PadPinInfo(
                _pinfunction_for(pin.name),
                pin.pin_type or "passive",
                _pin_has_no_connect_flag(sym, pin, mirror, nc_points),
            )
            info[key] = _merge(info.get(key), new)
    return info


def pad_pin_info_map(sch_path: str | Path, pcb: PCB) -> dict[tuple[str, str], PadPinInfo]:
    """Map ``(reference, pad number)`` to the schematic pin data for that pad.

    Schematic pin numbers are translated to footprint pad numbers with
    :func:`kicad_tools.operations.netlist.build_pin_to_pad_map`, the same
    resolution ``kct pcb sync-netlist`` uses for net assignment.
    """
    from kicad_tools.operations.netlist import build_pin_to_pad_map

    pin_info = schematic_pin_info(sch_path)
    try:
        pin_to_pad = build_pin_to_pad_map(sch_path, pcb)
    except Exception as exc:  # pragma: no cover - defensive, mirrors sync-netlist
        logger.warning("pin-to-pad map failed, assuming identity: %s", exc)
        pin_to_pad = {}

    result: dict[tuple[str, str], PadPinInfo] = {}
    for (ref, pin), data in pin_info.items():
        pad = pin_to_pad.get((ref, pin), pin)
        key = (ref, pad)
        result[key] = _merge(result.get(key), data)
    return result


# ---------------------------------------------------------------------------
# Object-level annotation (PCB + S-expression tree)
# ---------------------------------------------------------------------------


def annotate_pcb_pintypes(
    pcb: PCB,
    sch_path: str | Path,
    pad_info: dict[tuple[str, str], PadPinInfo] | None = None,
) -> PinTypeAnnotation:
    """Write ``pinfunction``/``pintype`` onto every pad of *pcb* from *sch_path*.

    Updates both ``Pad.pintype`` and the pad's S-expression node so the change
    reaches :meth:`PCB.save`.  Pads with no schematic pin (mounting holes,
    footprints with no symbol) are left untouched.
    """
    from kicad_tools.sexp import SExp

    if pad_info is None:
        pad_info = pad_pin_info_map(sch_path, pcb)
    result = PinTypeAnnotation()
    seen: set[tuple[str, str]] = set()

    for fp in pcb.footprints:
        ref = fp.reference
        if not ref:
            continue
        for pad in fp.pads:
            data = pad_info.get((ref, pad.number))
            if data is None:
                continue
            seen.add((ref, pad.number))
            pintype = data.effective_pintype
            node = pad._sexp_node
            current_func = ""
            if node is not None and (fn := node.find_child("pinfunction")) is not None:
                current_func = fn.get_string(0) or ""
            if pad.pintype == pintype and current_func == data.pinfunction:
                result.unchanged += 1
                continue
            pad.pintype = pintype
            result.updated += 1
            if node is None:
                continue
            for name in ("pinfunction", "pintype"):
                while (old := node.find_child(name)) is not None:
                    node.remove(old)
            new_children = []
            if data.pinfunction:
                new_children.append(SExp.list("pinfunction", SExp.quoted_atom(data.pinfunction)))
            new_children.append(SExp.list("pintype", SExp.quoted_atom(pintype)))
            net_node = node.find_child("net")
            if net_node is not None:
                idx = next(i for i, c in enumerate(node.children) if c is net_node) + 1
                for offset, child in enumerate(new_children):
                    node.insert(idx + offset, child)
            else:
                for child in new_children:
                    node.append(child)

    result.missing_pads = sorted(
        f"{ref}.{pad}" for ref, pad in pad_info.keys() - seen if pcb.get_footprint(ref) is not None
    )
    return result


# ---------------------------------------------------------------------------
# Text-preserving file annotation
# ---------------------------------------------------------------------------


@dataclass
class _Span:
    """A parenthesised list in the source text: ``text[start:end]``."""

    start: int
    end: int
    name: str
    children: list[_Span] = field(default_factory=list)


_NAME_RE = re.compile(r"\(\s*([^\s()\"]+)")


def _scan_spans(text: str) -> _Span:
    """Build a list-span tree for *text* (string- and escape-aware)."""
    root = _Span(0, len(text), "")
    stack: list[_Span] = [root]
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            m = _NAME_RE.match(text, i)
            span = _Span(i, -1, m.group(1) if m else "")
            stack[-1].children.append(span)
            stack.append(span)
        elif c == ")":
            if len(stack) > 1:
                span = stack.pop()
                span.end = i + 1
        i += 1
    return root


_REF_PROPERTY_RE = re.compile(r'\(\s*property\s+"Reference"\s+"((?:[^"\\]|\\.)*)"')
_REF_FPTEXT_RE = re.compile(r'\(\s*fp_text\s+reference\s+"?((?:[^"\\\s)]|\\.)*)"?')
_PAD_NUMBER_RE = re.compile(r'\(\s*pad\s+(?:"((?:[^"\\]|\\.)*)"|([^\s()"]+))')


def _footprint_reference(text: str, fp: _Span) -> str:
    for child in fp.children:
        if child.name == "property":
            if m := _REF_PROPERTY_RE.match(text, child.start):
                return m.group(1)
        elif child.name == "fp_text":
            if m := _REF_FPTEXT_RE.match(text, child.start):
                return m.group(1)
    return ""


def _child_separator(text: str, anchor: int) -> str:
    """Whitespace to put before a new sibling of the child starting at *anchor*.

    Multi-line pads (one child per line, as KiCad writes them) get a newline
    plus the anchor child's indentation; single-line pads get a space.
    """
    i = anchor
    while i > 0 and text[i - 1] in " \t":
        i -= 1
    if i > 0 and text[i - 1] == "\n":
        return "\n" + text[i:anchor]
    return " "


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _iter_footprint_spans(root: _Span):
    for top in root.children:  # the (kicad_pcb ...) list
        for child in top.children:
            if child.name in ("footprint", "module"):
                yield child


def annotate_pcb_file_pintypes(
    pcb_path: str | Path,
    sch_path: str | Path,
    *,
    dry_run: bool = False,
) -> PinTypeAnnotation:
    """Annotate pads in *pcb_path* with schematic pin data, preserving its text.

    Only the ``(pinfunction ...)``/``(pintype ...)`` children of affected pads
    change; every other byte of the file is kept, so hand-formatted generator
    output does not churn.  The new children are inserted directly after the
    pad's ``(net ...)`` child -- where KiCad writes them -- or before the
    pad's closing paren when it has no net.

    Args:
        pcb_path: ``.kicad_pcb`` file to rewrite.
        sch_path: Root ``.kicad_sch`` of the design.
        dry_run: Compute the result without writing the file.
    """
    from kicad_tools.schema.pcb import PCB

    pcb_path = Path(pcb_path)
    text = pcb_path.read_text(encoding="utf-8")
    pad_info = pad_pin_info_map(sch_path, PCB.load(pcb_path))

    result = PinTypeAnnotation()
    seen: set[tuple[str, str]] = set()
    refs_on_board: set[str] = set()
    # (start, end, replacement) edits, applied back-to-front.
    edits: list[tuple[int, int, str]] = []

    for fp in _iter_footprint_spans(_scan_spans(text)):
        ref = _footprint_reference(text, fp)
        if not ref:
            continue
        refs_on_board.add(ref)
        for pad in fp.children:
            if pad.name != "pad":
                continue
            m = _PAD_NUMBER_RE.match(text, pad.start)
            if not m:
                continue
            number = m.group(1) if m.group(1) is not None else m.group(2)
            data = pad_info.get((ref, number))
            if data is None:
                continue
            seen.add((ref, number))

            net_span = next((c for c in pad.children if c.name == "net"), None)
            pintype = data.effective_pintype

            existing = {c.name: c for c in pad.children if c.name in ("pinfunction", "pintype")}
            want = {"pintype": f"(pintype {_quote(pintype)})"}
            if data.pinfunction:
                want["pinfunction"] = f"(pinfunction {_quote(data.pinfunction)})"
            have = {k: text[s.start : s.end] for k, s in existing.items()}
            if have == want:
                result.unchanged += 1
                continue
            result.updated += 1

            # Drop stale children together with the separator in front of
            # them: the preceding spaces/tabs and, on a multi-line pad, the
            # line break too, so the old child's whole line disappears and
            # the insertion below (which brings its own separator) does not
            # leave a blank line behind.
            for span in existing.values():
                start = span.start
                while start > pad.start and text[start - 1] in " \t":
                    start -= 1
                if start > pad.start and text[start - 1] == "\n":
                    start -= 1
                    if start > pad.start and text[start - 1] == "\r":
                        start -= 1
                edits.append((start, span.end, ""))
            if net_span is not None:
                at = net_span.end
                anchor = net_span.start
            else:
                at = pad.end - 1
                while at > pad.start and text[at - 1] in " \t\r\n":
                    at -= 1
                anchor = pad.children[-1].start if pad.children else at
            sep = _child_separator(text, anchor)
            insertion = "".join(sep + want[k] for k in ("pinfunction", "pintype") if k in want)
            edits.append((at, at, insertion))

    result.missing_pads = sorted(
        f"{ref}.{pad}" for ref, pad in pad_info.keys() - seen if ref in refs_on_board
    )

    if edits and not dry_run:
        # Edits never overlap; an insertion and a deletion may share a start
        # offset, so order by (start, end): the zero-width insertion first.
        edits.sort(key=lambda e: (e[0], e[1]))
        pieces: list[str] = []
        cursor = 0
        for start, end, repl in edits:
            pieces.append(text[cursor:start])
            pieces.append(repl)
            cursor = max(cursor, end)
        pieces.append(text[cursor:])
        pcb_path.write_text("".join(pieces), encoding="utf-8")
    return result
