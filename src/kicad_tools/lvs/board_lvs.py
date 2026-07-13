"""Board-level LVS (Layout-vs-Schematic) comparator.

For each ``(ref, pad)`` pair present on either side, build a
``dict[(ref, pad), net_name | None]`` from the schematic and another from
the routed PCB, then diff them.  v1 compares as plain strings -- no
rename heuristics, no power-net normalization.  Those belong in the
fleet-wide rollout (issue #3742).

Inputs:

* ``.kicad_sch`` — the full sheet hierarchy is walked (root sheet plus
  every ``(sheet ...)`` sub-sheet); each pin resolves to its label-bound
  net name (``VCC``, ``GND``, ``LED_ANODE``, ...) via
  :meth:`Schematic.get_net_for_pin` on its own sheet, rather than the
  post-merge ``PWR_FLAG`` blob.  A root sheet holding only ``(sheet ...)``
  symbols still binds every sub-sheet pin (issue #4099).
* ``.kicad_pcb`` — walked via :func:`kicad_tools.sexp.parse_file` and the
  ``(footprint ... (pad N ... (net K "NAME")))`` shape.  Pads with no
  ``(net ...)`` child are treated as unconnected (``None``).

The comparator is pure: no logging, no side-effects, no exceptions for
mismatches.  The board recipe is the one that decides whether a dirty
result should fail the build (it raises
:class:`BoardNetlistMismatch`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kicad_tools.sexp import SExp, parse_file


@dataclass(frozen=True)
class LVSMismatch:
    """A single per-pin schematic↔PCB disagreement.

    ``schematic_net`` is ``None`` when the pin is absent from the
    schematic netlist (e.g. a PCB-only pad), and ``pcb_net`` is ``None``
    when the pad is present on the board but has no ``(net ...)`` entry
    (an unconnected pad).
    """

    ref: str
    pad: str
    schematic_net: str | None
    pcb_net: str | None


@dataclass(frozen=True)
class LVSResult:
    """Outcome of comparing a schematic against a routed PCB.

    ``clean`` is ``True`` iff ``mismatches`` is empty.  Construct via
    :func:`compare_netlists`; callers should treat this as read-only.
    """

    clean: bool
    mismatches: tuple[LVSMismatch, ...]


class BoardNetlistMismatch(Exception):
    """Raised by board recipes when LVS reports a mismatch.

    Carries the underlying :class:`LVSResult` on the ``.result``
    attribute so callers (tests, CLI wrappers) can inspect the full
    mismatch list without re-running the comparator.
    """

    def __init__(self, result: LVSResult) -> None:
        self.result = result
        super().__init__(self._format_message(result))

    @staticmethod
    def _format_message(result: LVSResult) -> str:
        if not result.mismatches:
            return "schematic/PCB netlist mismatch (no details)"
        lines = [f"schematic/PCB netlist mismatch ({len(result.mismatches)} pin(s)):"]
        for m in result.mismatches:
            lines.append(f"  {m.ref}.{m.pad}: schematic={m.schematic_net!r} pcb={m.pcb_net!r}")
        return "\n".join(lines)


def _ref_of(fp: SExp) -> str | None:
    """Resolve a footprint's reference designator across serializer dialects.

    The kicad-tools PCB generator emits ``(fp_text reference "R1" ...)``
    while a round-trip through ``kicad-cli`` rewrites the same field as
    ``(property "Reference" "R1" ...)``.  Either form may appear in a
    PCB this code reads, so probe both and return whichever is present.

    Returns ``None`` if neither form is found (which should not happen
    on a well-formed PCB; the caller decides whether to treat that as
    an error).
    """
    for ft in fp.find_all("fp_text"):
        if ft.get_string(0) == "reference":
            ref = ft.get_string(1)
            if ref:
                return ref
    for p in fp.find_all("property"):
        if p.get_string(0) == "Reference":
            ref = p.get_string(1)
            if ref:
                return ref
    return None


def _walk_hierarchy_schematics(sch_path: Path):
    """Yield every ``Schematic`` in a hierarchy, root first, depth-first.

    Follows ``(sheet ...)`` references the same way
    :func:`kicad_tools.operations.netlist._collect_hierarchy_components`
    does — resolving each ``Sheetfile`` relative to its parent's
    directory and guarding against circular references — so the set of
    ``(ref, pad)`` keys spans the full design, not just the root sheet.

    Yielding the loaded ``Schematic`` objects (rather than only their
    net dict) lets the caller enumerate *every* pin declared on *every*
    symbol across the hierarchy, including pins that never connect to a
    net.  That preserves the "floating pins resolve to ``None``, not
    dropped" contract the single-sheet loader had.
    """
    from kicad_tools.operations.netlist import _get_sheet_entries
    from kicad_tools.schematic.models.schematic import Schematic

    visited: set[Path] = set()

    def _walk(path: Path):
        resolved = path.resolve()
        if resolved in visited or not path.exists():
            return
        visited.add(resolved)
        yield Schematic.load(str(path))
        parent_dir = path.parent
        for entry in _get_sheet_entries(path):
            yield from _walk(parent_dir / entry.filename)

    yield from _walk(Path(sch_path))


def _schematic_pin_to_net(sch_path: Path) -> dict[tuple[str, str], str | None]:
    """Build ``{(ref, pad) -> net_name | None}`` for every pin in the schematic.

    Walks the full sheet hierarchy: the root ``.kicad_sch`` plus every
    sub-sheet referenced via ``(sheet ...)``.  On a root sheet that
    contains only ``(sheet ...)`` symbols (all components living in
    sub-sheets — the normal organization for a non-trivial board), the
    previous single-file loader saw zero symbols and bound zero pads,
    making LVS vacuous (issue #4099).  This version resolves every
    sub-sheet pin as well.

    Each pin is resolved via that sheet's :meth:`Schematic.get_net_for_pin`,
    which returns the *label-bound* net name (``VCC``, ``GND``,
    ``LED_ANODE``, ...) rather than collapsing power rails through a
    ``PWR_FLAG`` symbol.  This PWR_FLAG-safety is why the per-sheet
    ``get_net_for_pin`` path is used rather than the merged ``net_dict``
    from ``_collect_hierarchy_components``: the latter unifies every pin
    touching a ``PWR_FLAG`` symbol into a single ``PWR_FLAG`` net, which
    loses the VCC/GND distinction and makes LVS spuriously fail on every
    board that uses PWR_FLAG.  For a single-sheet design (root only) the
    resolution is byte-identical to the previous behaviour — the loop is
    simply applied to each recursed sheet in turn.

    ``None`` indicates a floating pin (declared on the symbol but not
    connected to anything on its sheet), matching the convention used for
    unconnected PCB pads.

    Net names local to each sheet are used as-is.  Hierarchical-label /
    sheet-pin nets therefore resolve to the label name (matching KiCad's
    common netlist naming).  Full cross-instance sheet-pin unification
    (the same sub-sheet placed twice under different parent net contexts)
    is out of scope here (a Phase 2 concern, issue #4099).
    """
    out: dict[tuple[str, str], str | None] = {}
    for sch in _walk_hierarchy_schematics(Path(sch_path)):
        for sym in sch.symbols:
            ref = sym.reference
            if not ref:
                continue
            # ``symbol_def.pins`` is the canonical pin list for this
            # symbol; iterate by pin number so the mapping aligns with the
            # PCB pads (which are also keyed by pin/pad number).
            for pin in sym.symbol_def.pins:
                number = pin.number
                if not number:
                    continue
                out[(ref, number)] = sch.get_net_for_pin(ref, number)
    return out


def _pcb_pin_to_net(pcb_path: Path) -> dict[tuple[str, str], str | None]:
    """Build ``{(ref, pad) -> net_name | None}`` from a routed PCB.

    ``None`` means the pad exists on the board but has no ``(net ...)``
    binding (unconnected).  Pads without a numeric label and footprints
    without a resolvable reference are skipped silently — they cannot
    take part in an LVS comparison.
    """
    doc = parse_file(pcb_path)
    out: dict[tuple[str, str], str | None] = {}
    for fp in doc.find_all("footprint"):
        ref = _ref_of(fp)
        if ref is None:
            continue
        for pad in fp.find_all("pad"):
            pad_num = pad.get_string(0)
            if pad_num is None:
                continue
            net = pad.find("net")
            net_name: str | None
            if net is None:
                net_name = None
            else:
                # ``(net K "NAME")`` — index 0 is the net number, index 1
                # the human-readable name.
                net_name = net.get_string(1)
            out[(ref, pad_num)] = net_name
    return out


def compare_netlists(sch_path: str | Path, pcb_path: str | Path) -> LVSResult:
    """Compare schematic and routed PCB netlists per-pin.

    The comparison is the simplest possible: for every ``(ref, pad)``
    key that appears in either dict, both sides must agree on the net
    name (string equality, no normalization).  Mismatches include:

    * Pin present in schematic, absent from PCB (``pcb_net=None``).
    * Pin present in PCB, absent from schematic (``schematic_net=None``).
    * Pin present on both sides but with different net names.

    The PCB's net-0 default ("no net") is treated as ``None`` on the PCB
    side -- it's the "no connection" sentinel, not a real net name.

    Args:
        sch_path: Path to a root ``.kicad_sch``.  The full sheet
            hierarchy is walked — every ``(sheet ...)`` sub-sheet is
            loaded and its pins are bound, not just the root sheet
            (issue #4099).
        pcb_path: Path to a ``.kicad_pcb`` (routed or unrouted; routing
            does not affect the netlist).

    Returns:
        :class:`LVSResult`.  Always returned -- mismatches are data, not
        exceptions.  Callers (recipes) raise
        :class:`BoardNetlistMismatch` themselves when they want to fail.
    """
    sch_path = Path(sch_path)
    pcb_path = Path(pcb_path)

    sch_map = _schematic_pin_to_net(sch_path)
    pcb_map = _pcb_pin_to_net(pcb_path)

    # The PCB's net 0 — encoded as an empty-string ``(net 0 "")`` net
    # name — is the "no connection" placeholder, not a real net.
    # Collapse it to ``None`` so unconnected pads compare equal to
    # schematic pins that genuinely lack a net (floating pin).
    #
    # KiCad's *explicit* no-connect encoding is normalized the same way:
    # a pad on a pin marked NC in the schematic is emitted with the
    # sentinel net ``unconnected-(<REF>-<PINNAME>-Pad<PAD>)`` (single-pad
    # by construction; kct's DRC ``single_pad_net`` rule already treats it
    # as "explicit no-connect, no action required").  It is only collapsed
    # when the sentinel names *this very pad* — a pad carrying some OTHER
    # pad's unconnected sentinel is a genuine anomaly and still mismatches.
    # A schematic pin that expects a real net over a PCB no-connect also
    # still mismatches (sch side is non-None).
    def _norm_pcb(name: str | None, ref: str, pad: str) -> str | None:
        if name is None or name == "":
            return None
        if name.startswith(f"unconnected-({ref}-") and name.endswith(f"-Pad{pad})"):
            return None
        return name

    mismatches: list[LVSMismatch] = []
    # Stable iteration order: union of keys, sorted by (ref, pad) so the
    # output is deterministic for golden-file tests.
    all_keys = sorted(set(sch_map) | set(pcb_map))
    for key in all_keys:
        ref, pad = key
        sch_net = sch_map.get(key)
        pcb_net = _norm_pcb(pcb_map.get(key), ref, pad)
        if sch_net != pcb_net:
            mismatches.append(
                LVSMismatch(
                    ref=ref,
                    pad=pad,
                    schematic_net=sch_net,
                    pcb_net=pcb_net,
                )
            )

    return LVSResult(clean=not mismatches, mismatches=tuple(mismatches))
