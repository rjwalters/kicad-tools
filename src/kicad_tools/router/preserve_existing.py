"""Already-complete-net detection for ``--preserve-existing`` (Issue #5788).

``kct route --preserve-existing`` promises (per its ``--help``) that existing
copper is loaded as an immovable obstacle and re-emitted unchanged "so only
unconnected nets are routed".  Loading the copper as an obstacle was the only
half that was actually implemented: nothing removed an *already complete* net
from the routable set, and both routing engines deliberately let a net that IS
in the routable set replace its own copper::

    fixed_copper = [r for r in self.existing_routes if r.net not in self.nets]

(``core.py``'s mesh and lattice netset drivers -- correct for ``--nets`` /
``--region``, where the caller explicitly asked for those nets to be
re-routed).  The net effect on board 06 was that all 64 pre-routed, coupled
LVDS segments were replaced by freshly computed single-ended routes and the
generator's coupled geometry was silently lost.

This module supplies the missing half: the set of board nets that are
**already fully connected in the input**, so the CLI can hold them out of the
routable set exactly the way ``--skip-nets`` already does (their copper is
still loaded as a hard obstacle and re-emitted verbatim by the
``--preserve-existing`` writer).

Why :class:`~kicad_tools.analysis.net_status.NetStatusAnalyzer` and not the
router's own :func:`~kicad_tools.router.observability.validate_net_connectivity`:

* ``NetStatusAnalyzer`` is the connectivity model ``kct check``'s
  ``connectivity`` rule, ``kct net-status`` and ``kct fleet status`` all
  consume, so "a net this module calls complete" is byte-for-byte "a net
  ``kct check`` does not report as unrouted / partially routed".  The flag's
  promise and the board's own DRC verdict cannot drift apart.
* It is zone-aware (a pad inside a same-net filled zone counts as connected)
  and, in ``strict`` mode, decides copper contact by real shapely geometry
  rather than the 0.01 mm endpoint-proximity tolerance
  ``validate_net_connectivity`` uses.  Over-connecting here would be the
  dangerous direction: it would hold a genuinely stranded net out of routing
  and ship a board that fails connectivity DRC.
* It runs in-process on an already-parsed board -- unlike
  :func:`~kicad_tools.router.partial_rescue.partially_connected_signal_nets`,
  which shells out to a full ``kct check`` subprocess.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = ["fully_connected_nets"]


def fully_connected_nets(
    pcb_path: Path | str | None = None,
    *,
    pcb: PCB | None = None,
    strict: bool = True,
) -> list[str]:
    """Return the names of multi-pad nets already fully connected on the board.

    A net qualifies when it has **two or more pads** and
    :class:`~kicad_tools.analysis.net_status.NetStatusAnalyzer` reports its
    status as ``"complete"`` -- i.e. every pad of the net sits on one
    contiguous piece of copper (traces, vias and/or same-net filled zones).
    Such a net has nothing left to route, so ``--preserve-existing`` must leave
    it (and its copper) alone instead of re-routing it from scratch.

    Single- and zero-pad nets are never returned: they carry no routable work
    either way, and adding them to a skip list would only perturb the pour /
    net-class machinery that keys off it.

    Deliberately NARROW: only ``status == "complete"`` counts.  A pour net
    whose ``incomplete`` status is a stitching residual (the
    ``has_filled_zone and is_advisory_incomplete`` suppression ``kct check``
    applies) is NOT reported here -- it stays routable exactly as before, so
    this helper can only ever *remove* work that provably has nothing to do.

    Args:
        pcb_path: Path to the ``.kicad_pcb`` file to analyze.  Ignored when
            *pcb* is given.
        pcb: An already-parsed board, for callers that have one (avoids a
            second parse).  Mutually exclusive with *pcb_path* in practice;
            *pcb* wins.
        strict: Forwarded to :class:`NetStatusAnalyzer`.  ``True`` (the
            default, matching ``kct check``'s own default since Issue #4673)
            decides copper contact by real geometric intersection; ``False``
            opts into the legacy endpoint-proximity model.

    Returns:
        Sorted list of net names.  Empty when the board cannot be analyzed --
        a connectivity model that cannot run must never silently *widen* the
        preserved set, and an empty result reproduces the pre-#5788 behaviour
        for that board.
    """
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    board = pcb
    if board is None:
        if pcb_path is None:
            raise TypeError("fully_connected_nets() requires either pcb_path or pcb")
        from kicad_tools.schema.pcb import PCB as _PCB

        board = _PCB.load(str(pcb_path))

    analysis = NetStatusAnalyzer(board, strict=strict).analyze()
    complete: set[str] = set()
    for net_status in analysis.nets:
        if net_status.total_pads < 2:
            continue
        if net_status.status != "complete":
            continue
        if not net_status.net_name:
            continue
        complete.add(net_status.net_name)
    return sorted(complete)
