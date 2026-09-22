"""Consumer-adapter protocol, the canonical :class:`Verdict`, and the adapters.

This package is the **split line** of Epic #5509's conformance harness.  Every
module *outside* it -- generator, board writer, oracle, fixtures, report -- is
consumer-free by construction (``test_report_shape.py``'s
``test_truth_side_does_not_import_the_code_it_measures`` enforces it), so the
truth side cannot drift towards the implementations it measures.  Inside this
package, ``import kicad_tools.router`` / ``import kicad_tools.validate`` is
exactly the point.

*This module itself* still imports no consumer: it holds only the vocabulary
(:class:`Verdict`) and the contract (:class:`ConsumerAdapter`).  The concrete
adapters live in sibling modules:

=========================  =====  ==============================================
module                     group  consumer
=========================  =====  ==============================================
``occupancy``                1    Python grid occupancy (halo cell marking)
``grid_cpp_occupancy``       2    C++ ``mark_segment`` / ``mark_via`` (write side)
``grid_cpp_occupancy``       3    C++ ``is_trace_blocked`` disc kernel (read side)
``route_halo``               4    ``RouteHaloGeometry.clear`` refinement
``route_geometry_cpp``       5    ``Grid3D::route_*_geometry_clear``
``fixed_copper``             6    ``FixedFillObstacles`` + ``fixed_fill_clear``
``coupled``                  7    ``CoupledPathfinder::rail_clear``
``diffpair``                 8    the coupled constructor's three gates
``lattice``                  9    ``CommittedCopper.seg_clear`` / ``via_clear``
``mesh``                    10    ``ObstacleModel.is_clear`` (per-leg consult)
``via_clearance``           11    ``via_clearance.py``'s four pure predicates
``grid_py``                 12    Python commit gate (``validate_*_clearance``)
``grid_cpp``                13    C++ commit gate (``Grid3D::validate_route``)
``pairwise``                14    ``PairwiseClearanceTable.path_is_clear``
``optimizer``               15    optimizer ``*CollisionChecker.path_is_clear``
``match_group``             16    ``match_group_tuning`` post-insertion detail
``drc_nudge``               17    ``drc_nudge``'s destination foreign-via gate
``kct_check``               18    ``kct check``'s ``ClearanceRule``
``drc_cpp``                 19    incremental placement ``check_pair_clearance``
``kernel``                   0    the Phase 1b kernel -- the **control** row
=========================  =====  ==============================================

**All nineteen groups are wired.**  Phase 1c left exactly one gap -- group 7's
``rail_clear``, a lambda inside the C++ coupled search loop that no binding
could reach -- recorded in ``report.NOT_MEASURED_REASONS`` as *unexposed*.
Epic #5509 Phase 3c (#5662) promoted it to a bound method while migrating it
onto the clearance kernel, so ``adapters/coupled.py`` measures it and
``test_corpus.UNWIRED_GROUPS`` is now empty.  The kernel's
``group`` is the :data:`~tests.conformance.adapters.kernel.KERNEL_GROUP`
sentinel ``0``: it is not one of the epic's nineteen consumer groups, it is the
model they are to be unified onto, so it renders in its own section.

``report.MIGRATED_GROUPS`` records which consumer groups have actually been
switched onto the kernel.  A migrated group's row stops being report-only:
see :mod:`tests.conformance.conftest` for exactly what hardens and what does
not.

Each drives an **unmodified** consumer through
:mod:`tests.conformance.adapters._support`, which is the single translation
from the harness's ``CopperCase`` into the objects those consumers speak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tests.conformance.generator import CopperCase

__all__ = [
    "BOARD_EDGE",
    "KIND_CLEARANCE",
    "KIND_COPPER_EDGE",
    "KIND_HOLE_CLEARANCE",
    "KIND_HOLE_TO_HOLE",
    "VERDICT_KINDS",
    "ConsumerAdapter",
    "Verdict",
]


# The four clearance-family verdict kinds the oracle and every adapter speak.
# These are *canonical* names, not kicad-cli's raw ``type`` strings -- the
# oracle maps raw strings onto these (see ``oracle.RAW_TYPE_TO_KIND``) so a
# kicad-cli version bump that renames an error code is a one-line change in
# one place.
KIND_CLEARANCE = "clearance"
"""Copper-to-copper edge gap (track/via/pad/zone against another net)."""

KIND_HOLE_CLEARANCE = "hole_clearance"
"""Drilled hole against foreign copper."""

KIND_HOLE_TO_HOLE = "hole_to_hole"
"""Drilled hole against another drilled hole (edge-to-edge)."""

KIND_COPPER_EDGE = "copper_edge_clearance"
"""Copper against the board outline."""

VERDICT_KINDS: frozenset[str] = frozenset(
    {KIND_CLEARANCE, KIND_HOLE_CLEARANCE, KIND_HOLE_TO_HOLE, KIND_COPPER_EDGE}
)

BOARD_EDGE = "<board-edge>"
"""Pseudo-net standing in for the board outline in a :class:`Verdict` pair.

``copper_edge_clearance`` is a one-sided finding; giving the edge a reserved
pseudo-net keeps every verdict a symmetric two-element pair.
"""


@dataclass(frozen=True)
class Verdict:
    """One "these two things are too close" finding, model-independent.

    Identity is deliberately **only** ``(kind, nets)``.  Every clearance model
    in the tree computes its own gap with its own pad/segment/via geometry, so
    comparing ``gap_mm`` across models would measure arithmetic noise rather
    than the thing under test: *did this model flag this pair at all?*
    ``gap_mm`` / ``required_mm`` / ``zone`` are recorded for the report table
    and are excluded from ``__eq__`` / ``__hash__`` (``compare=False``).

    Pair identity is by **net name**, which is why
    :func:`tests.conformance.generator.generate_case` gives every generated
    object its own net: a kicad-cli row then maps onto a pair by
    ``frozenset(nets)`` alone, with no geometry matching and no reliance on
    the reported ``pos``.

    Attributes:
        kind: One of :data:`VERDICT_KINDS`.
        nets: The unordered pair of net names involved.  ``copper_edge``
            findings pair the net with :data:`BOARD_EDGE`.
        gap_mm: Measured edge-to-edge gap, when the model reports one.
            ``None`` for models that answer in cells rather than millimetres
            (e.g. the grid-occupancy adapter).
        required_mm: The clearance the model required for this pair.
        zone: ``True`` when at least one side of the pair is zone-fill copper.
            Zone verdicts are only meaningful against freshly refilled zones,
            so callers drop them for ``refill=False`` runs via
            :meth:`tests.conformance.oracle.OracleResult.without_zones`.
    """

    kind: str
    nets: frozenset[str]
    gap_mm: float | None = field(default=None, compare=False)
    required_mm: float | None = field(default=None, compare=False)
    zone: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        if self.kind not in VERDICT_KINDS:
            raise ValueError(
                f"Unknown verdict kind {self.kind!r}; expected one of {sorted(VERDICT_KINDS)}"
            )

    @classmethod
    def pair(
        cls,
        kind: str,
        net_a: str,
        net_b: str,
        *,
        gap_mm: float | None = None,
        required_mm: float | None = None,
        zone: bool = False,
    ) -> Verdict:
        """Build a verdict from two net names (order is irrelevant)."""
        return cls(
            kind=kind,
            nets=frozenset({net_a, net_b}),
            gap_mm=gap_mm,
            required_mm=required_mm,
            zone=zone,
        )

    def describe(self) -> str:
        """Human-readable one-liner for test failure output."""
        a, b = sorted(self.nets)
        detail = ""
        if self.gap_mm is not None and self.required_mm is not None:
            detail = f" (gap {self.gap_mm:.4f} mm, required {self.required_mm:.4f} mm)"
        return f"{self.kind}: {a} <-> {b}{detail}"


@runtime_checkable
class ConsumerAdapter(Protocol):
    """A thin, read-only wrapper around one in-tree clearance consumer.

    An adapter answers exactly the question the oracle answers -- "which pairs
    in this case are too close?" -- using an **unmodified** consumer.  It never
    patches, relaxes or re-implements the consumer it wraps; if a consumer
    cannot express a question (no zone model, cell-space only), the adapter
    reports that by simply not emitting verdicts of that kind and the report
    marks the cell accordingly.

    Implementations live in the sibling modules listed in this package's
    module docstring.
    """

    #: Short stable identifier used as the table row key (e.g. ``"grid_py"``).
    name: str

    #: The Epic #5509 section-1 implementation group this adapter measures.
    group: int

    #: The :class:`tests.conformance.generator.PairKind` values this consumer
    #: is *consulted for* in production.  It is a scope declaration, not a
    #: results filter: a pair kind outside this set is not compared at all,
    #: because counting it would measure the absence of a check somewhere else
    #: in the pipeline rather than a disagreement in *this* model.  Every
    #: adapter must state why it narrows the set, in its module docstring.
    pair_kinds: frozenset[str]

    def available(self) -> bool:
        """Whether this adapter can run on this machine.

        ``False`` (e.g. the C++ router extension is not built) makes the
        adapter's group render ``not measured`` rather than silently
        contributing a zero-disagreement row -- the same "no answer beats a
        confident wrong answer" rule the oracle applies to a missing
        kicad-cli.
        """
        ...

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        """Return the set of pairs this consumer considers too close."""
        ...
