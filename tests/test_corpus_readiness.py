"""Tests for the corpus benchmark-readiness census (issue #4830, slice 4).

``scripts/corpus/benchmark_readiness.py`` decides which corpus boards can serve
as capacity-predictor calibration examples (#4799) and which can serve as
route-vs-human benchmark cases. It is offline by construction -- it reads
payloads that ``check_manifest.py`` already cached, or any local
``.kicad_pcb`` -- so, like the slice-2/3 pure modules, the test suite may import
it without violating the "no network in pytest" rule.

Everything below runs on synthetic boards written to ``tmp_path``. No corpus
payload is vendored or fetched.

The properties worth pinning are the ones whose breakage would silently corrupt
a benchmark verdict rather than crash:

* a **pour** counts as copper (a ground net served only by a filled zone is
  routed; scoring it unrouted mis-grades every board that has a plane), while a
  **keepout rule area** does not (it is a constraint, not a conductor);
* an empty component graph is attributed to the legacy ``(module ...)`` schema
  when that is the cause, because that blocker is fixable in the parser and the
  generic "no pads" bucket hides how much of the corpus it would unlock;
* a partially-routed board is disqualified as a *reference*, not silently scored
  against.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "scripts" / "corpus"

# The corpus tooling lives under scripts/, not in the installed package (same
# convention as tests/test_corpus_manifest.py).
sys.path.insert(0, str(CORPUS_DIR))

from benchmark_readiness import (  # noqa: E402  (sys.path manipulation above)
    BLOCK_LEGACY_MODULE_SCHEMA,
    BLOCK_NO_NET_BINDING,
    BLOCK_NO_OUTLINE,
    BLOCK_NO_PAD_GRAPH,
    BLOCK_NO_REFERENCE_COPPER,
    BLOCK_OUTLINE_NOT_POLYGONAL,
    BLOCK_PARTIAL_REFERENCE_ROUTING,
    LABEL_COMPLETE,
    LABEL_NONE,
    LABEL_PARTIAL,
    LABEL_UNROUTED,
    BoardFeatures,
    census,
    collect_manifest_boards,
    evaluate,
    features_for_file,
    render_census,
)

OUTLINE = "\n".join(
    f'  (gr_line (start {x1} {y1}) (end {x2} {y2}) (layer "Edge.Cuts") (width 0.05))'
    for x1, y1, x2, y2 in [(0, 0, 20, 0), (20, 0, 20, 10), (20, 10, 0, 10), (0, 10, 0, 0)]
)

# A rounded rectangle: 4 straight edges joined by 4 corner arcs. Same bounding
# box as OUTLINE, but ``get_board_outline()`` only chains the straight
# segments, so it yields 2 points -- the shape that made ``kct route`` die
# inside shapely on corpus board os-0003340-pcb0 ("A linearring requires at
# least 4 coordinates"). Reproduced here by hand: it is a library gap, not a
# property of any one corpus file.
ROUNDED_OUTLINE = "\n".join(
    [
        '  (gr_line (start 2 0) (end 18 0) (layer "Edge.Cuts") (width 0.05))',
        '  (gr_line (start 20 2) (end 20 8) (layer "Edge.Cuts") (width 0.05))',
        '  (gr_line (start 18 10) (end 2 10) (layer "Edge.Cuts") (width 0.05))',
        '  (gr_line (start 0 8) (end 0 2) (layer "Edge.Cuts") (width 0.05))',
        '  (gr_arc (start 2 2) (mid 0.6 0.6) (end 2 0) (layer "Edge.Cuts") (width 0.05))',
        '  (gr_arc (start 18 2) (mid 19.4 0.6) (end 20 2) (layer "Edge.Cuts") (width 0.05))',
        '  (gr_arc (start 18 8) (mid 19.4 9.4) (end 18 10) (layer "Edge.Cuts") (width 0.05))',
        '  (gr_arc (start 2 8) (mid 0.6 9.4) (end 0 8) (layer "Edge.Cuts") (width 0.05))',
    ]
)

GND_POUR = """  (zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5)
    (connect_pads (clearance 0.5))
    (min_thickness 0.25)
    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 10) (xy 0 10)))
  )"""

KEEPOUT = """  (zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5)
    (keepout (tracks not_allowed) (vias not_allowed) (pads allowed)
      (copperpour not_allowed) (footprints allowed))
    (polygon (pts (xy 1 1) (xy 5 1) (xy 5 5) (xy 1 5)))
  )"""

SIG_TRACE = '  (segment (start 5.8 5) (end 11.2 5) (width 0.25) (layer "F.Cu") (net 2))'
GND_TRACE = '  (segment (start 4.2 5) (end 12.8 5) (width 0.25) (layer "B.Cu") (net 1))'


def _footprint(ref: str, x: float, gnd_pad: str, sig_pad: str) -> str:
    return f"""  (footprint "R_0603" (layer "F.Cu") (at {x} 5)
    (fp_text reference "{ref}" (at 0 0) (layer "F.SilkS"))
    (pad "{gnd_pad}" smd rect (at -0.8 0) (size 0.9 0.8)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "{sig_pad}" smd rect (at 0.8 0) (size 0.9 0.8)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "SIG"))
  )"""


def board_text(
    *,
    outline: str = "rect",  # "rect" | "rounded" | "none"
    pads: bool = True,
    nets: bool = True,
    copper: str = "",
) -> str:
    """A minimal but genuinely parseable two-resistor board.

    Two footprints share a GND net and a SIG net, so both nets are multi-pad
    (i.e. routable) -- which is what every gate in the module keys off.
    """
    body = [
        "(kicad_pcb (version 20221018) (generator pcbnew)",
        "  (general (thickness 1.6))",
        "  (paper A4)",
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))',
        '  (net 0 "")',
        '  (net 1 "GND")',
        '  (net 2 "SIG")',
    ]
    if outline == "rect":
        body.append(OUTLINE)
    elif outline == "rounded":
        body.append(ROUNDED_OUTLINE)
    if pads:
        if nets:
            body.append(_footprint("R1", 5, "1", "2"))
            body.append(_footprint("R2", 12, "2", "1"))
        else:
            # Same components, no net bindings: a mechanical/panel board.
            body.append(
                _footprint("R1", 5, "1", "2")
                .replace('(net 1 "GND")', "")
                .replace('(net 2 "SIG")', "")
            )
    if copper:
        body.append(copper)
    body.append(")")
    return "\n".join(body) + "\n"


LEGACY_MODULE_BOARD = """(kicad_pcb (version 4) (host pcbnew "(2017-11-30)")
  (general (thickness 1.6))
  (page A4)
  (layers (0 F.Cu signal) (31 B.Cu signal) (44 Edge.Cuts user))
  (net 0 "")
  (net 1 GND)
  (gr_line (start 0 0) (end 20 0) (layer Edge.Cuts) (width 0.05))
  (gr_line (start 20 0) (end 20 10) (layer Edge.Cuts) (width 0.05))
  (gr_line (start 20 10) (end 0 10) (layer Edge.Cuts) (width 0.05))
  (gr_line (start 0 10) (end 0 0) (layer Edge.Cuts) (width 0.05))
  (module R_0603 (layer F.Cu) (tedit 0) (at 5 5)
    (fp_text reference R1 (at 0 0) (layer F.SilkS))
    (pad 1 smd rect (at -0.8 0) (size 0.9 0.8) (layers F.Cu F.Paste F.Mask) (net 1 GND))
  )
  (segment (start 4.2 5) (end 12.8 5) (width 0.25) (layer F.Cu) (net 1))
)
"""


def write_board(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(text, encoding="utf-8")
    return path


def features(tmp_path: Path, name: str, text: str) -> BoardFeatures:
    return features_for_file(write_board(tmp_path, name, text), board_id=name)


class TestFeatureExtraction:
    def test_counts_the_component_and_net_graph(self, tmp_path: Path) -> None:
        f = features(tmp_path, "complete", board_text(copper=f"{SIG_TRACE}\n{GND_TRACE}"))
        assert (f.footprints, f.pads, f.netted_pads) == (2, 4, 4)
        assert f.multi_pad_nets == 2  # GND and SIG each span both parts
        assert f.max_net_degree == 2
        assert f.copper_layers == 2
        assert f.board_mm == [20.0, 10.0]
        assert f.area_cm2 == pytest.approx(2.0)
        assert f.pad_density_per_cm2 == pytest.approx(2.0)
        assert f.avg_pads_per_net == pytest.approx(2.0)
        assert f.trace_length_mm > 0.0

    def test_a_pour_counts_as_copper_but_a_keepout_does_not(self, tmp_path: Path) -> None:
        pour = features(tmp_path, "pour", board_text(copper=f"{SIG_TRACE}\n{GND_POUR}"))
        assert pour.pour_zones_with_net == 1
        assert pour.multi_pad_nets_with_copper == 2
        assert pour.multi_pad_nets_with_trace_copper == 1
        assert pour.pour_served_nets == 1
        assert pour.routed_net_fraction == pytest.approx(1.0)

        keepout = features(tmp_path, "keepout", board_text(copper=f"{SIG_TRACE}\n{KEEPOUT}"))
        assert keepout.pour_zones_with_net == 0, "a rule area is a constraint, not a conductor"
        assert keepout.multi_pad_nets_with_copper == 1
        assert keepout.routed_net_fraction == pytest.approx(0.5)

    def test_legacy_module_board_parses_but_yields_no_pads(self, tmp_path: Path) -> None:
        f = features(tmp_path, "legacy", LEGACY_MODULE_BOARD)
        assert f.footprints == 0 and f.pads == 0
        assert f.legacy_module_tokens == 1, "the (module ...) token is the diagnosable cause"
        assert f.segments == 1, "copper still parses -- only the component graph is missing"

    def test_missing_outline_yields_zero_area_without_raising(self, tmp_path: Path) -> None:
        f = features(tmp_path, "no-outline", board_text(outline="none", copper=SIG_TRACE))
        assert f.area_cm2 == 0.0
        assert f.outline_points == 0
        assert f.pad_density_per_cm2 == 0.0
        assert f.net_density_per_cm2 == 0.0

    def test_rounded_outline_has_area_but_no_usable_polygon(self, tmp_path: Path) -> None:
        f = features(tmp_path, "rounded", board_text(outline="rounded", copper=SIG_TRACE))
        assert f.area_cm2 == pytest.approx(2.0), "the bounding box is fine"
        assert f.outline_points < 3, "arcs are not chained into the outline polygon"

    def test_to_dict_carries_the_derived_metrics(self, tmp_path: Path) -> None:
        data = features(tmp_path, "derived", board_text(copper=GND_POUR)).to_dict()
        for key in (
            "pad_density_per_cm2",
            "net_density_per_cm2",
            "avg_pads_per_net",
            "routed_net_fraction",
            "pour_served_nets",
        ):
            assert key in data


class TestVerdicts:
    def test_fully_routed_board_is_a_candidate_for_both_capabilities(self, tmp_path: Path) -> None:
        verdict = evaluate(
            features(tmp_path, "complete", board_text(copper=f"{SIG_TRACE}\n{GND_TRACE}"))
        )
        assert verdict.label == LABEL_COMPLETE
        assert verdict.capacity_features and verdict.capacity_labeled
        assert verdict.harness_candidate
        assert verdict.capacity_blockers == [] and verdict.harness_blockers == []

    def test_partial_routing_disqualifies_the_reference_not_the_features(
        self, tmp_path: Path
    ) -> None:
        verdict = evaluate(features(tmp_path, "partial", board_text(copper=SIG_TRACE)))
        assert verdict.label == LABEL_PARTIAL
        assert verdict.capacity_features, "features are still extractable"
        assert not verdict.capacity_labeled
        assert not verdict.harness_candidate
        assert verdict.harness_blockers == [BLOCK_PARTIAL_REFERENCE_ROUTING]

    def test_unrouted_board_is_an_input_not_a_reference(self, tmp_path: Path) -> None:
        verdict = evaluate(features(tmp_path, "bare", board_text()))
        assert verdict.label == LABEL_UNROUTED
        assert verdict.capacity_features and not verdict.capacity_labeled
        assert verdict.harness_blockers == [BLOCK_NO_REFERENCE_COPPER]

    def test_legacy_module_board_names_the_fixable_cause(self, tmp_path: Path) -> None:
        verdict = evaluate(features(tmp_path, "legacy", LEGACY_MODULE_BOARD))
        assert verdict.label == LABEL_NONE
        assert not verdict.capacity_features
        assert verdict.capacity_blockers == [BLOCK_NO_PAD_GRAPH, BLOCK_LEGACY_MODULE_SCHEMA]

    def test_pads_without_nets_are_a_distinct_blocker(self, tmp_path: Path) -> None:
        verdict = evaluate(features(tmp_path, "mech", board_text(nets=False)))
        assert verdict.capacity_blockers == [BLOCK_NO_NET_BINDING]
        assert not verdict.capacity_features

    def test_missing_outline_blocks_featurization(self, tmp_path: Path) -> None:
        verdict = evaluate(
            features(tmp_path, "no-outline", board_text(outline="none", copper=SIG_TRACE))
        )
        assert BLOCK_NO_OUTLINE in verdict.capacity_blockers
        assert not verdict.capacity_features

    def test_rounded_outline_is_a_separate_blocker_from_a_missing_one(self, tmp_path: Path) -> None:
        """The fix is arc chaining, not a missing Edge.Cuts -- keep them apart."""
        verdict = evaluate(
            features(
                tmp_path,
                "rounded",
                board_text(outline="rounded", copper=f"{SIG_TRACE}\n{GND_TRACE}"),
            )
        )
        assert verdict.capacity_blockers == [BLOCK_OUTLINE_NOT_POLYGONAL]
        assert BLOCK_NO_OUTLINE not in verdict.capacity_blockers
        assert not verdict.harness_candidate


class TestCensus:
    def _mixed(self, tmp_path: Path) -> tuple[list[BoardFeatures], list]:
        boards = [
            features(tmp_path, "complete", board_text(copper=f"{SIG_TRACE}\n{GND_TRACE}")),
            features(tmp_path, "pour", board_text(copper=f"{SIG_TRACE}\n{GND_POUR}")),
            features(tmp_path, "partial", board_text(copper=SIG_TRACE)),
            features(tmp_path, "legacy", LEGACY_MODULE_BOARD),
        ]
        return boards, [evaluate(f) for f in boards]

    def test_aggregates_rates_labels_and_blockers(self, tmp_path: Path) -> None:
        boards, verdicts = self._mixed(tmp_path)
        report = census(boards, verdicts, unavailable=[{"board_id": "x", "reason": "not cached"}])

        totals = report["totals"]
        assert totals["boards"] == 4
        assert totals["unavailable"] == 1
        assert totals["capacity_features"] == 3
        assert totals["capacity_labeled"] == 2
        assert totals["harness_candidates"] == 2
        assert totals["harness_candidate_rate"] == pytest.approx(0.5)
        assert totals["pour_dependent_candidates"] == 1

        assert report["by_label"] == {LABEL_COMPLETE: 2, LABEL_PARTIAL: 1, LABEL_NONE: 1}
        assert report["blockers"][BLOCK_LEGACY_MODULE_SCHEMA] == 1
        assert report["blockers"][BLOCK_PARTIAL_REFERENCE_ROUTING] == 1
        assert report["candidate_profile"]["count"] == 2
        assert [b["board_id"] for b in report["boards"]] == sorted(
            b["board_id"] for b in report["boards"]
        )

    def test_empty_input_does_not_divide_by_zero(self) -> None:
        report = census([], [])
        assert report["totals"]["boards"] == 0
        assert report["totals"]["harness_candidate_rate"] is None
        assert report["candidate_profile"] == {}
        assert "corpus benchmark-readiness census" in render_census(report)

    def test_render_names_every_blocker_and_board(self, tmp_path: Path) -> None:
        boards, verdicts = self._mixed(tmp_path)
        text = render_census(census(boards, verdicts))
        assert BLOCK_LEGACY_MODULE_SCHEMA in text
        assert BLOCK_PARTIAL_REFERENCE_ROUTING in text
        for board in boards:
            assert board.board_id in text
        assert "route-vs-human candidates" in text


class TestManifestIntegration:
    def test_uncached_entries_are_unavailable_not_failures(self, tmp_path: Path) -> None:
        """A cold payload cache is an operator state, never a data verdict."""
        boards, unavailable = collect_manifest_boards(
            CORPUS_DIR / "manifests" / "open-schematics-sample.json",
            tmp_path,  # deliberately empty cache dir
        )
        assert boards == []
        assert unavailable, "every PCB entry should be reported as uncached"
        assert all("not cached" in item["reason"] for item in unavailable)


class TestZeroCIImpact:
    def test_not_referenced_from_any_workflow(self) -> None:
        """The corpus tooling stays opt-in and local (issue #4830 constraint)."""
        for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
            assert "benchmark_readiness" not in workflow.read_text(encoding="utf-8"), workflow.name
