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
    from kicad_tools.schematic.models.symbol import SymbolInstance

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

# Upper bound on sheet *placements* walked by
# :func:`_walk_hierarchy_schematics`.  Since issue #5815 the walk visits one
# sheet per ``(sheet ...)`` symbol rather than one per file, so a hierarchy
# that nests repeated placements can expand exponentially.  Real designs sit
# in the tens; this cap exists only so a pathological (or maliciously
# constructed) file fails loudly instead of hanging.
_MAX_SHEET_VISITS = 10_000


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
    """True when ``pcb_net`` is a less-qualified spelling of ``sch_net``.

    KiCad scopes a plain ``(label "SENSE")`` to its sheet and names the
    resulting net ``/SENSE`` (root sheet) or ``/MCU/DBG_LED`` (one level
    down); :func:`_schematic_pin_to_net` reports the same identity.  A board
    may spell that one node differently depending on who wrote its nets:

    * ``/MCU/DBG_LED`` — KiCad's own netlist (identical: not a variant);
    * ``MCU/DBG_LED`` — the same path without the leading slash (#5815);
    * ``DBG_LED`` — kicad-tools' pure-Python netlist fallback, which does
      not qualify local labels at all (the committed
      ``boards/00-simple-led`` artifacts use this form; #5809).

    So the pair is nominated only when ``sch_net`` is sheet-qualified
    (starts with ``/``), ``pcb_net`` is **not** (a board name starting with
    ``/`` is itself a fully qualified identity and must match exactly), and
    ``pcb_net`` is either ``sch_net`` minus its leading slash or its leaf.

    That is deliberately narrower than suffix matching (issue #5826): a
    root-qualified ``/SENSE`` is never a variant of ``/ChildA/SENSE`` —
    they are two nets at different depths that merely share a leaf — and
    two different sheet paths (``/MCU/DBG_LED`` vs ``/AUX/DBG_LED``) are
    never variants either.  The direction is one-way: a *bare* schematic
    name facing a qualified board name is left alone.

    This predicate only *nominates* a pair; :func:`compare_netlists` still
    has to prove the spelling cannot mean a second net before accepting
    it.  Placeholder names are excluded — they have their own, stricter
    partition rule (issue #4615).
    """
    if sch_net is None or pcb_net is None or sch_net == pcb_net:
        return False
    if _is_placeholder_net(sch_net) or _is_placeholder_net(pcb_net):
        return False
    if not sch_net.startswith("/") or pcb_net.startswith("/") or not pcb_net:
        return False
    return pcb_net == sch_net[1:] or pcb_net == sch_net.rsplit("/", 1)[-1]


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


@dataclass(frozen=True, eq=False)
class _SheetVisit:
    """One sheet reached by :func:`_walk_hierarchy_schematics`.

    Attributes:
        sheet_path: KiCad **sheet-name path prefix** of this sheet, with no
            trailing slash: ``""`` for the root sheet, ``"/SubMcu"`` for a
            child placed under ``(property "Sheetname" "SubMcu")``,
            ``"/SubMcu/Inner"`` for a grandchild.  A local label ``NAME``
            on this sheet is the net ``f"{sheet_path}/{NAME}"`` — exactly
            the identity KiCad writes into its netlist and onto the PCB
            (``/SENSE`` at the root, ``/SubMcu/DBG_LED`` one level down;
            verified against ``kicad-cli`` 10.0.6, issues #5809 / #5815).
        schematic: The loaded :class:`~kicad_tools.schematic.models.schematic.Schematic`.
        parent: The visit of the sheet whose ``(sheet ...)`` symbol
            instantiated this one; ``None`` for the root sheet.
        parent_pin_names: Names of the ``(pin ...)`` children of that
            ``(sheet ...)`` symbol — the sheet pins this sheet's
            hierarchical labels mate with.  Empty for the root sheet.
        uuid_path: KiCad **instance path** of this sheet — the root
            schematic's ``(uuid ...)`` followed by one sheet-symbol
            ``(uuid ...)`` per level, e.g.
            ``"/<root-uuid>/<sheet-a-uuid>"``.  Unlike :attr:`sheet_path`
            (which is built from human-readable ``Sheetname``s and can
            legitimately repeat) this is unique per *placement*, which is
            what a symbol's ``(instances ...)`` block is keyed by.  It is
            how the two placements of one shared ``Sheetfile`` get their
            own reference designators (issue #5815).
        source: Path of the ``.kicad_sch`` file this sheet was loaded from.
            Two placements of one sub-sheet share a ``source`` but differ
            in :attr:`sheet_path` and :attr:`uuid_path`.
    """

    sheet_path: str
    schematic: Schematic
    parent: _SheetVisit | None = None
    parent_pin_names: frozenset[str] = frozenset()
    uuid_path: str = ""
    source: Path = Path()


def _walk_hierarchy_schematics(sch_path: Path) -> Iterator[_SheetVisit]:
    """Yield every sheet of a hierarchy as a :class:`_SheetVisit`, root first.

    Follows ``(sheet ...)`` references the same way
    :func:`kicad_tools.operations.netlist._collect_hierarchy_components`
    does — resolving each ``Sheetfile`` relative to its parent's
    directory and guarding against circular references — so the set of
    ``(ref, pad)`` keys spans the full design, not just the root sheet.
    The walk is depth-first **pre-order**: a sheet is always yielded after
    its parent, which :func:`_schematic_pin_to_net` relies on to resolve a
    child's hierarchical labels against names its parent already resolved.

    Yielding the loaded ``Schematic`` objects (rather than only their
    net dict) lets the caller enumerate *every* pin declared on *every*
    symbol across the hierarchy, including pins that never connect to a
    net.  That preserves the "floating pins resolve to ``None``, not
    dropped" contract the single-sheet loader had.

    The sheet path is built from ``Sheetname``, not ``Sheetfile``: the
    filename is not part of a KiCad net name.  A sheet symbol that omits
    ``Sheetname`` falls back to its file stem, so an empty segment never
    collapses a child's labels onto its parent's qualification.

    **One sheet file placed twice is walked twice** (issue #5815).  KiCad
    treats every ``(sheet ...)`` symbol as its own sheet: placing
    ``mcu.kicad_sch`` as both ``MCU_A`` and ``MCU_B`` gives two independent
    sheets whose local labels are ``/MCU_A/DBG_LED`` and ``/MCU_B/DBG_LED``
    — different nets (verified against ``kicad-cli`` 10.0.6).  An earlier
    hierarchy-wide ``visited`` set walked the shared file only once, which
    dropped every pin of the second placement out of the schematic-side map
    entirely.  Cycle safety does not need that: a hierarchy is only
    *circular* when a sheet reaches one of its own **ancestors**, so the
    guard is the ancestor chain of the branch being walked, which leaves
    sibling re-placement (the legal, common case) free.

    ``_MAX_SHEET_VISITS`` bounds the walk.  Per-placement expansion is
    exponential in the worst case (N nested levels that each place the same
    sub-sheet twice yield 2**N sheets) — inherent to the format, and true of
    KiCad itself — so a pathological hierarchy raises :class:`ValueError`
    rather than hanging.  Real designs are orders of magnitude below the cap.
    """
    from kicad_tools.operations.netlist import _get_sheet_entries
    from kicad_tools.schematic.models.schematic import Schematic

    visits = 0

    def _walk(
        path: Path,
        sheet_path: str,
        parent: _SheetVisit | None,
        parent_pin_names: frozenset[str],
        uuid_path: str,
        ancestors: frozenset[Path],
    ) -> Iterator[_SheetVisit]:
        nonlocal visits
        resolved = path.resolve()
        # Ancestors only: a sheet that re-enters its own chain is circular,
        # but a sheet placed twice as a *sibling* is two legitimate sheets.
        if resolved in ancestors or not path.exists():
            return
        visits += 1
        if visits > _MAX_SHEET_VISITS:
            raise ValueError(
                f"sheet hierarchy under {sch_path} expands to more than "
                f"{_MAX_SHEET_VISITS} sheet placements; refusing to walk further"
            )
        schematic = Schematic.load(str(path))
        if parent is None:
            uuid_path = f"/{getattr(schematic, 'sheet_uuid', '') or ''}"
        visit = _SheetVisit(
            sheet_path=sheet_path,
            schematic=schematic,
            parent=parent,
            parent_pin_names=parent_pin_names,
            uuid_path=uuid_path,
            source=path,
        )
        yield visit
        parent_dir = path.parent
        child_ancestors = ancestors | {resolved}
        for entry in _get_sheet_entries(path):
            segment = entry.sheetname or Path(entry.filename).stem
            yield from _walk(
                parent_dir / entry.filename,
                f"{sheet_path}/{segment}",
                visit,
                frozenset(entry.pin_names),
                f"{uuid_path}/{entry.uuid}",
                child_ancestors,
            )

    yield from _walk(Path(sch_path), "", None, frozenset(), "", frozenset())


def _instance_reference_map(visit: _SheetVisit) -> dict[str, str]:
    """Map ``(property "Reference")`` -> this placement's reference designator.

    A ``.kicad_sch`` file stores one ``(property "Reference" ...)`` per
    symbol — a single value, however many times the sheet is placed — plus
    an ``(instances ...)`` block that records the *real* designator for each
    placement, keyed by that placement's instance path::

        (instances
          (project "chorus"
            (path "/<root-uuid>/<mcu-a-uuid>" (reference "R1")  (unit 1))
            (path "/<root-uuid>/<mcu-b-uuid>" (reference "R11") (unit 1))))

    :meth:`Schematic.get_all_pin_nets` keys its result by the *property*
    reference, so both placements of one file answer ``R1``/``R2`` and the
    second placement's pads would be attributed to the first's nets.  This
    returns the rename to apply for :attr:`_SheetVisit.uuid_path`, e.g.
    ``{"R1": "R11", "R2": "R12"}`` for the ``MCU_B`` placement above
    (issue #5815).

    Empty — meaning "use the property references as-is" — whenever the file
    has no ``(instances ...)`` data for this path.

    Callers are expected to invoke this only for a sheet file that really is
    placed more than once (see :func:`_schematic_pin_to_net`).  A singly
    placed sheet's property reference *is* its instance reference — KiCad
    keeps the two in step — so skipping it there costs nothing and spares
    the common design a second full parse of every sheet.
    """
    if not visit.uuid_path:
        return {}
    try:
        doc = parse_file(visit.source)
    except (OSError, ValueError):  # pragma: no cover - unreadable/odd sheet
        return {}

    renames: dict[str, str] = {}
    # Top-level ``(symbol ...)`` children only: ``find_all`` is recursive and
    # would also return the ``(lib_symbols ...)`` definitions and their nested
    # unit sub-symbols, none of which describe a placement.
    for sym in doc.children:
        if getattr(sym, "name", None) != "symbol" and getattr(sym, "tag", None) != "symbol":
            continue
        instances = sym.find("instances")
        if instances is None:
            continue
        prop_ref: str | None = None
        for prop in sym.find_all("property"):
            if prop.get_string(0) == "Reference":
                prop_ref = prop.get_string(1)
                break
        if not prop_ref:
            continue
        for project in instances.find_all("project"):
            for path_node in project.find_all("path"):
                if path_node.get_string(0) != visit.uuid_path:
                    continue
                ref_node = path_node.find("reference")
                inst_ref = ref_node.get_string(0) if ref_node is not None else None
                if inst_ref and inst_ref != prop_ref:
                    renames[prop_ref] = inst_ref
    return renames


def _iter_instance_symbols(sch_path: Path) -> Iterator[tuple[str, SymbolInstance, _SheetVisit]]:
    """Yield ``(reference, symbol, visit)`` for every placed symbol in a hierarchy.

    ``reference`` is the designator of *this placement* of the symbol: the
    ``(property "Reference")`` value, renamed through
    :func:`_instance_reference_map` when the symbol's sheet file is placed
    more than once (issue #5815).  A sheet placed twice therefore yields
    each of its symbols twice -- once as ``R1`` (``MCU_A``) and once as
    ``R11`` (``MCU_B``) -- so per-pin consumers (pin-type annotation,
    pin-to-pad mapping; issue #6004) see both footprints.

    Singly placed sheets -- every sheet in a non-reusing design -- are
    neither re-parsed nor renamed, so the common case is unchanged.
    """
    visits = list(_walk_hierarchy_schematics(Path(sch_path)))
    placements: dict[Path, int] = {}
    for visit in visits:
        key_path = visit.source.resolve()
        placements[key_path] = placements.get(key_path, 0) + 1
    for visit in visits:
        renames = _instance_reference_map(visit) if placements[visit.source.resolve()] > 1 else {}
        for sym in visit.schematic.symbols:
            ref = getattr(sym, "reference", "") or ""
            yield renames.get(ref, ref), sym, visit


def _sheet_net_identities(
    sch: Schematic, sheet_path: str, inherited: dict[str, str]
) -> dict[str, str]:
    """Map each label-driven net name on one sheet to its hierarchy-wide identity.

    The result is keyed by the name :meth:`Schematic.get_all_pin_nets`
    reports for a net on this sheet (a label's text) and valued by the
    identity KiCad gives that net.  The decision is made from the
    **provenance of the name on this sheet** — which kind of label drives
    it *here* — never from the same spelling appearing somewhere else in
    the hierarchy.  A global ``SENSE`` on the root sheet therefore does not
    stop a child's unrelated *local* ``SENSE`` from being ``/ChildA/SENSE``
    (label scope is set by label type, not by text; issue #5826).

    Precedence, highest first, for a name on this sheet:

    1. **Global label or power symbol** — hierarchy-wide by definition;
       the bare name (``GLOB``, ``GND``, ``+3V3``, ``PWR_FLAG``).
    2. **Hierarchical label** mating with a named net on the parent — the
       net crosses the sheet boundary, so it takes the identity the parent
       already resolved for it (``inherited``).  A root-sheet local label
       ``BUS`` wired to the ``BUS`` sheet pin makes the child's
       hierarchical ``BUS`` the net ``/BUS`` too, so the two halves of one
       physical net share one identity (the case PR #5824 fixed and
       ``main``'s per-sheet rule missed).  ``kicad-cli`` names such a net
       after the parent's local label, which is the identity inherited.
    3. **Hierarchical label with no named parent net** — left bare, as
       before.  Its KiCad name comes from whatever the parent wires to the
       sheet pin, which this label-text model cannot see (issue #4099
       Phase 2).
    4. **Local label only** — ``f"{sheet_path}/{name}"``.

    Names absent from the result (auto-generated ``Net-(...)``
    placeholders) are used as-is.
    """
    identities: dict[str, str] = {}
    for label in sch.labels:
        if label.text:
            identities[label.text] = f"{sheet_path}/{label.text}"
    for hl in sch.hier_labels:
        if hl.text:
            identities[hl.text] = inherited.get(hl.text, hl.text)
    for gl in sch.global_labels:
        if gl.text:
            identities[gl.text] = gl.text
    for pwr in sch.power_symbols:
        if pwr.net_name:
            identities[pwr.net_name] = pwr.net_name
    return identities


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
    board that uses PWR_FLAG.

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

    **Local labels are sheet-qualified** (issues #5809 / #5815 / #5826).
    A plain ``(label "SENSE")`` is scoped to its sheet, and KiCad names the
    resulting net with that sheet's path — ``/SENSE`` on the root sheet,
    ``/SubMcu/DBG_LED`` inside a sheet named ``SubMcu``.  Two sibling
    sheets that reuse one label text are therefore two nets, not one.
    Global labels and power-symbol nets keep their bare spelling, and a
    hierarchical label takes the identity its parent resolved for the
    mating sheet pin, so a net that crosses a sheet boundary keeps one
    identity on both sides.  See :func:`_sheet_net_identities` for the
    precise, per-sheet provenance rules.

    **One sheet file placed twice is two sheets** (issue #5815), each with
    its own qualification (``/MCU_A/DBG_LED`` vs ``/MCU_B/DBG_LED``) and its
    own reference designators, taken from each symbol's ``(instances ...)``
    block via :func:`_instance_reference_map`.  What remains out of scope is
    the *sheet-pin* half of that story (issue #4099 Phase 2): a hierarchical
    label whose mating sheet pin is wired to a differently-named net in each
    parent still resolves from label text alone, so the two placements share
    one bare identity there instead of inheriting one name per placement.
    """
    out: dict[tuple[str, str], str | None] = {}
    # Keyed by visit identity (``eq=False`` keeps ``_SheetVisit`` hashable
    # by object), so a repeated sheet path can never alias another sheet.
    resolved: dict[_SheetVisit, dict[str, str]] = {}
    # Materialized because the per-placement reference rename below is only
    # needed -- and only paid for -- when a sheet *file* carries more than one
    # placement.  Pre-order is preserved, so ``resolved[visit.parent]`` is
    # always populated before a child is processed.
    visits = list(_walk_hierarchy_schematics(Path(sch_path)))
    placements: dict[Path, int] = {}
    for visit in visits:
        key_path = visit.source.resolve()
        placements[key_path] = placements.get(key_path, 0) + 1
    for visit in visits:
        inherited: dict[str, str] = {}
        if visit.parent is not None:
            parent_ids = resolved[visit.parent]
            inherited = {
                name: parent_ids[name] for name in visit.parent_pin_names if name in parent_ids
            }
        identities = _sheet_net_identities(visit.schematic, visit.sheet_path, inherited)
        resolved[visit] = identities
        # ``get_all_pin_nets`` builds the sheet's connectivity graph once
        # and resolves every ``(ref, number)`` pin against it, instead of
        # ``get_net_for_pin`` rebuilding that graph (an O(wires^2)
        # all-pairs scan) once per pin (issue #5240). Board 05's ~316-pin
        # schematic rebuilt the graph 316 times before this change;
        # profiling attributed roughly 30s of a 42s single-threaded
        # ``kct check`` run to those redundant rebuilds. Behavior is
        # identical to the historical per-pin loop -- see
        # ``get_all_pin_nets``'s docstring for why.
        # One sheet file placed twice answers the same property-based
        # reference designators on both placements; rename each placement's
        # pins to the designator its ``(instances ...)`` block records, so
        # ``MCU_B``'s pads land on ``R11``/``R12`` rather than overwriting
        # ``MCU_A``'s ``R1``/``R2`` (issue #5815).  Skipped outright for a
        # singly-placed sheet -- every sheet in a non-reusing design -- so
        # the usual case pays no extra parse and is bit-for-bit unchanged.
        renames = _instance_reference_map(visit) if placements[visit.source.resolve()] > 1 else {}
        for (ref, pad), net in visit.schematic.get_all_pin_nets().items():
            key = (renames.get(ref, ref), pad)
            out[key] = identities.get(net, net) if net is not None else None
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

    **A local net's less-qualified spelling is accepted as an alias for
    its sheet-qualified identity where that is unambiguous** (issues #5809,
    #5815, #5826).  A local label is a sheet-scoped name: KiCad writes it
    ``/SENSE`` (root sheet) or ``/SubMcu/SENSE`` (child sheet), while a
    board may carry ``SubMcu/SENSE`` or, from kicad-tools' pure-Python
    netlist fallback, bare ``SENSE``.  Such a spelling matches only when it
    is a variant of exactly one schematic net, is not a net of its own, the
    board does not also use the qualified spelling, and the schematic net
    is spelled one consistent way across all of its pads on the board.
    Nothing broader is relaxed: a different leaf (``/SENSE`` vs
    ``RETURN``) is a swap and still mismatches; a board name starting with
    ``/`` must match exactly, so ``/SENSE`` vs ``/SubMcu/SENSE`` (different
    depths) and ``/MCU/X`` vs ``/AUX/X`` (different sheets) still mismatch;
    two sibling sheets that each carry a local ``SENSE`` grant no alias at
    all, so a board that collapses them onto one bare ``SENSE`` still fails
    on every affected pad.

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

    # --- Less-qualified spellings of one local net (#5809 / #5815 / #5826) ---
    #
    # A local label's net identity is ``/SENSE`` (root) or ``/SubMcu/SENSE``
    # (child) in KiCad and in :func:`_schematic_pin_to_net`, but a board may
    # carry ``SubMcu/SENSE`` (no leading slash) or bare ``SENSE`` (kicad-tools'
    # pure-Python netlist fallback).  :func:`_is_sheet_path_variant`
    # nominates such a pair; it is accepted only **where the two documents
    # prove the spelling cannot mean a second net**.  Four conditions, all
    # required:
    #
    #   1. the board spelling is a variant of exactly one schematic identity.
    #      Two sibling sheets that each label a net ``SENSE`` give
    #      ``/A/SENSE`` and ``/B/SENSE``; a board that binds both sheets'
    #      pads to one bare ``SENSE`` has shorted two nets, so no alias is
    #      granted and every affected pad is reported (#5815's criterion);
    #   2. the board spelling is not itself a schematic net identity —
    #      otherwise a global label / power net named ``SENSE`` would be
    #      conflated with the local ``/A/SENSE``;
    #   3. the PCB does not *also* use the qualified spelling anywhere.  A
    #      board carrying both ``/A/SENSE`` and ``SENSE`` is distinguishing
    #      two nets; honouring its distinction keeps the mismatch;
    #   4. the schematic net is spelled **one way** across all of its own
    #      pads on the board (issue #5826).  A net whose pads carry both
    #      ``MCU/X`` and ``X`` is split in two on the board — a real open —
    #      and no pad of it is excused.  Only spellings of *this* net count:
    #      a pad the board binds to an unrelated net is judged on its own key
    #      and does not smear its verdict onto the net's other pads.
    #
    # This is narrower than "ignore slashes" in every direction that
    # matters.  A different leaf name is never an alias, so a swap
    # (``/SENSE`` vs ``RETURN``) is still reported.  A board name that starts
    # with ``/`` is a fully qualified identity and must match exactly, so
    # ``/SENSE`` (root) vs ``/SubMcu/SENSE`` (child) — genuinely different
    # nets in KiCad — and ``/MCU/X`` vs ``/AUX/X`` still mismatch.  And the
    # tolerance is one-directional: a *schematic* net that is bare while the
    # PCB is qualified is left alone, because that is the sheet-pin case whose
    # correct name depends on what the parent wires to the pin (issue #4099
    # Phase 2); inventing an alias there could paper over a real cross-sheet
    # binding error.
    sch_names = {name for name in sch_map.values() if name is not None}
    pcb_names = {name for name in norm_pcb_map.values() if name is not None}
    qualified_sch_names = {name for name in sch_names if name.startswith("/")}
    owners: dict[str, set[str]] = {}
    for pcb_name in pcb_names:
        for sch_name in qualified_sch_names:
            if _is_sheet_path_variant(sch_name, pcb_name):
                owners.setdefault(pcb_name, set()).add(sch_name)
    # Every spelling (exact or variant) the board gives each schematic net.
    spellings: dict[str, set[str]] = {}
    for key in set(sch_map) & set(pcb_map):
        sch_name = sch_map[key]
        pcb_name = norm_pcb_map.get(key)
        if sch_name is None or pcb_name is None:
            continue
        if pcb_name == sch_name or _is_sheet_path_variant(sch_name, pcb_name):
            spellings.setdefault(sch_name, set()).add(pcb_name)

    def _is_alias_of(qualified: str | None, variant: str | None) -> bool:
        """True when PCB-side ``variant`` unambiguously means ``qualified``."""
        if qualified is None or variant is None:
            return False
        if not _is_sheet_path_variant(qualified, variant):
            return False
        return (
            owners.get(variant) == {qualified}  # (1) unique owner
            and variant not in sch_names  # (2) not a net of its own
            and qualified not in pcb_names  # (3) board keeps the short form
            and spellings.get(qualified) == {variant}  # (4) one spelling per net
        )

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
        if _is_alias_of(sch_net, pcb_net):
            # One local net: sheet-qualified on the schematic side, spelled
            # with a shorter path on the board, and unambiguously the same
            # net (#5809 / #5815 / #5826).
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
