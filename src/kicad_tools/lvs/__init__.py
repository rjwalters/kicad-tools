"""LVS (Layout-vs-Schematic) verification for kicad-tools boards.

Compares the schematic netlist (the design intent) against the routed PCB
(what manufacturing will see) and reports any per-pin net mismatches.

Currently single-board scope (board 00); see issue #3742 for the
generalized fleet-wide LVS rollout.

Two complementary comparators live here:

* **label-based** (:func:`compare_netlists`) trusts each pad's declared
  ``(net ...)`` label.
* **copper-extracted** (:func:`compare_copper_netlist`, issue #3742)
  ignores pad labels and diffs the *physical* copper partition against the
  schematic, catching shorts/opens a mislabeled router would hide.

Public API:

* :func:`compare_netlists` — pure comparator returning :class:`LVSResult`.
* :func:`compare_copper_netlist` — independent copper-extracted comparator
                              returning :class:`CopperLVSResult`.
* :func:`compare_partitions` — pure partition diff (IO-free core of the
                              copper-extracted comparator).
* :class:`CopperLVSResult` / :class:`CopperLVSMismatch` — copper-LVS
                              result + per-short/open record.
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
from kicad_tools.lvs.copper_lvs import (
    CopperLVSMismatch,
    CopperLVSResult,
    compare_copper_netlist,
    compare_partitions,
)
from kicad_tools.lvs.recipe import (
    ADVISORY_LVS_BOARDS,
    FreshCopperCheckError,
    write_lvs_report,
)

__all__ = [
    "ADVISORY_LVS_BOARDS",
    "BoardNetlistMismatch",
    "CopperLVSMismatch",
    "CopperLVSResult",
    "FreshCopperCheckError",
    "LVSMismatch",
    "LVSResult",
    "_ref_of",
    "compare_copper_netlist",
    "compare_netlists",
    "compare_partitions",
    "write_lvs_report",
]
