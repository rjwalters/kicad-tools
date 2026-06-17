"""Independent copper-extracted LVS (issue #3742).

This is the "third leg" of board soundness the external reviewer asked
for: a netlist-correspondence gate that is independent of the router's
pad-net labels.  Where :mod:`kicad_tools.lvs.board_lvs` trusts each
pad's declared ``(net K "NAME")`` child (and so passes a board whose
router mislabels its own copper), this comparator extracts the
*physical* pad partition straight from routed copper — via
:meth:`ConnectivityValidator.extract_pad_partition`, which never reads a
net label — and diffs that against the schematic partition.

Two failure classes are reported:

* **short** — two pads on *different* schematic nets land in the *same*
  copper component (copper fuses nets that should be isolated).  This is
  the board-00 failure mode: ``GND`` shorted to ``LED_ANODE`` because the
  router wired copper to the wrong pad, even though the pad labels still
  read correctly.
* **open** — two pads on the *same* schematic net land in *different*
  copper components (the net is not fully connected by copper).

The comparator is pure: it returns data, never raises for a mismatch.
Pads present on only one side (schematic-only or PCB-only) are ignored
for the partition diff — that asymmetry is the label-based comparator's
job (:func:`board_lvs.compare_netlists`), and the two checks are meant
to run side by side.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kicad_tools.lvs.board_lvs import _schematic_pin_to_net


@dataclass(frozen=True)
class CopperLVSMismatch:
    """A single copper-vs-schematic partition disagreement.

    Attributes:
        kind: ``"short"`` (different schematic nets fused in copper) or
            ``"open"`` (same schematic net split across copper islands).
        net_a / net_b: The schematic net names involved.  For a short
            these differ; for an open they are equal.
        pad_a / pad_b: The two offending pads (``"REF.PAD"`` form) that
            witness the mismatch.
    """

    kind: str
    net_a: str
    net_b: str
    pad_a: str
    pad_b: str


@dataclass(frozen=True)
class CopperLVSResult:
    """Outcome of comparing routed copper against the schematic netlist.

    ``clean`` is ``True`` iff ``mismatches`` is empty.  ``shorts`` and
    ``opens`` partition the mismatches by kind for convenient reporting.
    """

    clean: bool
    mismatches: tuple[CopperLVSMismatch, ...]

    @property
    def shorts(self) -> tuple[CopperLVSMismatch, ...]:
        return tuple(m for m in self.mismatches if m.kind == "short")

    @property
    def opens(self) -> tuple[CopperLVSMismatch, ...]:
        return tuple(m for m in self.mismatches if m.kind == "open")


def compare_partitions(
    schematic_net_of_pad: dict[tuple[str, str], str | None],
    copper_partition: list[frozenset[str]],
) -> CopperLVSResult:
    """Diff a physical copper partition against a schematic netlist.

    This is the pure core, decoupled from any file IO so it can be unit
    tested with hand-built inputs.

    Args:
        schematic_net_of_pad: ``{(ref, pad) -> net_name | None}`` as built
            by :func:`board_lvs._schematic_pin_to_net`.  ``None`` means the
            pin is floating in the schematic and is excluded from the diff.
        copper_partition: list of ``frozenset`` pad-id groups (``"REF.PAD"``
            form) from :meth:`ConnectivityValidator.extract_pad_partition`.

    Returns:
        :class:`CopperLVSResult`.  A short is reported once per offending
        net pair (the lexicographically smallest pad witnesses are used);
        an open is reported once per pair of same-net copper islands.
    """
    # Build {pad_id -> schematic_net} restricted to pads that (a) have a
    # real schematic net and (b) actually appear on the board, so the diff
    # only considers pads both sides agree exist.  Pads only on one side are
    # the label-based comparator's concern.
    on_board: set[str] = set()
    for comp in copper_partition:
        on_board |= comp

    pad_net: dict[str, str] = {}
    for (ref, pad), net in schematic_net_of_pad.items():
        if net is None:
            continue
        pad_id = f"{ref}.{pad}"
        if pad_id in on_board:
            pad_net[pad_id] = net

    # Map each pad to its copper-component index.
    comp_of_pad: dict[str, int] = {}
    for idx, comp in enumerate(copper_partition):
        for pad_id in comp:
            comp_of_pad[pad_id] = idx

    mismatches: list[CopperLVSMismatch] = []

    # --- Shorts: within each copper component, every distinct schematic
    #     net present is a short against every other distinct net there. ---
    seen_short_pairs: set[tuple[str, str]] = set()
    for comp in copper_partition:
        # Net name -> representative (smallest) pad id in this component.
        net_rep: dict[str, str] = {}
        for pad_id in sorted(comp):
            net = pad_net.get(pad_id)
            if net is None:
                continue
            net_rep.setdefault(net, pad_id)
        nets_here = sorted(net_rep)
        for i, na in enumerate(nets_here):
            for nb in nets_here[i + 1 :]:
                key = (na, nb)
                if key in seen_short_pairs:
                    continue
                seen_short_pairs.add(key)
                mismatches.append(
                    CopperLVSMismatch(
                        kind="short",
                        net_a=na,
                        net_b=nb,
                        pad_a=net_rep[na],
                        pad_b=net_rep[nb],
                    )
                )

    # --- Opens: for each schematic net, the pads carrying it must all live
    #     in one copper component.  If they span multiple components the net
    #     is not fully routed (open). ---
    net_to_pads: dict[str, list[str]] = {}
    for pad_id, net in pad_net.items():
        net_to_pads.setdefault(net, []).append(pad_id)

    for net, pads in sorted(net_to_pads.items()):
        if len(pads) < 2:
            continue
        # Group these pads by copper component.
        comps: dict[int, list[str]] = {}
        for pad_id in sorted(pads):
            comps.setdefault(comp_of_pad[pad_id], []).append(pad_id)
        if len(comps) <= 1:
            continue
        # Report one open per pair of distinct islands, using the smallest
        # pad in each island as the witness.
        island_reps = [sorted(members)[0] for members in comps.values()]
        island_reps.sort()
        for i in range(len(island_reps) - 1):
            mismatches.append(
                CopperLVSMismatch(
                    kind="open",
                    net_a=net,
                    net_b=net,
                    pad_a=island_reps[i],
                    pad_b=island_reps[i + 1],
                )
            )

    return CopperLVSResult(clean=not mismatches, mismatches=tuple(mismatches))


def compare_copper_netlist(sch_path: str | Path, pcb_path: str | Path) -> CopperLVSResult:
    """Compare a routed PCB's *copper* against the schematic netlist.

    Loads the schematic partition (label-bound nets, PWR_FLAG-safe) via
    :func:`board_lvs._schematic_pin_to_net`, extracts the physical copper
    partition from the PCB via
    :meth:`ConnectivityValidator.extract_pad_partition`, then diffs them
    with :func:`compare_partitions`.

    Args:
        sch_path: Path to a ``.kicad_sch`` (root sheet for hierarchy).
        pcb_path: Path to a routed ``.kicad_pcb``.

    Returns:
        :class:`CopperLVSResult`.  Always returned — mismatches are data,
        not exceptions.
    """
    # Import lazily: ConnectivityValidator pulls in the PCB schema stack and
    # we want ``import kicad_tools.lvs`` to stay cheap.
    from kicad_tools.validate.connectivity import ConnectivityValidator

    sch_path = Path(sch_path)
    pcb_path = Path(pcb_path)

    schematic_net_of_pad = _schematic_pin_to_net(sch_path)
    copper_partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    return compare_partitions(schematic_net_of_pad, copper_partition)
