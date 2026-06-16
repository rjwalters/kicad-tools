"""LVS (Layout-vs-Schematic) verification for kicad-tools boards.

Compares the schematic netlist (the design intent) against the routed PCB
(what manufacturing will see) and reports any per-pin net mismatches.

Currently single-board scope (board 00); see issue #3742 for the
generalized fleet-wide LVS rollout.

Public API:

* :func:`compare_netlists` — pure comparator returning :class:`LVSResult`.
* :class:`LVSResult`        — frozen dataclass with ``clean`` flag and
                              tuple of :class:`LVSMismatch` entries.
* :class:`LVSMismatch`      — frozen dataclass naming the offending
                              ``(ref, pad)`` and both nets.
* :class:`BoardNetlistMismatch` — exception carrying the LVSResult;
                                  raised by board recipes, never by the
                                  comparator itself.
* :func:`_ref_of`           — helper that resolves a footprint's
                              reference designator across both KiCad
                              serializer dialects (kept underscored
                              because it is implementation detail that
                              board recipes also call directly while
                              the #3742 deduplication is pending).
"""

from kicad_tools.lvs.board_lvs import (
    BoardNetlistMismatch,
    LVSMismatch,
    LVSResult,
    _ref_of,
    compare_netlists,
)

__all__ = [
    "BoardNetlistMismatch",
    "LVSMismatch",
    "LVSResult",
    "_ref_of",
    "compare_netlists",
]
