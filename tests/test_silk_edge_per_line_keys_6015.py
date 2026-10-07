"""Per-line finding keys for ``silk_edge_clearance`` + re-waive hints (Issue #6015).

``silk_edge_clearance`` used to name every offending stroke of a footprint
``J1 (fp_line)``, so two lines near the edge shared a key *and* an evidence
hash (board 03's last key+hash twin).  It now uses the same footprint-local
per-line naming as ``silkscreen_line_width`` (#5946).  A waiver written
against the old coarse name matches nothing; its ``waiver_unused`` advisory
now names the per-line keys to re-waive.
"""

from __future__ import annotations

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB, Footprint, FootprintGraphic, FootprintText
from kicad_tools.sexp import SExp
from kicad_tools.validate import DRCResults
from kicad_tools.validate.evidence import annotate_evidence
from kicad_tools.validate.rules.waivers import (
    WAIVER_UNUSED_RULE_ID,
    apply_waivers,
    waivers_from_dict,
)

pytest.importorskip("shapely")

from kicad_tools.validate.rules.silkscreen import (  # noqa: E402
    check_silk_edge_clearance,
    check_silkscreen_line_width,
)

# Two vertical strokes hugging the left board edge once the footprint sits at
# x=0: one on the edge, one 0.05 mm inboard (both inside the 0.2 mm floor).
LINE_A = ((0.0, -2.0), (0.0, 2.0))
LINE_B = ((0.05, -2.0), (0.05, 2.0))


def _rules():
    return get_profile("jlcpcb").get_design_rules(layers=2)


def _board(lines, *, position=(0.0, 10.0), stroke_width=0.15, texts=(), gr_lines=()):
    from kicad_tools.schema.pcb import BoardGraphic, GraphicLine

    pcb = PCB(SExp(name="kicad_pcb"))
    for start, end in (
        ((0.0, 0.0), (20.0, 0.0)),
        ((20.0, 0.0), (20.0, 20.0)),
        ((20.0, 20.0), (0.0, 20.0)),
        ((0.0, 20.0), (0.0, 0.0)),
    ):
        pcb._graphic_lines.append(GraphicLine(start=start, end=end, layer="Edge.Cuts", width=0.1))
    for start, end in gr_lines:
        pcb._graphics.append(
            BoardGraphic(
                graphic_type="line", layer="F.SilkS", stroke_width=0.15, start=start, end=end
            )
        )
    pcb._footprints.append(
        Footprint(
            name="TestFP",
            layer="F.Cu",
            position=position,
            rotation=0.0,
            reference="J1",
            value="CONN",
            pads=[],
            texts=list(texts),
            graphics=[
                FootprintGraphic(
                    graphic_type="line",
                    layer="F.SilkS",
                    stroke_width=stroke_width,
                    start=s,
                    end=e,
                )
                for s, e in lines
            ],
        )
    )
    return pcb


def _edge_findings(pcb) -> DRCResults:
    results = check_silk_edge_clearance(pcb, _rules())
    annotate_evidence(results, pcb)
    return results


class TestPerLineEdgeKeys:
    def test_two_lines_on_one_footprint_get_distinct_keys_and_hashes(self):
        results = _edge_findings(_board([LINE_A, LINE_B]))
        assert len(results.violations) == 2
        assert len({v.key for v in results.violations}) == 2
        assert len({v.evidence_hash for v in results.violations}) == 2
        for v in results.violations:
            assert v.items[0].startswith("J1 (fp_line@")
            assert v.items[1] == "Edge.Cuts"
        assert {v.items[0] for v in results.violations} == {
            "J1 (fp_line@0/-2~0/2)",
            "J1 (fp_line@0.05/-2~0.05/2)",
        }

    def test_item_is_footprint_local_so_key_survives_a_move(self):
        before = {v.key for v in _edge_findings(_board([LINE_A])).violations}
        after = {v.key for v in _edge_findings(_board([LINE_A], position=(0.0, 14.0))).violations}
        assert before == after

    def test_two_runs_are_identical(self):
        def run():
            return [
                (v.key, v.evidence_hash)
                for v in _edge_findings(_board([LINE_A, LINE_B])).violations
            ]

        assert run() == run()

    def test_text_item_name_is_unchanged(self):
        text = FootprintText(
            text_type="reference",
            text="J1",
            position=(0.0, 0.0),
            layer="F.SilkS",
            font_size=(1.0, 1.0),
            font_thickness=0.15,
        )
        results = _edge_findings(_board([], texts=[text]))
        assert [v.items for v in results.violations] == [("J1 (reference)", "Edge.Cuts")]

    def test_board_level_line_is_named_by_its_geometry(self):
        results = _edge_findings(_board([], gr_lines=[((0.0, 5.0), (0.0, 8.0))]))
        assert [v.items[0] for v in results.violations] == ["gr_line@0/5~0/8"]

    def test_message_keeps_the_readable_label(self):
        (v,) = _edge_findings(_board([LINE_A])).violations
        assert "Silkscreen J1 (fp_line) to board edge" in v.message


def _unused(results: DRCResults):
    return [v for v in results.violations if v.rule_id == WAIVER_UNUSED_RULE_ID]


class TestLegacyWaiverHint:
    def test_legacy_edge_waiver_points_at_per_line_keys(self):
        results = _edge_findings(_board([LINE_A, LINE_B]))
        new_keys = sorted(v.key for v in results.violations)
        waivers = waivers_from_dict(
            {
                "version": 2,
                "waivers": [
                    {
                        "rule": "silk_edge_clearance",
                        "items": ["J1 (fp_line)", "Edge.Cuts"],
                        "reason": "connector overhang",
                        "issue": "x#1",
                    }
                ],
            }
        )
        apply_waivers(results, waivers)
        (advisory,) = _unused(results)
        assert "kct check --waive KEY" in advisory.message
        for key in new_keys:
            assert repr(key) in advisory.message
        # The legacy entry no longer waives anything: the warnings are back.
        assert not any(v.waived for v in results.violations if v.rule_id == "silk_edge_clearance")

    def test_legacy_line_width_waiver_points_at_per_line_keys(self):
        pcb = _board([LINE_A], stroke_width=0.05)
        results = check_silkscreen_line_width(pcb, _rules())
        annotate_evidence(results, pcb)
        (finding,) = results.violations
        waivers = waivers_from_dict(
            {
                "version": 2,
                "waivers": [
                    {
                        "rule": "silkscreen_line_width",
                        "items": ["J1", "fp_line"],
                        "reason": "vendor footprint",
                        "issue": "x#2",
                    }
                ],
            }
        )
        apply_waivers(results, waivers)
        (advisory,) = _unused(results)
        assert repr(finding.key) in advisory.message

    def test_old_keyed_entry_points_at_per_line_keys(self):
        results = _edge_findings(_board([LINE_A]))
        (finding,) = results.violations
        waivers = waivers_from_dict(
            {
                "version": 3,
                "waivers": [
                    {
                        "key": "silk_edge_clearance|Edge.Cuts,J1 (fp_line)||F.SilkS",
                        "evidence_hash": "ev1:fd835390fab2dbe5",
                        "reason": "r",
                        "reviewer": "ee",
                        "date": "2026-10-06",
                    }
                ],
            }
        )
        apply_waivers(results, waivers)
        (advisory,) = _unused(results)
        assert repr(finding.key) in advisory.message

    def test_unrelated_unused_waiver_gets_no_hint(self):
        results = _edge_findings(_board([LINE_A]))
        waivers = waivers_from_dict(
            {
                "version": 2,
                "waivers": [
                    {
                        "rule": "silk_edge_clearance",
                        "items": ["U9 (fp_line)", "Edge.Cuts"],
                        "reason": "r",
                        "issue": "x#3",
                    }
                ],
            }
        )
        apply_waivers(results, waivers)
        (advisory,) = _unused(results)
        assert "--waive" not in advisory.message

    def test_already_rewaived_finding_is_not_suggested_again(self):
        results = _edge_findings(_board([LINE_A]))
        (finding,) = results.violations
        waivers = waivers_from_dict(
            {
                "version": 3,
                "waivers": [
                    {
                        "key": finding.key,
                        "evidence_hash": finding.evidence_hash,
                        "reason": "r",
                        "reviewer": "ee",
                        "date": "2026-10-06",
                    },
                    {
                        "rule": "silk_edge_clearance",
                        "items": ["J1 (fp_line)", "Edge.Cuts"],
                        "reason": "old",
                        "issue": "x#4",
                    },
                ],
            }
        )
        apply_waivers(results, waivers)
        (advisory,) = _unused(results)
        assert "--waive" not in advisory.message
