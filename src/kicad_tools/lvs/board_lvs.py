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
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from kicad_tools.schema.pcb import _find_all_footprints
from kicad_tools.sexp import SExp, parse_file

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


def _bare_local_name(net_name: str | None) -> str | None:
    """Return the unqualified spelling of a sheet-qualified local net name.

    KiCad scopes a plain ``(label "SENSE")`` to its sheet and names the
    resulting net ``/SENSE`` (root sheet) or ``/SubMcu/SENSE`` (one level
    down).  kicad-tools' own PCB generator, when it falls back to
    pure-Python netlist extraction, writes that same node bare (``SENSE``) —
    so a board may legitimately carry either spelling (the committed
    ``boards/00-simple-led`` artifacts use the bare form; anything KiCad
    produced uses the qualified form).

    ``"/SENSE" -> "SENSE"``, ``"/SubMcu/SENSE" -> "SENSE"``, and ``None``
    for anything that is not sheet-qualified (``"GND"``, ``"/"``, ``None``).
    Recognizing the spelling is only the first half of the test — see
    :func:`compare_netlists`, which accepts it as an alias only where the
    design proves it cannot mean a second net.

    Issue #5809 (root-sheet form) / #5815 (child-sheet form).
    """
    if net_name is None or not net_name.startswith("/"):
        return None
    leaf = net_name.rsplit("/", 1)[-1]
    return leaf or None


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


def _walk_hierarchy_schematics(sch_path: Path):
    """Yield ``(sheet_path, Schematic)`` for a hierarchy, root first, depth-first.

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

    ``sheet_path`` is the KiCad **sheet-name path prefix** for the sheet
    being yielded, with no trailing slash: ``""`` for the root sheet,
    ``"/SubMcu"`` for a child placed under ``(property "Sheetname"
    "SubMcu")``, ``"/SubMcu/Inner"`` for a grandchild, and so on.  A local
    label ``NAME`` on that sheet is therefore named
    ``f"{sheet_path}/{NAME}"`` — exactly the identity KiCad writes into its
    netlist and onto the PCB (``/SENSE`` at the root, ``/SubMcu/DBG_LED``
    one level down; verified against ``kicad-cli`` 10.0.6, issue #5809).
    It is built from ``Sheetname``, not ``Sheetfile``: the filename is not
    part of a KiCad net name.

    A sheet *file* reached twice is still walked only once (the ``visited``
    guard, needed for circular-reference safety).  Two instances of one
    sub-sheet therefore contribute a single sheet path rather than one per
    instance; per-instance identities need the cross-instance sheet-pin
    unification that is still out of scope here (issue #4099 Phase 2).
    """
    from kicad_tools.operations.netlist import _get_sheet_entries
    from kicad_tools.schematic.models.schematic import Schematic

    visited: set[Path] = set()

    def _walk(path: Path, sheet_path: str):
        resolved = path.resolve()
        if resolved in visited or not path.exists():
            return
        visited.add(resolved)
        yield sheet_path, Schematic.load(str(path))
        parent_dir = path.parent
        for entry in _get_sheet_entries(path):
            # Fall back to the file stem when a sheet carries no
            # ``Sheetname`` property: hand-written and machine-generated
            # sheets in the wild sometimes omit it, and an empty segment
            # would silently collapse a child's labels onto its parent's
            # qualification ("//NAME" / "/NAME").
            segment = entry.sheetname or Path(entry.filename).stem
            yield from _walk(parent_dir / entry.filename, f"{sheet_path}/{segment}")

    yield from _walk(Path(sch_path), "")


def _sheet_local_label_names(sch) -> frozenset[str]:
    """Return the net names on ``sch`` that are driven *only* by local labels.

    KiCad scopes a plain ``(label ...)`` to its own sheet and names the
    resulting net with the sheet path (``/SENSE``, ``/SubMcu/DBG_LED``).
    The other three name sources on a sheet are **not** sheet-scoped and
    keep their bare spelling in KiCad's netlist:

    * ``global_labels`` — global by definition (``GLOB``, never ``/GLOB``);
    * ``power_symbols`` — ``GND`` / ``+3V3`` / ``PWR_FLAG`` behave as
      globals;
    * ``hier_labels`` — a sheet-pin net's name comes from wherever the net
      sits *highest* in the hierarchy, which a single-sheet resolution
      cannot know (a parent local label wins over the child's hierarchical
      label: ``kicad-cli`` names that net ``/PARENTNET``, not
      ``/SubMcu/DBG``).  Guessing the child's own path here would invent a
      *different* wrong answer, so these stay unqualified exactly as
      before — cross-sheet unification remains issue #4099's Phase 2.

    A name that appears as a local label *and* as one of those three on the
    same sheet is excluded: the non-local driver wins in KiCad, and
    qualifying it would break a net that legitimately spans sheets.  Being
    a *set of names* (rather than a per-pin provenance record) is enough
    because the resolved value this is tested against is itself a net name:
    a net that carries the local label ``SENSE`` is exactly the net whose
    resolved name is ``"SENSE"``.
    """
    local = {label.text for label in sch.labels if label.text}
    if not local:
        return frozenset()
    non_local = {gl.text for gl in sch.global_labels if gl.text}
    non_local |= {hl.text for hl in sch.hier_labels if hl.text}
    non_local |= {pwr.net_name for pwr in sch.power_symbols if pwr.net_name}
    return frozenset(local - non_local)


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

    **Local labels are sheet-qualified** (issue #5809).  A plain
    ``(label "SENSE")`` is scoped to its sheet, and KiCad names the
    resulting net with that sheet's path — ``/SENSE`` on the root sheet,
    ``/SubMcu/DBG_LED`` inside a sheet named ``SubMcu``.  This function
    returns the same spelling, so a schematic compares equal to the PCB
    KiCad itself derived from it.  Returning the bare label text made every
    named local net a false mismatch (``SENSE`` vs ``/SENSE``): 291 of them
    on the board in issue #5809, and the child-sheet form of the same
    defect in issue #5815.

    Only local labels are qualified.  Global labels, power-symbol nets
    (``GND``, ``+3V3``, ``PWR_FLAG``) and hierarchical-label / sheet-pin
    nets keep their bare spelling, matching KiCad for the first two and
    preserving the previous behaviour for the third — see
    :func:`_sheet_local_label_names` for why a sheet-pin net's correct
    qualification cannot be decided one sheet at a time.  Auto-generated
    ``Net-(...)`` placeholders are likewise left alone; their cross-tool
    spelling is reconciled by the partition comparison in
    :func:`compare_netlists`.  Full cross-instance sheet-pin unification
    (the same sub-sheet placed twice under different parent net contexts)
    is still out of scope here (a Phase 2 concern, issue #4099).
    """
    out: dict[tuple[str, str], str | None] = {}
    for sheet_path, sch in _walk_hierarchy_schematics(Path(sch_path)):
        local_names = _sheet_local_label_names(sch)
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
            out[key] = f"{sheet_path}/{net}" if net in local_names else net
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

    **A local net's bare spelling is accepted as an alias for its
    sheet-qualified identity where that is unambiguous** (issue #5809).  A
    local label is a sheet-scoped name: KiCad writes it ``/SENSE`` (root
    sheet) or ``/SubMcu/SENSE`` (child sheet), while a PCB whose nets came
    from kicad-tools' pure-Python netlist fallback writes the same node
    bare.  A bare PCB name therefore matches a qualified schematic name
    with the same leaf — but only when exactly one schematic net has that
    leaf, the bare name is not a net of its own, and the board does not
    also use the qualified spelling.  Nothing broader is relaxed: a
    different leaf (``/SENSE`` vs ``RETURN``) is a swap and still
    mismatches; two different qualifications (``/SENSE`` vs
    ``/SubMcu/SENSE``) are different nets and still mismatch; two sibling
    sheets that each carry a local ``SENSE`` grant no alias at all, so a
    board that collapses them onto one bare ``SENSE`` still fails on every
    affected pad.

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

    # --- Sheet-qualified vs bare spelling of one local net (issue #5809) ---
    #
    # A local label's net identity is ``/SENSE`` (root) or ``/SubMcu/SENSE``
    # (child) in KiCad and in :func:`_schematic_pin_to_net`, but bare
    # ``SENSE`` on a PCB whose nets came from kicad-tools' pure-Python
    # netlist fallback.  Both name the same node, so a pad may carry either.
    #
    # The bare spelling is accepted as an alias for exactly one qualified
    # schematic identity, and **only where the two documents prove it cannot
    # mean a second net**.  Three conditions, all required:
    #
    #   1. exactly one schematic identity has that leaf name.  Two sibling
    #      sheets that each label a net ``SENSE`` give ``/A/SENSE`` and
    #      ``/B/SENSE``; a board that binds both sheets' pads to one bare
    #      ``SENSE`` has shorted two nets, so no alias is granted and every
    #      affected pad is reported (the #5815 distinctness criterion);
    #   2. the bare name is not itself a schematic net identity elsewhere —
    #      otherwise a global label / power net named ``SENSE`` would be
    #      conflated with the local ``/A/SENSE``;
    #   3. the PCB does not *also* use the qualified spelling somewhere.  A
    #      board carrying both ``/A/SENSE`` and ``SENSE`` is distinguishing
    #      two nets; honouring its distinction keeps the mismatch.
    #
    # This is narrower than "ignore slashes" in every direction that
    # matters.  A different leaf name is never an alias, so a swap
    # (``/SENSE`` vs ``RETURN``) is still reported.  Two different
    # qualifications are never aliases, so ``/SENSE`` (root) vs
    # ``/SubMcu/SENSE`` (child) — genuinely different nets in KiCad — still
    # mismatch.  And the tolerance is one-directional: a *schematic* net
    # that is bare while the PCB is qualified is left alone, because that is
    # the sheet-pin / hierarchical-label case whose correct qualification
    # cannot be decided one sheet at a time (see
    # :func:`_sheet_local_label_names`); inventing an alias there could
    # paper over a real cross-sheet binding error.
    sch_names = {name for name in sch_map.values() if name is not None}
    pcb_names = {name for name in norm_pcb_map.values() if name is not None}
    qualified_by_leaf: dict[str, set[str]] = {}
    for name in sch_names:
        leaf = _bare_local_name(name)
        if leaf is not None:
            qualified_by_leaf.setdefault(leaf, set()).add(name)

    def _bare_is_alias_of(qualified: str | None, bare: str | None) -> bool:
        """True when PCB-side ``bare`` unambiguously means ``qualified``."""
        if qualified is None or bare is None:
            return False
        leaf = _bare_local_name(qualified)
        if leaf is None or leaf != bare:
            return False
        return (
            qualified_by_leaf.get(bare) == {qualified}  # (1) unique owner
            and bare not in sch_names  # (2) not a net of its own
            and qualified not in pcb_names  # (3) board keeps one spelling
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
        if _bare_is_alias_of(sch_net, pcb_net):
            # One local net: sheet-qualified on the schematic side, bare on
            # the board, and unambiguously the same net (#5809).
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
