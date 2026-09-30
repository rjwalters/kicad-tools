"""Board-level LVS (Layout-vs-Schematic) comparator.

For each ``(ref, pad)`` pair present on either side, build a
``dict[(ref, pad), net_name | None]`` from the schematic and another from
the routed PCB, then diff them.  Named nets compare as plain strings -- no
rename heuristics, no power-net normalization.  Those belong in the
fleet-wide rollout (issue #3742).

The single exception is **auto-generated placeholder names** for unnamed
nets (``Net-(C11-2)`` / ``Net-(C11-Pad2)``): when both sides carry a
placeholder, the induced *pad partitions* are compared instead of the
strings, because the text is invented by whichever tool wrote the file and
carries no design intent (issue #4615).  See :func:`compare_netlists`.

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

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeGuard

from kicad_tools.schema.pcb import _find_all_footprints
from kicad_tools.sexp import SExp, parse_file

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, types only
    from kicad_tools.schematic.models.schematic import Schematic

# An auto-generated ("placeholder") net name: the synthetic name a netlister
# invents for a connected component that carries no label or power symbol.
# Deliberately permissive about what sits inside the parentheses, because the
# spelling is tool- and version-dependent and is NOT something this comparator
# should adjudicate:
#
#   ``Net-(C11-2)``            one convention
#   ``Net-(C11-Pad2)``         the other convention
#   ``Net-(/pwr/C11-Pad2)``    sheet-qualified variants
#
# ``kicad_tools.audit.net_audit`` labels these two spellings "new style" and
# "old style" respectively; issue #4615's filed body asserted the opposite
# mapping.  That question is left unsettled *on purpose* — the comparison
# below never has to pick a winner, because it stops comparing the strings at
# all once both sides are recognized as placeholders.
_PLACEHOLDER_NET_RE = re.compile(r"^Net-\(.*\)$")

# --- Label-leg vacuity guard (issue #4681, mirroring copper's #4005 guard) ---
#
# Sentinel ``ref`` carried by the single synthetic mismatch emitted when the
# PCB side contributes **zero** net bindings (see :func:`compare_netlists`).
# Angle brackets keep it out of the legal KiCad reference-designator space,
# the same convention as :data:`kicad_tools.lvs.copper_lvs.VACUOUS_NET`.
NETLIST_VACUOUS_REF = "<vacuous>"

# Sentinel "net name" carried on the synthetic vacuity record's ``pcb_net``
# side: it states explicitly that the PCB supplied no evidence, instead of
# the ``null`` a real unconnected pad would carry.
NETLIST_VACUOUS_NET = "<no-pcb-evidence>"


def _is_placeholder_net(name: str | None) -> TypeGuard[str]:
    """True when ``name`` is an auto-generated (unnamed-net) placeholder.

    A placeholder carries no design intent: it names a node the designer
    never labelled, and its exact text is chosen by whichever tool wrote the
    file.  Explicitly named nets (``VCC``, ``DAC_CLK``, ...) are never
    placeholders and keep strict string equality.

    Typed as a :class:`~typing.TypeGuard` so a positive answer also narrows
    ``str | None`` to ``str`` for callers.
    """
    return name is not None and bool(_PLACEHOLDER_NET_RE.match(name))


def _is_sheet_path_variant(sch_net: str | None, pcb_net: str | None) -> bool:
    """True when one name is the other with a *shorter* sheet-path prefix.

    Three spellings of one hierarchical node are seen in practice, and
    which one a board carries depends only on who wrote its nets:

    * ``/MCU/DBG_LED`` — KiCad's own netlist (and now kicad-tools'
      schematic side);
    * ``MCU/DBG_LED`` — the same, without the leading slash;
    * ``DBG_LED`` — kicad-tools' pure-Python netlist fallback, which does
      not qualify local labels at all.

    So the pair is nominated when, after dropping a leading slash from
    each, the two are equal or one is a whole trailing path segment of the
    other.  **Two different sheet paths are not variants**:
    ``/MCU/DBG_LED`` vs ``/AUX/DBG_LED`` share a leaf but name nets in
    different sheets, and forgiving that would let a board swap two
    sibling sheets' same-named nets unnoticed.

    This predicate only *nominates* a pair for tolerance; the caller must
    still confirm the two names cover the same pads before accepting them
    (see :func:`compare_netlists`).  Placeholder names are excluded — they
    have their own, stricter partition rule (issue #4615).
    """
    if sch_net is None or pcb_net is None or sch_net == pcb_net:
        return False
    if _is_placeholder_net(sch_net) or _is_placeholder_net(pcb_net):
        return False
    a = sch_net[1:] if sch_net.startswith("/") else sch_net
    b = pcb_net[1:] if pcb_net.startswith("/") else pcb_net
    if not a or not b:
        return False
    return a == b or a.endswith(f"/{b}") or b.endswith(f"/{a}")


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

    @property
    def vacuous(self) -> bool:
        """True when this result is the PCB-evidence vacuity verdict (#4681).

        A vacuous result means the PCB side supplied zero net bindings
        (see :func:`compare_netlists`), so the comparator observed
        nothing — ``clean`` is forced ``False`` and the sole mismatch
        carries ``ref=NETLIST_VACUOUS_REF``.  Derived from the mismatch
        list (not stored) so JSON round-trips and hand-built results
        preserve it, mirroring ``CopperLVSResult.vacuous``.
        """
        return any(m.ref == NETLIST_VACUOUS_REF for m in self.mismatches)


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


@dataclass(frozen=True)
class _SheetVisit:
    """One sheet reached by :func:`_walk_hierarchy_schematics`.

    Attributes:
        sheet_path: KiCad-style hierarchical sheet path of *this* sheet,
            always slash-delimited and slash-terminated: ``"/"`` for the
            root sheet, ``"/MCU/"`` for a child sheet whose ``Sheetname``
            is ``MCU``, ``"/MCU/ADC/"`` for a grandchild.  Concatenating
            it with a local label's text yields the identity KiCad's own
            netlister writes (``/MCU/DBG_LED``).
        schematic: The loaded :class:`~kicad_tools.schematic.models.Schematic`.
        sheet_pin_names: Names of the ``(pin ...)`` children of every
            ``(sheet ...)`` symbol declared *on this sheet*.  A sheet pin
            name is, by KiCad's rules, identical to the hierarchical
            label it mates with in the child sheet, so this set names
            every net that crosses a sheet boundary downward from here.
    """

    sheet_path: str
    schematic: Schematic
    sheet_pin_names: frozenset[str]


def _walk_hierarchy_schematics(sch_path: Path) -> Iterator[_SheetVisit]:
    """Yield every sheet in a hierarchy as a :class:`_SheetVisit`, root first.

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

    Each visit also carries the **sheet path** used to reach the sheet
    (issue #5815).  A local label is a per-sheet name: two sibling sheets
    may both use ``DBG_LED`` for two electrically unrelated nets, and
    KiCad disambiguates them by prefixing the sheet path
    (``/MCU/DBG_LED`` vs ``/AUX/DBG_LED``).  Without the path the walker
    cannot reconstruct that identity, which is what made
    :func:`_schematic_pin_to_net` report a bare ``DBG_LED`` against the
    board's sheet-qualified name.

    The path component is the sheet symbol's ``Sheetname`` property (what
    KiCad puts in the path), falling back to the ``Sheetfile`` stem when
    a hand-written sheet symbol omits it.

    **Repeated instances of one sub-sheet file are still visited once.**
    The ``visited`` guard is keyed on the resolved file path, so a
    sub-sheet placed twice yields only its first sheet path.  Resolving
    the second instance would need per-instance reference designators
    from the ``(instances ...)`` block, which this model does not read;
    binding the same ``(ref, pad)`` keys twice under two different paths
    would silently pick one at random.  The second instance's pads stay
    unbound and LVS reports them as schematic-side misses — loud, and
    the same behaviour as before this change (issue #4099 Phase 2).
    """
    from kicad_tools.operations.netlist import _get_sheet_entries
    from kicad_tools.schematic.models.schematic import Schematic

    visited: set[Path] = set()

    def _walk(path: Path, sheet_path: str) -> Iterator[_SheetVisit]:
        resolved = path.resolve()
        if resolved in visited or not path.exists():
            return
        visited.add(resolved)
        entries = _get_sheet_entries(path)
        yield _SheetVisit(
            sheet_path=sheet_path,
            schematic=Schematic.load(str(path)),
            sheet_pin_names=frozenset(name for e in entries for name in e.pin_names),
        )
        parent_dir = path.parent
        for entry in entries:
            child_name = entry.sheetname or Path(entry.filename).stem
            yield from _walk(parent_dir / entry.filename, f"{sheet_path}{child_name}/")

    yield from _walk(Path(sch_path), "/")


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

    The returned value is a net **identity**, not a per-pin display label:
    every pin on one connected component maps to the same string, including
    unnamed components (whose auto-generated ``Net-(...)`` name is derived
    from the component's canonical representative pin — see
    :func:`kicad_tools.schematic.models.netlist_mixin.auto_net_name`).  That
    property is load-bearing for :func:`copper_lvs.compare_partitions`,
    which treats two distinct strings inside one copper component as a
    short; before issue #4615 an unnamed node answered a different name to
    every pad and manufactured C(k, 2) false shorts.

    ``None`` indicates a floating pin (declared on the symbol but not
    connected to anything on its sheet), matching the convention used for
    unconnected PCB pads.

    **Local labels are sheet-qualified** (issues #5815 / #5809).  A local
    label names a net only within its own sheet, so KiCad's netlister
    writes it prefixed by that sheet's path: a root-sheet ``SENSE``
    becomes ``/SENSE``, an ``MCU`` child sheet's ``DBG_LED`` becomes
    ``/MCU/DBG_LED``.  Emitting the bare text made every such pin
    disagree with the board — the false ``label: N mismatch(es)`` of
    issue #5815 — and, worse, merged two sibling sheets' same-named but
    electrically unrelated local nets into one identity, which
    :func:`copper_lvs.compare_partitions` then had to call a short.

    Qualification is deliberately narrow: **only** names that are local
    labels *and* are not used anywhere in the hierarchy as a global
    label, a power symbol's net, a hierarchical label, or a sheet-pin
    name.  Those four are exactly the names that are shared across
    sheets — KiCad leaves global/power names unqualified, and the
    sheet-pin/hierarchical-label pair is how a net legitimately spans
    two sheets.  Prefixing either kind would split a net that the board
    (correctly) keeps whole.  A local label whose text matches the sheet
    pin it feeds therefore stays bare, preserving the parent↔child
    unification this function already had.

    Full cross-instance sheet-pin unification (the same sub-sheet placed
    twice under different parent net contexts, and adopting the parent's
    higher-priority name for a net that crosses a sheet boundary) remains
    out of scope here (a Phase 2 concern, issue #4099).
    """
    visits = list(_walk_hierarchy_schematics(Path(sch_path)))

    # Names that are shared across sheets and must NOT be sheet-qualified:
    # globals and power rails are hierarchy-wide by definition, while a
    # hierarchical label / sheet pin pair is the mechanism by which one net
    # spans a parent and a child sheet.  Collected over the WHOLE hierarchy
    # (not per sheet) because the two ends of such a net live on different
    # sheets: qualifying the parent's local ``BUS`` label while the child's
    # ``BUS`` hierarchical label stayed bare would split the net in half.
    shared_names: set[str] = set()
    for visit in visits:
        sch = visit.schematic
        shared_names.update(gl.text for gl in sch.global_labels if gl.text)
        shared_names.update(hl.text for hl in sch.hier_labels if hl.text)
        for pwr in sch.power_symbols:
            if pwr.net_name:
                shared_names.add(pwr.net_name)
        shared_names.update(visit.sheet_pin_names)

    out: dict[tuple[str, str], str | None] = {}
    for visit in visits:
        sch = visit.schematic
        # Texts this sheet names with a *local* label, minus anything the
        # hierarchy also uses as a cross-sheet name.  An auto-generated
        # ``Net-(...)`` identity is never a label text, so it is never
        # qualified (and never needs to be: its representative reference
        # designator is already unique board-wide).
        qualifiable = {lbl.text for lbl in sch.labels if lbl.text} - shared_names

        # ``get_all_pin_nets`` builds the sheet's connectivity graph once
        # and resolves every ``(ref, number)`` pin against it, instead of
        # ``get_net_for_pin`` rebuilding that graph (an O(wires^2)
        # all-pairs scan) once per pin (issue #5240). Board 05's ~316-pin
        # schematic rebuilt the graph 316 times before this change;
        # profiling attributed roughly 30s of a 42s single-threaded
        # ``kct check`` run to those redundant rebuilds. Behavior is
        # identical to the historical per-pin loop -- see
        # ``get_all_pin_nets``'s docstring for why.
        for key, net in sch.get_all_pin_nets().items():
            if net is not None and net in qualifiable:
                net = f"{visit.sheet_path}{net}"
            out[key] = net
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
    for fp in _find_all_footprints(doc):
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
            elif len(net.children) == 1 and isinstance(net.get_value(0), str):
                # KiCad 10: (net "NAME"). Check the atom type rather than
                # coercing it: (net 5) is not a name, but (net "5") is.
                net_name = net.get_string(0)
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

    **Auto-generated placeholder names are compared by partition, not by
    string** (issue #4615).  An unnamed net's name (``Net-(C11-2)``,
    ``Net-(C11-Pad2)``) is invented by the netlister — the spelling
    convention and the representative pin both vary by tool and version —
    so when *both* sides carry a placeholder, the pads the schematic puts
    on that node must equal the pads the PCB puts on its node, and the
    strings themselves are not compared.  Nothing else is relaxed: a
    placeholder facing a real named net (or a floating/unconnected side)
    still mismatches, a pad bound to a *different* unnamed node still
    mismatches because the pad sets differ, and explicitly named nets keep
    exact string equality.

    **A shorter sheet-path spelling is tolerated as a 1:1 renaming**
    (issue #5815).  The schematic side qualifies a local label with its
    sheet path (``/MCU/DBG_LED``), matching KiCad's netlister; a board
    may spell the same node ``/MCU/DBG_LED``, ``MCU/DBG_LED`` or bare
    ``DBG_LED`` depending on which tool wrote its nets.  Such a pair is
    forgiven only when the correspondence is one-to-one across the whole
    board, so a board that merges two sibling sheets' same-named nets, a
    swap between them, and a pad bound to the wrong net all still fail.

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

        **Vacuity guard (issue #4681):** when the PCB supplies zero net
        bindings while the board has pads and the schematic binds >=1
        pin, the result is a single synthetic mismatch with
        ``ref=NETLIST_VACUOUS_REF`` / ``pcb_net=NETLIST_VACUOUS_NET``
        (and ``LVSResult.vacuous`` is ``True``) instead of one
        pseudo-mismatch per bound schematic pin.  The result is still
        dirty: no evidence is not a pass (same rationale as the copper
        leg's #4005 guard).
    """
    sch_path = Path(sch_path)
    pcb_path = Path(pcb_path)

    sch_map = _schematic_pin_to_net(sch_path)
    pcb_map = _pcb_pin_to_net(pcb_path)

    # --- Vacuity guard (issue #4681, mirroring the copper leg's #4005
    #     guard): when the PCB side supplies **zero** net bindings — every
    #     pad lacks a ``(net ...)`` child or carries only the empty net-0
    #     name — while the board has pads and the schematic binds at least
    #     one pin, the comparator has no PCB evidence at all.  Diffing
    #     anyway would emit one pseudo-mismatch per bound schematic pin
    #     (264 all-``pcb_net=null`` records on the #4681 board), which
    #     reads as N real defects instead of one degraded input.  Refuse:
    #     return a single synthetic record so the result is still dirty
    #     (no evidence is not a pass) and consumers see an explicit
    #     vacuous verdict.
    #
    #     Raw (pre-normalization) bindings count as evidence on purpose:
    #     an explicit no-connect sentinel (``unconnected-(REF-PIN-PadN)``)
    #     is a deliberate per-pad binding even though it normalizes to
    #     ``None`` below, so a small board of NC pads still gets the real
    #     per-pad diff (test_board_lvs_nc_sentinel.py pins that).  Any
    #     board with >=1 net-bearing pad is likewise unchanged — genuine
    #     partial mismatches keep their per-pad records.
    sch_bound = sum(1 for v in sch_map.values() if v is not None)
    pcb_bound = sum(1 for v in pcb_map.values() if v)  # non-None AND non-empty
    if pcb_map and sch_bound and not pcb_bound:
        vacuity = LVSMismatch(
            ref=NETLIST_VACUOUS_REF,
            pad=f"board_pads={len(pcb_map)}",
            schematic_net=f"sch_bound_pins={sch_bound}",
            pcb_net=NETLIST_VACUOUS_NET,
        )
        return LVSResult(clean=False, mismatches=(vacuity,))

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

    # Stable iteration order: union of keys, sorted by (ref, pad) so the
    # output is deterministic for golden-file tests.
    all_keys = sorted(set(sch_map) | set(pcb_map))
    norm_pcb_map = {key: _norm_pcb(pcb_map.get(key), key[0], key[1]) for key in all_keys}

    # --- Placeholder-vs-placeholder: compare partitions, not strings ---
    #
    # An unnamed net's name is invented, not designed: kct derives it from
    # the lexicographically smallest pin on the node, KiCad derives it from
    # its own representative, and the two spellings differ by convention
    # (``Net-(C11-2)`` vs ``Net-(C11-Pad2)``) as well as by representative.
    # Comparing those strings makes every pad on every unnamed net mismatch
    # even when both sides describe *exactly the same* connectivity — the
    # failure that would simply have taken over the ``LVS: FAILED`` line
    # once the copper-side splintering of issue #4615 was fixed.
    #
    # So when BOTH sides carry a placeholder, compare the induced pad
    # partitions instead: the set of pads the schematic puts on that node
    # must equal the set of pads the PCB puts on its node.  This is strictly
    # a *renaming* tolerance, not a relaxation:
    #
    #   * placeholder vs real named net (either direction) -> still compared
    #     as strings, still mismatches;
    #   * placeholder vs ``None`` (floating / unconnected) -> still mismatches;
    #   * a pad bound to a DIFFERENT unnamed node than the schematic says ->
    #     the two pad sets differ, so it still mismatches (as do the other
    #     pads on both affected nodes);
    #   * explicitly named nets are untouched — exact string equality.
    #
    # Groups are built only over keys where BOTH sides carry a placeholder.
    # A pad that is one-sided, floating, or bound to a real named net on
    # either side is already judged on its own key by the rules above, and
    # excluding it keeps its verdict from smearing across every other pad
    # that shares its net.  Detection is unaffected: any pad whose two sides
    # disagree about *which* unnamed node it belongs to is a both-placeholder
    # key by construction, so it is still inside the partitions being diffed.
    sch_groups: dict[str, set[tuple[str, str]]] = {}
    pcb_groups: dict[str, set[tuple[str, str]]] = {}
    for key in sorted(set(sch_map) & set(pcb_map)):
        sch_name = sch_map.get(key)
        pcb_name = norm_pcb_map.get(key)
        if _is_placeholder_net(sch_name) and _is_placeholder_net(pcb_name):
            sch_groups.setdefault(sch_name, set()).add(key)
            pcb_groups.setdefault(pcb_name, set()).add(key)

    # --- Sheet-path variants: one net, one shorter spelling (issue #5815) ---
    #
    # The *path* half of a hierarchical net name is a convention, not a
    # design decision.  For one and the same node a board may carry
    # ``/MCU/DBG_LED`` (kicad-cli / KiCad's own netlist), ``MCU/DBG_LED``
    # (no leading slash), or a bare ``DBG_LED`` (kicad-tools' pure-Python
    # netlist fallback, which does not qualify names).  The schematic side
    # now always emits KiCad's qualified spelling, so the convention gap
    # would turn every hierarchical local net into a mismatch.
    #
    # ``_is_sheet_path_variant`` nominates such a pair; it is accepted only
    # when the nomination is a consistent **1:1 renaming** across the whole
    # board — every pad the schematic puts on that name faces the same board
    # name, and vice versa.  That is what keeps it a renaming tolerance
    # rather than a relaxation:
    #
    #   * an unqualified board name (``DBG_LED``) facing *two* distinct
    #     sibling nets is not injective -- the board really does merge two
    #     nets, and every pad on both is reported;
    #   * one schematic net spread over two board spellings is likewise
    #     rejected in the other direction;
    #   * two sibling sheets are not variants of each other at all
    #     (``/MCU/...`` is not a suffix of ``/AUX/...``), so swapping the two
    #     nets' names on the board still fails;
    #   * names differing anywhere but the path prefix -> exact equality;
    #   * a floating/unconnected side -> unchanged, still mismatches.
    #
    # Only variant pairs feed the mapping.  A pad the board genuinely binds
    # to the wrong net is judged on its own key by the rules above, and
    # leaving it out keeps its verdict from smearing across every other pad
    # that shares its net -- exactly the scoping the exact-string comparison
    # has always had.
    variant_fwd: dict[str, set[str]] = {}
    variant_rev: dict[str, set[str]] = {}
    for key in set(sch_map) & set(pcb_map):
        sch_name = sch_map.get(key)
        pcb_name = norm_pcb_map.get(key)
        if _is_sheet_path_variant(sch_name, pcb_name):
            assert sch_name is not None and pcb_name is not None  # narrowed above
            variant_fwd.setdefault(sch_name, set()).add(pcb_name)
            variant_rev.setdefault(pcb_name, set()).add(sch_name)

    def _is_consistent_rename(sch_name: str, pcb_name: str) -> bool:
        """True when ``sch_name`` <-> ``pcb_name`` is 1:1 over the board."""
        return variant_fwd.get(sch_name) == {pcb_name} and variant_rev.get(pcb_name) == {sch_name}

    mismatches: list[LVSMismatch] = []
    for key in all_keys:
        ref, pad = key
        sch_net = sch_map.get(key)
        pcb_net = norm_pcb_map[key]
        if sch_net == pcb_net:
            continue
        if (
            _is_placeholder_net(sch_net)
            and _is_placeholder_net(pcb_net)
            and sch_groups.get(sch_net) == pcb_groups.get(pcb_net)
        ):
            # Same physical node, different invented spelling — not a defect.
            continue
        if _is_sheet_path_variant(sch_net, pcb_net) and _is_consistent_rename(
            sch_net,  # type: ignore[arg-type]  # non-None: the predicate said so
            pcb_net,  # type: ignore[arg-type]
        ):
            # Same physical node, different sheet-path convention (#5815).
            continue
        mismatches.append(
            LVSMismatch(
                ref=ref,
                pad=pad,
                schematic_net=sch_net,
                pcb_net=pcb_net,
            )
        )

    return LVSResult(clean=not mismatches, mismatches=tuple(mismatches))
