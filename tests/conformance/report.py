"""Render ``docs/clearance-conformance.md`` -- one row per consumer group.

The table's shape is the deliverable, not just its numbers.  Epic #5509
section 1 inventories **nineteen** implementation groups that carry their own
clearance arithmetic; the acceptance criterion is that every one of them
appears, with groups that have no adapter yet marked ``not measured`` rather
than quietly omitted.  An omitted row reads as "fine"; a ``not measured`` row
reads as "unknown", which is the truth and is what keeps later phases honest
about their own coverage.

:data:`ADAPTERS` wires one adapter per measured group.  Each adapter drives an
**unmodified** consumer, and an adapter that cannot run on this machine (e.g.
``grid_cpp`` without the compiled router extension) is skipped so its group
reads ``not measured`` rather than contributing a confidently-wrong
zero-disagreement row.

A group with no adapter is never silently omitted, and never merely stamped
``not measured`` either: :data:`NOT_MEASURED_REASONS` gives each one a stated
reason, rendered in the table's ``notes`` column.  ``not measured`` with no
reason is indistinguishable from "nobody looked", which is the failure mode
the whole document exists to prevent -- and it is what the epic means by
"group 7 is the only permitted *unexposed* entry": exactly one consumer is
unreachable from Python, and every other gap has to justify itself.

:data:`NOTES` carries the same column for *measured* rows, where it records
the sub-entry-points a row does **not** cover (the C++ pairwise threshold, the
sub-grid escape internals, the diff-pair variant of the match-group check),
so a measured row cannot imply more coverage than it has.

The Phase 1b **kernel** is measured too, by ``adapters/kernel.py``, but it is
not one of the nineteen groups -- it is the model they are to be unified onto
-- so it renders in its own section under the key
:data:`~tests.conformance.adapters.kernel.KERNEL_GROUP` (``0``).

Run as a module (``tests`` is a package and pytest's ``pythonpath`` only adds
``src``, so invoking this file by path cannot import its own package)::

    uv run python -m tests.conformance.report --seeds 0-23 \\
        --out docs/clearance-conformance.md

Without kicad-cli this exits non-zero with ``kicad_cli_absent`` and writes
nothing.  A table regenerated on a machine with no KiCad would show zero
disagreement everywhere -- a confident, wrong answer is worse than no answer.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tests.conformance.adapters import ConsumerAdapter
from tests.conformance.adapters.diffpair import DiffPairAdapter
from tests.conformance.adapters.drc_cpp import DrcCppAdapter
from tests.conformance.adapters.drc_nudge import DrcNudgeAdapter
from tests.conformance.adapters.fixed_copper import FixedCopperAdapter
from tests.conformance.adapters.grid_cpp import GridCppAdapter
from tests.conformance.adapters.grid_cpp_occupancy import (
    CppTraceBlockedAdapter,
    GridCppMarkingAdapter,
)
from tests.conformance.adapters.grid_py import GridPyAdapter
from tests.conformance.adapters.kct_check import KctCheckAdapter
from tests.conformance.adapters.kernel import KERNEL_GROUP, KernelAdapter
from tests.conformance.adapters.lattice import LatticeAdapter
from tests.conformance.adapters.match_group import MatchGroupAdapter
from tests.conformance.adapters.mesh import MeshAdapter
from tests.conformance.adapters.occupancy import OccupancyAdapter
from tests.conformance.adapters.optimizer import OptimizerCollisionAdapter
from tests.conformance.adapters.pairwise import PairwiseAdapter
from tests.conformance.adapters.route_geometry_cpp import RouteGeometryCppAdapter
from tests.conformance.adapters.route_halo import RouteHaloAdapter
from tests.conformance.adapters.via_clearance import ViaClearanceAdapter
from tests.conformance.board import write_case
from tests.conformance.generator import (
    BOUNDARY_BAND_MM,
    CopperCase,
    generate_corpus,
    parse_seed_range,
)
from tests.conformance.oracle import (
    REASON_ABSENT,
    KiCadCliAbsent,
    OracleResult,
    kicad_cli_available,
    run_oracle,
)

__all__ = [
    "ADAPTERS",
    "GROUPS",
    "KERNEL_GROUP",
    "NOTES",
    "NOT_MEASURED",
    "NOT_MEASURED_REASONS",
    "TABLE_BEGIN",
    "TABLE_END",
    "KERNEL_TABLE_BEGIN",
    "KERNEL_TABLE_END",
    "TABLE_COLUMNS",
    "AdapterMeasurement",
    "CorpusStats",
    "Group",
    "adapter_for_group",
    "main",
    "measure_corpus",
    "not_measured_reason",
    "render_document",
    "render_kernel_table",
    "render_table",
]

NOT_MEASURED = "not measured"

TABLE_BEGIN = "<!-- clearance-conformance:table:begin -->"
TABLE_END = "<!-- clearance-conformance:table:end -->"

KERNEL_TABLE_BEGIN = "<!-- clearance-conformance:kernel:begin -->"
KERNEL_TABLE_END = "<!-- clearance-conformance:kernel:end -->"

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "clearance-conformance.md"


@dataclass(frozen=True)
class Group:
    """One Epic #5509 section-1 implementation group.

    Attributes:
        number: The group's number in the epic's inventory (1-19).
        stage: Pipeline stage the group belongs to.
        label: Short human name used in the table.
        where: Primary source location(s), for navigation.
    """

    number: int
    stage: str
    label: str
    where: str


# The nineteen groups, verbatim from Epic #5509 section 1.  Pinned here as a
# constant (rather than parsed from the epic) so the table cannot silently
# lose a row: the renderer asserts it emitted exactly ``len(GROUPS)`` rows.
GROUPS: tuple[Group, ...] = (
    Group(
        1,
        "Search-time occupancy",
        "Python grid halo marking",
        "`router/grid.py` `_mark_segment` / `_mark_via` / `_get_clearance_mask` / `_dilate_blocked`",
    ),
    Group(
        2,
        "Search-time occupancy",
        "C++ grid halo marking",
        "`router/cpp/src/grid.cpp` `Grid3D::mark_segment` / `Grid3D::mark_via`",
    ),
    Group(
        3,
        "Search-time occupancy",
        "C++ A* blocked-cell kernel",
        "`router/cpp/src/pathfinder.cpp` `Pathfinder::is_trace_blocked`",
    ),
    Group(
        4,
        "Search-time refinement",
        "Python route-halo geometry",
        "`router/route_halo_geometry.py` `RouteHaloGeometry.clear`",
    ),
    Group(
        5,
        "Search-time refinement",
        "C++ route geometry",
        "`router/cpp/src/grid.cpp` `route_trace_geometry_clear` / `route_via_geometry_clear` / `trace_stored_vias_clear`",
    ),
    Group(
        6,
        "Search-time refinement",
        "Fixed-copper predicate",
        "`router/fixed_copper.py` `segment_clear` / `via_clear`; `grid.cpp` `Grid3D::fixed_fill_clear`",
    ),
    Group(
        7,
        "Search-time refinement",
        "C++ coupled rail check",
        "`router/cpp/src/coupled_pathfinder.cpp` `rail_clear`",
    ),
    Group(
        8,
        "Search-time refinement",
        "Python coupled / diff-pair predicates",
        "`router/diffpair_routing.py` `_segment_cells_clear` / `_span_pad_clear` / `_copper_conflicts` / ...",
    ),
    Group(
        9,
        "Search-time refinement",
        "Lattice engine obstacles",
        "`router/lattice/obstacles.py` `seg_clear` / `node_clear` / `via_clear`; `lattice/coupled.py`; `lattice/pairwise.py`",
    ),
    Group(
        10,
        "Search-time refinement",
        "Mesh engine obstacles",
        "`router/mesh/obstacles.py` `is_clear`",
    ),
    Group(
        11,
        "Search-time refinement",
        "Escape router / stitcher",
        "`router/via_clearance.py` `drill_hole_to_hole_clear` / `point_clear_of_copper` / ...; `router/subgrid.py`",
    ),
    Group(
        12,
        "Commit validation",
        "Python commit gate",
        "`router/grid.py` `validate_segment_clearance` / `validate_via_clearance` / `validate_via_to_via_clearance`",
    ),
    Group(
        13,
        "Commit validation",
        "C++ commit gate",
        "`router/cpp/src/grid.cpp` `Grid3D::validate_route`",
    ),
    Group(
        14,
        "Commit validation",
        "Pairwise net-class matrix",
        "`router/pairwise_clearance.py` `required_clearance` / `path_is_clear`; `grid.cpp` `pairwise_required_clearance`",
    ),
    Group(
        15,
        "Post-route",
        "Optimizer collision checks",
        "`router/optimizer/collision.py` `path_is_clear`; `router/optimizer/trace.py` `_path_is_clear`",
    ),
    Group(
        16,
        "Post-route",
        "Match-group tuning",
        "`router/match_group_tuning.py` `_post_insertion_clearance_detail_*`",
    ),
    Group(
        17,
        "Post-route",
        "DRC nudge repair",
        "`router/drc_nudge.py` `_post_nudge_introduces_foreign_via_violation` / `_via_edge_sweep_clear`",
    ),
    Group(
        18,
        "Post-route",
        "kct check ClearanceRule",
        "`validate/rules/clearance.py` `ClearanceRule` (exact polygon pad model); `SegmentZoneClearanceRule`; `ViaZoneClearanceRule`",
    ),
    Group(
        19,
        "Post-route",
        "Incremental placement DRC (drc_cpp)",
        "`drc/cpp/src/drc_clearance.cpp` `check_pair_clearance`",
    ),
)

assert len(GROUPS) == 19, "Epic #5509 section 1 inventories nineteen groups"


# The wired consumer adapters, in Epic #5509 section-1 group order.
#
# Importing them here does NOT put a consumer import in this module: an adapter
# module is the one legitimate place for ``import kicad_tools.router`` /
# ``kicad_tools.validate``, which is why
# ``test_truth_side_does_not_import_the_code_it_measures`` scopes itself to the
# harness's own top-level modules and exempts the ``adapters/`` package.  The
# generator, the board writer and the oracle -- the truth side proper -- still
# import nothing from the code under test.
ADAPTERS: tuple[ConsumerAdapter, ...] = (
    KernelAdapter(),  # group 0 -- the Phase 1b control row, not a consumer group
    OccupancyAdapter(),
    GridCppMarkingAdapter(),
    CppTraceBlockedAdapter(),
    RouteHaloAdapter(),
    RouteGeometryCppAdapter(),
    FixedCopperAdapter(),
    DiffPairAdapter(),
    LatticeAdapter(),
    MeshAdapter(),
    ViaClearanceAdapter(),
    GridPyAdapter(),
    GridCppAdapter(),
    PairwiseAdapter(),
    OptimizerCollisionAdapter(),
    MatchGroupAdapter(),
    DrcNudgeAdapter(),
    KctCheckAdapter(),
    DrcCppAdapter(),
)


# Why a group has no adapter.  A bare ``not measured`` is indistinguishable
# from "nobody looked"; every gap here states its kind.  Group 7 is the only
# entry, and it is the *unexposed* kind -- no Python entry point exists at all,
# which is exactly the one exception the epic's acceptance criterion permits.
NOT_MEASURED_REASONS: dict[int, str] = {
    7: (
        "**unexposed**: `CoupledPathfinder::rail_clear` "
        "(`coupled_pathfinder.cpp:627`) is a lambda inside the coupled search "
        "loop -- not a method, so `bindings.cpp` cannot reach it and no Python "
        "caller exists. Measured in its own Phase 3 PR, which can add the "
        "binding; this phase adds no C++."
    ),
}

# What a *measured* row does not cover.  Every sub-entry-point named in the
# epic's group inventory that this phase could not score is recorded here, so
# a measured row cannot imply more coverage than it has.
NOTES: dict[int, str] = {
    1: "Cell-set answer, so no mm gap is reported; square Chebyshev halo (#5410).",
    2: (
        "Write side: `mark_segment` / `mark_via` square halo. Routed copper "
        "only -- the C++ grid never marks pads itself "
        "(`CppGrid.from_routing_grid` copies the Python blocked plane), so "
        "pad pairs would re-measure group 1."
    ),
    3: (
        "Read side: the Euclidean-disc acceptance kernel (#3229), narrower "
        "than group 2's square by construction. Via candidates go through the "
        "sibling `is_via_blocked`."
    ),
    4: "Raises the requirement to `max(required, via_clearance)` for trace-vs-via.",
    5: (
        "Measures the `Grid3D` predicate; the `Pathfinder` wrappers "
        "(`trace_halo_cell_clear`, `via_route_geometry_clear`) are unbound and "
        "add only cell-to-world conversion, per-net `search_fill_*` overrides "
        "and a `route_cell_has_geometry` pre-check -- no arithmetic."
    ),
    6: (
        "Both halves driven (Python `FixedFillObstacles` + native "
        "`Grid3D::fixed_fill_clear`); a pair is flagged when either refuses. "
        "The fill polygon is the pad's exact outline via the `_pad_polygon` "
        "reference model, so the row measures group 6's arithmetic, not a "
        "harness approximation."
    ),
    8: (
        "Three gates in series (`_segment_cells_clear` raster walk through the "
        "**Python** `CoupledPathfinder._is_cell_blocked`, the #4571 exact "
        "`_segment_pad_clear`, the #4575 `_route_via_clear`); flagged when any "
        "refuses. The via gate is driven inside the consumer's own "
        "`_shadow_foreign_copper` context manager -- outside it "
        "`_shadow_foreign_universe` is `None` and the gate short-circuits to "
        "`(0.0, None)`, so an unarmed run would have reported a confident zero "
        "for a third of this row. **Not measured**: "
        "`find_intra_pair_clearance_violations` (`diffpair_routing.py:1198`) "
        "scores a P/N pair against *itself*, and this corpus declares no diff "
        "pairs -- every object carries its own independent net so a kicad-cli "
        "row maps onto a pair by net alone."
    ),
    9: (
        "`CommittedCopper.seg_clear` / `via_clear` -- the epic's `:583` / "
        "`:709` citations land on `CommittedCopper`, not "
        "`LatticeObstacleModel`, so the row needs no board file and no "
        "`from_board`: the five derived gap values are copied from the model's "
        "one production call site (`lattice/pathfinder.py:252-258`, `:460`). "
        "Routed copper only -- none of the three predicates consults a pad "
        "(pad keep-outs gate *site availability* on "
        "`LatticeObstacleModel.node_pads`, which is group 9's masking half, not "
        "its clearance arithmetic). Centreline answers, so no mm gap is "
        "reported. `pairwise` left `None`, the only path reachable without "
        "`--voltage-map` (Phase 2's corpus)."
    ),
    10: (
        "`ObstacleModel.is_clear`, constructed directly as "
        "`mesh/pathfinder.py:303-313` does: other-net pads as keep-out rects "
        "inflated by the agent radius, committed traces as capsule polygons "
        "inflated by a **full** `trace_width + clearance` (`_route_obstacles`' "
        "own over-approximation, square end-caps included). Seg-candidate "
        "pairs only -- `is_clear(a, b)` takes two points and no width, and a "
        "committed via is not in this model at all (`_route_via_injection` "
        "handles those). `fixed_fills` left `None`: group 6 already measures "
        "`FixedFillObstacles` on both its halves. **Not measured**: the `pours` "
        "zone branch and the `outline` containment branch need a zone and an "
        "edge pair kind, which the generator does not place -- tracked as #5644."
    ),
    11: (
        "`via_clearance.py`'s four pure predicates. **Not measured**: "
        "`subgrid.py:550 _min_clearance_to_neighbors` and `:1063 "
        "_validate_segment_relaxed` are private `SubGridRouter` internals "
        "needing a live sub-grid escape context (parent grid, pad-cluster box, "
        "per-pad overrides) -- escape router's Phase 3 PR."
    ),
    12: "Via-first insertion order only; the other order is #5398's fixture.",
    13: (
        "Via-first insertion order only. Pads carry no roundrect/oval flag -- "
        "`Grid3D::add_pad` takes `is_circular` and a rotation and nothing else, "
        "which is the `roundrect-corner-gap` mechanism."
    ),
    14: (
        "Scalar (`dru`) behaviour: every net at 0 V, so no creepage widening "
        "applies and the HV `max()` can only tighten from here. **Not "
        "measured**: `Grid3D::pairwise_required_clearance` "
        "(`grid.cpp:751`) is dormant until `set_pairwise_domains` installs a "
        "domain matrix, so it would return a meaningless 0.0 -- it needs an HV "
        "corpus (Phase 2)."
    ),
    15: (
        "`VectorCollisionChecker`, which delegates to `GridCollisionChecker` "
        "when the per-layer R-tree is unpopulated, so both citations are "
        "exercised by this row. `ignore_overflow` left at its stricter default."
    ),
    16: (
        "**Not measured**: `_post_insertion_clearance_detail_pair_group` "
        "(`match_group_tuning.py:2404`) needs a mirrored diff-pair candidate "
        "with declared P/N net ids, which the generator does not place. Group "
        "8's row does not close this: it drives the coupled *constructor's* "
        "gates over ordinary pairs, so the missing ingredient is a corpus pair "
        "kind, not a live `DiffPairRouter`."
    ),
    17: (
        "The destination gate's foreign-via clearance check. **Not measured**: "
        "`_via_drill_overlaps_bbox` (`:1173`) is an overlap detector with no "
        "clearance term (every corpus pair has a positive gap, so it would be "
        "a meaningless zero), and `_via_edge_sweep_clear` (`:2204`) is a "
        "displacement certificate against the board outline needing a "
        "before/after position pair and a copper-to-edge pair kind."
    ),
    18: (
        "Exact polygon pad model -- the other half of `roundrect-corner-gap`. "
        "One scalar `min_clearance_mm` for every pair, so it cannot reproduce "
        "groups 12/13's order asymmetry. **Not measured**: "
        "`SegmentZoneClearanceRule` / `ViaZoneClearanceRule` / "
        "`physical_gap.py` / `EdgeClearanceRule` need a zone and an edge pair "
        "kind on refilled runs, which the generator does not place -- tracked "
        "as #5644."
    ),
    19: (
        "Every pad is a disc of `max(w, h) / 2` "
        "(`drc/cpp_backend.py:96`), which can over-reject but never "
        "under-reject. **Not measured**: the corpus probes the one axis where "
        "that envelope is tight (each probe shape's long side is its local X "
        "side, so `max(w, h) / 2` is the true support there) -- quantifying "
        "the off-axis over-approximation needs a short-side pad pair kind "
        "(Phase 4)."
    ),
    KERNEL_GROUP: (
        "Control row, not a consumer group: both ports (`clearance_kernel.py` "
        "and `router_cpp`) driven, flagged when either flags. `required_mm` is "
        "the `.kicad_pro` `Default` netclass value kicad-cli applies, because "
        "the kernel carries no rule values of its own -- Phase 2's resolver "
        "owns that. Any non-zero cell is a Phase 1b bug: capture a fixture and "
        "fix it through `test_clearance_kernel_parity.py`, never here."
    ),
}


def _adapter_available(adapter: ConsumerAdapter) -> bool:
    """Whether ``adapter`` can run here (optional protocol member)."""
    probe = getattr(adapter, "available", None)
    return True if probe is None else bool(probe())


def _adapter_pair_kinds(adapter: ConsumerAdapter) -> frozenset[str] | None:
    """The pair kinds ``adapter`` is in scope for, or ``None`` for all of them."""
    kinds = getattr(adapter, "pair_kinds", None)
    return None if kinds is None else frozenset(kinds)


@dataclass(frozen=True)
class CorpusStats:
    """What was actually exercised, for the document's provenance lines."""

    seed_range: str
    case_count: int = 0
    pair_count: int = 0
    boundary_count: int = 0
    flagged_pair_count: int = 0
    fill_states: tuple[str, ...] = ()
    oracle_runs: int = 0
    unmapped_rows: tuple[str, ...] = ()

    @property
    def corpus_cell(self) -> str:
        return f"{self.case_count} cases (seeds {self.seed_range})"


@dataclass(frozen=True)
class AdapterMeasurement:
    """One adapter's disagreement rates against kicad-cli."""

    group: int
    adapter_name: str
    pairs_compared: int = 0
    over_reject: int = 0
    under_reject: int = 0
    boundary: int = 0
    fill_states: tuple[str, ...] = ()

    def _pct(self, count: int) -> str:
        if self.pairs_compared == 0:
            return NOT_MEASURED
        return f"{100.0 * count / self.pairs_compared:.1f}% ({count}/{self.pairs_compared})"

    @property
    def over_reject_cell(self) -> str:
        return self._pct(self.over_reject)

    @property
    def under_reject_cell(self) -> str:
        return self._pct(self.under_reject)


TABLE_COLUMNS: tuple[str, ...] = (
    "adapter",
    "corpus (seed range)",
    "over-reject %",
    "under-reject %",
    "boundary",
    "fill state",
    "notes",
)
"""The table's columns, in order.

The ``notes`` column is what makes the "every row is measured *or* states its
reason" criterion checkable rather than aspirational: it carries
:data:`NOT_MEASURED_REASONS` for an unmeasured group and :data:`NOTES` -- the
sub-entry-points a row does not cover -- for a measured one.
"""


def adapter_for_group(number: int) -> ConsumerAdapter | None:
    """The adapter registered for a group, if any."""
    for adapter in ADAPTERS:
        if adapter.group == number:
            return adapter
    return None


def not_measured_reason(number: int) -> str:
    """Why group ``number`` has no measurement, always a stated reason.

    Three cases, in order.  A group with **no registered adapter** must carry
    an explicit :data:`NOT_MEASURED_REASONS` entry; that is a static
    invariant, asserted by
    ``test_report_shape.test_every_unwired_group_states_its_reason`` rather
    than only at render time, so the gap is caught by a unit test on a laptop
    with no KiCad installed.  A group that *has* an adapter but is still
    unmeasured was skipped at runtime because ``available()`` said no -- a
    machine fact, not a coverage decision, and reported as such.  The final
    fallback exists only so rendering can never crash mid-document; reaching
    it means the invariant test is missing a case.
    """
    explicit = NOT_MEASURED_REASONS.get(number)
    if explicit is not None:
        return explicit
    adapter = adapter_for_group(number)
    if adapter is not None:
        return (
            f"adapter `{adapter.name}` is wired but could not run on the "
            "machine that generated this document (most often: the compiled "
            "extension is not built -- `uv run kct build-native`). A skipped "
            "adapter deliberately renders `not measured` rather than a "
            "zero-disagreement row."
        )
    return "no adapter registered and no reason recorded -- this is a harness bug."


def _escape_cell(text: str) -> str:
    """Make a note safe inside a markdown table cell.

    Only ``|`` needs handling: it would end the cell.  Newlines cannot occur
    (every note is a single string literal), and asserting that keeps a future
    multi-line note from silently corrupting the table.
    """
    assert "\n" not in text, f"table notes must be single-line: {text!r}"
    return text.replace("|", "\\|")


def _rows(
    stats: CorpusStats,
    measurements: dict[int, AdapterMeasurement],
) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for group in GROUPS:
        m = measurements.get(group.number)
        if m is None:
            reason = not_measured_reason(group.number)
            rows.append(
                (
                    f"{group.number}. {group.label} — *(no adapter)*",
                    NOT_MEASURED,
                    NOT_MEASURED,
                    NOT_MEASURED,
                    NOT_MEASURED,
                    NOT_MEASURED,
                    _escape_cell(reason),
                )
            )
            continue
        rows.append(
            (
                f"{group.number}. {group.label} — `{m.adapter_name}`",
                stats.corpus_cell,
                m.over_reject_cell,
                m.under_reject_cell,
                str(m.boundary),
                ", ".join(m.fill_states) or NOT_MEASURED,
                _escape_cell(NOTES.get(group.number, "")),
            )
        )
    return rows


def _kernel_row(
    stats: CorpusStats,
    measurements: dict[int, AdapterMeasurement],
) -> tuple[str, ...] | None:
    """The Phase 1b kernel's row, or ``None`` when it was not measured."""
    m = measurements.get(KERNEL_GROUP)
    if m is None:
        return None
    return (
        f"Phase 1b clearance kernel — `{m.adapter_name}`",
        stats.corpus_cell,
        m.over_reject_cell,
        m.under_reject_cell,
        str(m.boundary),
        ", ".join(m.fill_states) or NOT_MEASURED,
        _escape_cell(NOTES[KERNEL_GROUP]),
    )


def _markdown_table(rows: list[tuple[str, ...]]) -> list[str]:
    header = "| " + " | ".join(TABLE_COLUMNS) + " |"
    sep = "|" + "---|" * len(TABLE_COLUMNS)
    body = ["| " + " | ".join(row) + " |" for row in rows]
    for row in rows:
        assert len(row) == len(TABLE_COLUMNS), f"row has {len(row)} cells: {row}"
    return [header, sep, *body]


def render_table(
    stats: CorpusStats,
    measurements: dict[int, AdapterMeasurement] | None = None,
) -> str:
    """Render the 19-row consumer-group table (delimited by begin/end markers).

    The kernel is deliberately **not** in here -- it is not one of the epic's
    nineteen consumer groups, and mixing it in would make the row count stop
    being a checkable invariant.  :func:`render_kernel_table` renders it.

    Args:
        stats: Corpus provenance for the measured rows.
        measurements: Per-group adapter results; groups absent from the map
            render as ``not measured`` plus their stated reason.

    Returns:
        Markdown, including the ``TABLE_BEGIN`` / ``TABLE_END`` markers that
        let the doc test count rows without parsing prose.
    """
    measurements = measurements or {}
    rows = _rows(stats, measurements)
    assert len(rows) == len(GROUPS)
    return "\n".join([TABLE_BEGIN, *_markdown_table(rows), TABLE_END])


def render_kernel_table(
    stats: CorpusStats,
    measurements: dict[int, AdapterMeasurement] | None = None,
) -> str:
    """Render the kernel's one-row table, or a ``not measured`` note."""
    row = _kernel_row(stats, measurements or {})
    if row is None:
        return (
            f"The kernel row is `{NOT_MEASURED}`: its adapter could not run on "
            "the machine that generated this document."
        )
    return "\n".join([KERNEL_TABLE_BEGIN, *_markdown_table([row]), KERNEL_TABLE_END])


def render_document(
    stats: CorpusStats,
    measurements: dict[int, AdapterMeasurement] | None = None,
) -> str:
    """Render the full ``docs/clearance-conformance.md`` body."""
    measurements = measurements or {}
    measured = sorted(g for g in measurements if g != KERNEL_GROUP)
    unmeasured = [g.number for g in GROUPS if g.number not in measurements]

    lines: list[str] = [
        "# Clearance conformance: kicad-cli vs. in-tree consumers",
        "",
        "<!-- Generated by `uv run python -m tests.conformance.report`. Do not edit by hand. -->",
        "",
        "Epic [#5509](https://github.com/rjwalters/kicad-tools/issues/5509) "
        "(*one clearance kernel*) starts from a measurement, not a refactor: "
        "**how far does each of our clearance models sit from "
        "`kicad-cli pcb drc`?** This document is that measurement.",
        "",
        "`kicad-cli` is ground truth here. Every row below compares one "
        "in-tree consumer's answer -- *is this pair of copper objects too "
        "close?* -- against KiCad's answer on the same board, for a corpus of "
        "seeded random copper configurations plus four named fixtures "
        "reproducing known disagreements.",
        "",
        "**This document is report-only.** A disagreement here is evidence, "
        "not a bug report and not a patch: consumers are switched to the "
        "shared kernel in their own epic phase, and only then do their rows "
        "become a merge gate.",
        "",
        "## Status",
        "",
        *(
            [
                "Phase 1a has landed the **truth side** of the harness: the "
                "generator, the board writer, the kicad-cli oracle, the four "
                "named fixtures, and this document's shape. No adapter wraps "
                "a consumer yet, so every group row below reads "
                f"`{NOT_MEASURED}` -- that is the expected state for this "
                "phase, not a gap in the measurement. The corpus figures "
                "further down are real: the whole pipeline ran, it just had "
                "nothing to compare kicad-cli against.",
                "",
                "The adapter phase populates `ADAPTERS` in "
                "`tests/conformance/report.py` and regenerates this table; "
                "nothing else about the document changes.",
            ]
            if not measured
            else [
                f"{len(measured)} of {len(GROUPS)} consumer groups are wrapped "
                "by an adapter and measured below "
                f"(groups {', '.join(str(g) for g in measured)}). The rest "
                f"read `{NOT_MEASURED}`.",
                "",
                "Each adapter drives an **unmodified** consumer: no rule "
                "value, heuristic or consumer was changed to produce these "
                "numbers. Two things shape the denominators and are worth "
                "reading before the percentages. First, a consumer is only "
                "compared on the pair kinds it is actually consulted for in "
                "production (`ConsumerAdapter.pair_kinds`) -- the "
                "route-halo refinement, for instance, never sees pad copper, "
                "so pad pairs are out of its scope rather than counted "
                "against it. Second, the commit gates are measured under one "
                "named insertion order, **via-first**; #5398 is the "
                "observation that the other order gives a different answer "
                "for the same two objects, and the "
                "`issue5398-seg-via-0p18-order` fixture is where that is "
                "recorded.",
                "",
                "Every group that is still `not measured` carries its reason "
                "in the `notes` column, and so does every group that *is* "
                "measured but whose entry-point list this phase could not "
                'cover in full. A bare "not measured" reads the same as '
                '"nobody looked"; naming the gap is what keeps the next phase '
                "honest about what it inherits.",
            ]
        ),
        "",
        "## How to read the table",
        "",
        "- **over-reject** -- the consumer rejects a pair kicad-cli finds "
        "clean. This is the failure mode that makes legal candidates look "
        "unroutable and burns A* expansions (#5410).",
        "- **under-reject** -- the consumer accepts a pair kicad-cli flags. "
        'This is the failure mode that makes a route "complete" and then '
        "fail the final DRC gate (#5398, #3803).",
        "- **boundary** -- pairs whose gap is within "
        f"{BOUNDARY_BAND_MM * 1000:.0f} um of the requirement. Counted "
        "separately and never as a disagreement: at that distance the two "
        "models are arguing about rounding, not about geometry (kct's "
        "`CLEARANCE_EPSILON_MM` is 1e-4 mm; KiCad works in integer "
        "nanometres).",
        "- **fill state** -- whether zones were refilled "
        "(`kicad-cli pcb drc --refill-zones`) before measuring. Zone verdicts "
        "are only meaningful against fresh fills, so they are dropped from "
        "unrefilled runs.",
        "- **not measured** -- no adapter wraps that consumer yet. The row is "
        'kept rather than omitted: an omitted row reads as "agrees", and we '
        "do not know that. The `notes` column says *why* in every case.",
        "- **notes** -- for a measured row, the sub-entry-points the row does "
        "**not** cover, so the percentage cannot imply more coverage than it "
        "has. For an unmeasured row, the reason.",
        "",
        "## Conformance by consumer group",
        "",
        f"Groups are Epic #5509 section 1's inventory of implementations that "
        f"carry their own clearance arithmetic ({len(GROUPS)} of them, across "
        "two native extensions and Python).",
        "",
        render_table(stats, measurements),
        "",
        "## The kernel, measured as a consumer",
        "",
        "The row the rest of the table is *for*. Epic #5509's plan is to move "
        "every consumer above onto one exact-geometry kernel "
        "(`router/clearance_kernel.py` plus its `router_cpp` twin, Phase 1b), "
        "and that plan is only sound if the kernel itself can agree with "
        "`kicad-cli` everywhere. So the kernel is measured by the same harness, "
        "on the same corpus, and its acceptance criterion is a hard "
        "**0 % / 0 %**.",
        "",
        "It is not one of the nineteen groups -- it is the model they are to be "
        "unified onto -- which is why it sits in its own table. It is also the "
        "only row whose `required_mm` comes from the `.kicad_pro` netclass "
        "rather than from the consumer's own rule values: the kernel carries no "
        "rule values at all (Phase 2's resolver owns that), so its row isolates "
        "the **geometry** question from the **rule-resolution** question. A "
        "non-zero cell here is a Phase 1b bug -- capture a fixture and fix it "
        "through `tests/router/test_clearance_kernel_parity.py`, never by "
        "loosening this measurement.",
        "",
        render_kernel_table(stats, measurements),
        "",
        "## Corpus",
        "",
        f"- Seed range: `{stats.seed_range}` ({stats.case_count} generated cases)",
        f"- Analytic close pairs placed: {stats.pair_count}",
        f"- Pairs kicad-cli flagged: {stats.flagged_pair_count}",
        f"- Pairs in the boundary band: {stats.boundary_count}",
        f"- Fill states measured: {', '.join(stats.fill_states) or NOT_MEASURED}",
        f"- kicad-cli invocations: {stats.oracle_runs}",
        "",
        "Cases are generated by `tests/conformance/generator.py` with "
        "`random.Random(seed)` -- no `hypothesis` dependency. Each case places "
        "2-6 segments, 1-3 vias and up to 2 pads (rect / circle / oval / "
        "roundrect, rotated), optionally one zone, with **every object on its "
        "own net** so a kicad-cli finding maps onto a pair by net alone. Close "
        "pairs are placed *analytically* at a gap drawn near the requirement, "
        "rather than by sampling random positions and hoping some land near "
        "the threshold -- otherwise the corpus would almost never probe the "
        "boundary and every rate below would be vacuously zero.",
        "",
        "Six pair kinds are placed: `seg-seg`, `seg-via`, `via-via`, "
        "`pad-seg`, `pad-via` and `pad-pad`. The first five each have a "
        "*routing candidate* -- a segment or a via a router could propose -- "
        "and are what the router-side rows are scored on. `pad-pad` has none "
        "(both sides are placement copper) and exists for group 19, the "
        "incremental placement DRC, whose entry point takes two whole "
        "footprints and can answer nothing else.",
        "",
        "This document regenerates byte-identically from its seed range: "
        "`uv run python -m tests.conformance.report --seeds "
        f"{stats.seed_range} --out docs/clearance-conformance.md`. A "
        "generating-commit SHA is deliberately **not** stamped into the "
        "header -- it would make byte-identical regeneration impossible by "
        "construction, and the seed range plus the pinned generator is what "
        "actually makes the numbers reproducible.",
        "",
        "## Named fixtures",
        "",
        "Committed under `tests/fixtures/conformance/` as "
        "`<name>-seed<N>.kicad_pcb` + `.kicad_pro`. Each reproduces a "
        "*specific* observed disagreement; `test_named_fixtures.py` asserts "
        "kicad-cli's verdict on each one.",
        "",
        "| fixture | kicad-cli verdict | expected consumer disagreement |",
        "|---|---|---|",
        "| `issue5398-seg-via-0p18-order` | one `clearance` violation "
        "(required 0.20 mm, actual 0.180 mm) | Python/C++ commit gates "
        "**accept** when the via is inserted first, **reject** when the "
        "segment is (#5398) |",
        "| `issue5410-dqs-n-halo-vs-legal-via` | clean (0.213 mm copper, "
        "0.513 mm drill vs 0.20 / 0.50) | grid occupancy **rejects** -- "
        "six-cell Chebyshev square halo on a 0.127 mm grid (#5410) |",
        "| `search-vs-commit-seg-via-max` | clean (project `Default` class "
        "0.15 mm) | route-halo geometry **rejects** via "
        "`max(required, via_clearance)`; commit gates **accept** |",
        "| `roundrect-corner-gap` | clean (0.22 mm to the exact outline) | "
        "the router's rectangle-bounded pad model **rejects** (0.1164 mm to "
        "the bounding rectangle); `kct check`'s polygon model **accepts** |",
        "",
        "## Scope and known gaps",
        "",
        f"- Adapters wired: {', '.join(str(g) for g in measured) or 'none yet'}.",
        f"- Groups still `not measured`: {', '.join(str(g) for g in unmeasured) or 'none'}.",
        "- Same-net drill spacing (`validate_same_net_drill_spacing`) is out "
        "of scope: pair identity here is by net, so a same-net finding has no "
        "two-net key. It needs its own harness.",
        "- Verdict identity is `(kind, {net_a, net_b})` only. Gaps and "
        "required values are recorded for this table but never compared "
        "across models -- each model computes its own gap, and comparing "
        "those would measure arithmetic noise instead of the question under "
        "test (*did this model flag this pair at all?*).",
        "- `shorting_items` rows are dropped: that is a connectivity "
        "conclusion about copper that already overlaps, not a clearance "
        "measurement, and no adapter models it.",
        "",
    ]
    if stats.unmapped_rows:
        lines.extend(
            [
                "### Unmapped kicad-cli rows",
                "",
                "Clearance-family findings that could not be reduced to a net "
                "pair. These indicate a harness gap, not a consumer "
                "disagreement:",
                "",
                *[f"- `{row}`" for row in stats.unmapped_rows],
                "",
            ]
        )
    return "\n".join(lines)


def _case_pair_stats(case: CopperCase) -> tuple[int, int]:
    return len(case.pairs), len(case.boundary_pairs)


def measure_corpus(
    seeds: range,
    *,
    fill_states: tuple[bool, ...] = (False, True),
    work_dir: Path | None = None,
) -> tuple[CorpusStats, dict[int, AdapterMeasurement]]:
    """Generate, write and measure the corpus; return stats + adapter results.

    With no adapters registered (Phase 1a) this still does real work: it
    exercises the generator, the board writer and the oracle across the whole
    seed range, so the document's corpus provenance is measured rather than
    asserted.

    Raises:
        KiCadCliAbsent: When kicad-cli is not installed.
    """
    if not kicad_cli_available():
        raise KiCadCliAbsent()

    owns_tmp = work_dir is None
    tmp = tempfile.TemporaryDirectory(prefix="conformance_report_") if owns_tmp else None
    directory = Path(tmp.name) if tmp is not None else Path(work_dir)  # type: ignore[arg-type]

    try:
        cases = generate_corpus(seeds)
        pair_count = 0
        boundary_count = 0
        flagged = 0
        runs = 0
        unmapped: list[str] = []
        results: list[tuple[CopperCase, OracleResult]] = []

        for case in cases:
            pairs, boundary = _case_pair_stats(case)
            pair_count += pairs
            boundary_count += boundary
            board = write_case(case, directory)
            for refill in fill_states:
                result = run_oracle(board.pcb_path, refill=refill, work_dir=directory)
                runs += 1
                unmapped.extend(result.dropped)
                results.append((case, result))
                if not refill:
                    flagged += len(result.without_zones())

        state_names = tuple("refilled" if refill else "as-is" for refill in fill_states)
        stats = CorpusStats(
            seed_range=f"{seeds.start}-{seeds.stop - 1}",
            case_count=len(cases),
            pair_count=pair_count,
            boundary_count=boundary_count,
            flagged_pair_count=flagged,
            fill_states=state_names,
            oracle_runs=runs,
            unmapped_rows=tuple(dict.fromkeys(unmapped)),
        )

        measurements: dict[int, AdapterMeasurement] = {}
        for adapter in ADAPTERS:
            if not _adapter_available(adapter):
                # No row rather than a zero-disagreement row: the same rule the
                # oracle applies to a missing kicad-cli.  A group whose adapter
                # could not run renders ``not measured``.
                continue
            kinds = _adapter_pair_kinds(adapter)
            over = under = boundary_hits = 0
            compared = 0
            # An adapter never sees zone fill, so its answer is the same at
            # both fill states; measure once per case and reuse it for each.
            verdict_cache: dict[str, set[frozenset[str]]] = {}
            for case, result in results:
                truth = {v.nets for v in result.without_zones()}
                consumer = verdict_cache.get(case.name)
                if consumer is None:
                    consumer = {v.nets for v in adapter.verdicts(case)}
                    verdict_cache[case.name] = consumer
                for pair in case.pairs:
                    if kinds is not None and pair.kind not in kinds:
                        # Out of this consumer's declared scope -- see
                        # ``ConsumerAdapter.pair_kinds``.  Counting it would
                        # record the absence of a check elsewhere in the
                        # pipeline, not a disagreement in this model.
                        continue
                    if pair.boundary:
                        boundary_hits += 1
                        continue
                    compared += 1
                    in_truth = pair.nets in truth
                    in_consumer = pair.nets in consumer
                    if in_consumer and not in_truth:
                        over += 1
                    elif in_truth and not in_consumer:
                        under += 1
            measurements[adapter.group] = AdapterMeasurement(
                group=adapter.group,
                adapter_name=adapter.name,
                pairs_compared=compared,
                over_reject=over,
                under_reject=under,
                boundary=boundary_hits,
                fill_states=state_names,
            )
        return stats, measurements
    finally:
        if tmp is not None:
            tmp.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate docs/clearance-conformance.md from a measured corpus."
    )
    parser.add_argument(
        "--seeds",
        default="0-23",
        help="Inclusive seed range, e.g. '0-199' (default: 0-23).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DOC_PATH,
        help=f"Output path (default: {DOC_PATH}).",
    )
    parser.add_argument(
        "--fill",
        choices=("both", "as-is", "refilled"),
        default="both",
        help="Fill states to measure (default: both).",
    )
    args = parser.parse_args(argv)

    fill_states = {
        "both": (False, True),
        "as-is": (False,),
        "refilled": (True,),
    }[args.fill]

    try:
        stats, measurements = measure_corpus(parse_seed_range(args.seeds), fill_states=fill_states)
    except KiCadCliAbsent as exc:
        # Refuse to emit a document. Without kicad-cli every disagreement rate
        # would read zero, which is a confident wrong answer.
        print(f"{REASON_ABSENT}: {exc}", file=sys.stderr)
        print("refusing to write a conformance table without ground truth", file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_document(stats, measurements), encoding="utf-8")
    print(f"wrote {args.out} ({len(GROUPS)} group rows, {stats.case_count} cases)")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
