"""Render ``docs/clearance-conformance.md`` -- one row per consumer group.

The table's shape is the deliverable, not just its numbers.  Epic #5509
section 1 inventories **nineteen** implementation groups that carry their own
clearance arithmetic; the acceptance criterion is that every one of them
appears, with groups that have no adapter yet marked ``not measured`` rather
than quietly omitted.  An omitted row reads as "fine"; a ``not measured`` row
reads as "unknown", which is the truth and is what keeps later phases honest
about their own coverage.

Phase 1a ships the truth side of the harness, so :data:`ADAPTERS` is
deliberately empty here and every group row renders ``not measured``.  The
corpus columns are still real: the generator, the board writer and the oracle
all run, so the document records the seed range actually exercised, how many
pairs were placed, how many landed in the boundary band, and which fill states
were measured.  The adapter PR populates :data:`ADAPTERS` and the same
renderer fills the rate columns in.

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
    "TABLE_BEGIN",
    "TABLE_END",
    "AdapterMeasurement",
    "CorpusStats",
    "Group",
    "NOT_MEASURED",
    "main",
    "measure_corpus",
    "render_document",
    "render_table",
]

NOT_MEASURED = "not measured"

TABLE_BEGIN = "<!-- clearance-conformance:table:begin -->"
TABLE_END = "<!-- clearance-conformance:table:end -->"

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


# Populated by the adapter PR (Epic #5509 Phase 1b).  Keeping it empty here is
# what makes this module free of any ``import kicad_tools.router`` /
# ``kicad_tools.validate`` -- the truth side of the harness does not depend on
# the code it measures.
ADAPTERS: tuple[ConsumerAdapter, ...] = ()


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


def _rows(
    stats: CorpusStats,
    measurements: dict[int, AdapterMeasurement],
) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for group in GROUPS:
        m = measurements.get(group.number)
        if m is None:
            rows.append(
                (
                    f"{group.number}. {group.label} — *(no adapter)*",
                    NOT_MEASURED,
                    NOT_MEASURED,
                    NOT_MEASURED,
                    NOT_MEASURED,
                    NOT_MEASURED,
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
            )
        )
    return rows


def render_table(
    stats: CorpusStats,
    measurements: dict[int, AdapterMeasurement] | None = None,
) -> str:
    """Render the 19-row conformance table (delimited by begin/end markers).

    Args:
        stats: Corpus provenance for the measured rows.
        measurements: Per-group adapter results; groups absent from the map
            render as ``not measured``.

    Returns:
        Markdown, including the ``TABLE_BEGIN`` / ``TABLE_END`` markers that
        let the doc test count rows without parsing prose.
    """
    measurements = measurements or {}
    header = (
        "| adapter | corpus (seed range) | over-reject % | under-reject % | boundary | fill state |"
    )
    sep = "|---|---|---|---|---|---|"
    body = ["| " + " | ".join(row) + " |" for row in _rows(stats, measurements)]
    assert len(body) == len(GROUPS)
    return "\n".join([TABLE_BEGIN, header, sep, *body, TABLE_END])


def render_document(
    stats: CorpusStats,
    measurements: dict[int, AdapterMeasurement] | None = None,
) -> str:
    """Render the full ``docs/clearance-conformance.md`` body."""
    measurements = measurements or {}
    measured = sorted(measurements)
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
                "by an adapter and measured below. The rest read "
                f"`{NOT_MEASURED}`.",
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
        "do not know that.",
        "",
        "## Conformance by consumer group",
        "",
        f"Groups are Epic #5509 section 1's inventory of implementations that "
        f"carry their own clearance arithmetic ({len(GROUPS)} of them, across "
        "two native extensions and Python).",
        "",
        render_table(stats, measurements),
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
        for adapter in ADAPTERS:  # pragma: no cover - empty until the adapter PR
            over = under = boundary_hits = 0
            compared = 0
            for case, result in results:
                truth = {v.nets for v in result.without_zones()}
                consumer = {v.nets for v in adapter.verdicts(case)}
                for pair in case.pairs:
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
