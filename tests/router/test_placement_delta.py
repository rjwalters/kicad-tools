"""Unit tests for the classifier -> placement-delta translator (issue #4466).

Phase 1 of the board-07 router<->placement feedback epic (#3438).  The
translator is a pure, read-only function: it maps one classifier
``StuckNetDiagnosis`` onto a single applyable ``PlacementDelta`` (or ``None``),
driven off the top-ranked action and honoring the ladder's deliberate
omissions.  These tests exercise:

* the mapping table directly (constructed diagnoses -- no PCB), and
* the end-to-end path on synthetic boards and the committed board-07 artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.router.placement_delta import (
    MAX_TRANSLATE_MM,
    PlacementDelta,
    delta_from_diagnosis,
    deltas_from_result,
)
from kicad_tools.router.stuck_classifier import (
    BundleOrientation,
    Confidence,
    RankedAction,
    RecommendedAction,
    StuckClass,
    StuckNetDiagnosis,
    classify_stuck_nets_from_pcb,
)
from kicad_tools.schema.pcb import PCB

# ---------------------------------------------------------------------------
# Synthetic boards (self-contained; mirror the stuck-classifier fixtures)
# ---------------------------------------------------------------------------

_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
"""


def _same_group_ring(cx: float, cy: float, radius: float, count: int, gap: int) -> str:
    """`count` DQ0 pads ringing (cx, cy) -- same-match-group siblings of DQ2."""
    import math

    out = []
    slots = count + gap
    for i in range(count):
        ang = 2 * math.pi * i / slots
        px = cx + radius * math.cos(ang)
        py = cy + radius * math.sin(ang)
        out.append(
            f'  (footprint "dq0_{i}" (layer "F.Cu") (at {px:.4f} {py:.4f})\n'
            f'    (property "Reference" "S{i}")\n'
            f'    (pad "1" smd circle (at 0 0) (size 0.2 0.2) '
            f'(layers "F.Cu") (net 2 "DQ0"))\n'
            f"  )\n"
        )
    return "".join(out)


def _facing_rows_bundle_board(*, reversed_rows: bool) -> str:
    """A DDR_DATA bundle whose facing columns UA/UB are co-oriented or reversed.

    The stranded DQ2 pad is crowded by a dense ring of its own DDR siblings, so
    the net classifies PLACEMENT_BOUND with a self-crossing topology; the two
    multi-net columns UA/UB are read as the facing rows by the orientation
    resolver (co-oriented when ``reversed_rows`` is False, fully reversed when
    True).  ``secondary_ref`` resolves to UB (count tie broken on reference asc).
    """
    ua_order = ["DQ0", "DQ1", "DQ2"]
    ub_order = list(reversed(ua_order)) if reversed_rows else ua_order
    net_num = {"DQ2": 1, "DQ0": 2, "DQ1": 3}

    def _column(ref: str, cx: float, order: list[str]) -> str:
        pads = "".join(
            f'    (pad "{i + 1}" smd circle (at 0 {float(i - 1):.1f}) (size 0.2 0.2) '
            f'(layers "F.Cu") (net {net_num[name]} "{name}"))\n'
            for i, name in enumerate(order)
        )
        return (
            f'  (footprint "col_{ref}" (layer "F.Cu") (at {cx:.1f} 50)\n'
            f'    (property "Reference" "{ref}")\n' + pads + "  )\n"
        )

    return _HEADER + (
        '  (net 0 "")\n'
        '  (net 1 "DQ2")\n'
        '  (net 2 "DQ0")\n'
        '  (net 3 "DQ1")\n'
        '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
        '    (property "Reference" "R1")\n'
        '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "DQ2"))\n'
        '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "DQ2"))\n'
        "  )\n"
        '  (footprint "U_SOT" (layer "F.Cu") (at 80 80)\n'
        '    (property "Reference" "U1")\n'
        '    (pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net 1 "DQ2"))\n'
        "  )\n"
        + _same_group_ring(80, 80, 1.5, count=22, gap=2)
        + _column("UA", 30.0, ua_order)
        + _column("UB", 50.0, ub_order)
        + '  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
        ")\n"
    )


def _foreign_ring(cx: float, cy: float, radius: float, count: int, gap: int) -> str:
    """`count` distinct single-pad FOREIGN nets ringing (cx, cy) with an open arc."""
    import math

    out = []
    slots = count + gap
    for i in range(count):
        ang = 2 * math.pi * i / slots
        px = cx + radius * math.cos(ang)
        py = cy + radius * math.sin(ang)
        net = 100 + i
        out.append(
            f'  (net {net} "OBS{i}")\n'
            f'  (footprint "obs{i}" (layer "F.Cu") (at {px:.4f} {py:.4f})\n'
            f'    (property "Reference" "O{i}")\n'
            f'    (pad "1" smd circle (at 0 0) (size 0.2 0.2) '
            f'(layers "F.Cu") (net {net} "OBS{i}"))\n'
            f"  )\n"
        )
    return "".join(out)


def _foreign_cluster_board() -> str:
    """A stranded TGT pad walled by genuinely foreign copper -> MOVE_PART."""
    return _HEADER + (
        '  (net 0 "")\n'
        '  (net 1 "TGT")\n'
        '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
        '    (property "Reference" "R1")\n'
        '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
        '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
        "  )\n"
        '  (footprint "U_SOT" (layer "F.Cu") (at 80 80)\n'
        '    (property "Reference" "U1")\n'
        '    (pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net 1 "TGT"))\n'
        "  )\n"
        + _foreign_ring(80, 80, 1.5, count=10, gap=4)
        + '  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
        ")\n"
    )


def _load(tmp_path: Path, text: str) -> PCB:
    p = tmp_path / "board.kicad_pcb"
    p.write_text(text)
    return PCB.load(str(p))


def _diag(pcb: PCB, net_name: str) -> StuckNetDiagnosis:
    result = classify_stuck_nets_from_pcb(pcb)
    return next(d for d in result.diagnoses if d.net_name == net_name)


# ---------------------------------------------------------------------------
# Mapping-table unit tests (constructed diagnoses; PCB unused on these paths)
# ---------------------------------------------------------------------------


def _make_diag(net_name: str, actions, **kw) -> StuckNetDiagnosis:
    return StuckNetDiagnosis(
        net_name=net_name,
        net_number=1,
        classification=StuckClass.PLACEMENT_BOUND,
        unconnected_pads=["U1-1"],
        recommendation=[
            RankedAction(a, f"rationale for {a.value}", Confidence.MEDIUM) for a in actions
        ],
        **kw,
    )


class TestMappingTable:
    def test_de_reverse_maps_to_rotate_180_on_secondary_ref(self):
        diag = _make_diag(
            "DQ2",
            [RecommendedAction.DE_REVERSE_BUNDLE, RecommendedAction.ACCEPT_PLATEAU],
            bundle_orientation=BundleOrientation(
                verdict="reversed",
                inverted_pairs=3,
                total_pairs=3,
                inversion_fraction=1.0,
                primary_ref="UA",
                secondary_ref="UB",
            ),
        )
        delta = delta_from_diagnosis(None, diag)  # pcb unused for rotate_180
        assert delta is not None
        assert delta.kind == "rotate_180"
        assert delta.target_ref == "UB"
        assert delta.rotation_delta == 180.0
        assert delta.dx == 0.0 and delta.dy == 0.0
        assert delta.source_action == "de_reverse_bundle"
        assert delta.confidence == "medium"

    def test_de_reverse_without_orientation_returns_none(self):
        diag = _make_diag("DQ2", [RecommendedAction.DE_REVERSE_BUNDLE])
        assert delta_from_diagnosis(None, diag) is None

    def test_reorder_pins_maps_to_reorder_kind_rationale_only(self):
        diag = _make_diag(
            "DQ2",
            [RecommendedAction.REORDER_PINS],
            bundle_orientation=BundleOrientation(
                verdict="reversed", primary_ref="UA", secondary_ref="UB"
            ),
        )
        delta = delta_from_diagnosis(None, diag)
        assert delta is not None
        assert delta.kind == "reorder_pins"
        assert delta.target_ref == "UB"
        assert delta.dx == 0.0 and delta.dy == 0.0 and delta.rotation_delta == 0.0
        assert delta.source_action == "reorder_pins"

    def test_accept_plateau_top_returns_none(self):
        diag = _make_diag("N", [RecommendedAction.ACCEPT_PLATEAU])
        assert delta_from_diagnosis(None, diag) is None

    def test_widen_channel_top_returns_none(self):
        diag = _make_diag("N", [RecommendedAction.WIDEN_CHANNEL])
        assert delta_from_diagnosis(None, diag) is None

    def test_empty_recommendation_returns_none(self):
        diag = _make_diag("N", [])
        assert delta_from_diagnosis(None, diag) is None


# ---------------------------------------------------------------------------
# End-to-end tests on synthetic boards
# ---------------------------------------------------------------------------


class TestSyntheticBoards:
    def test_reversed_bundle_emits_rotate_180_on_secondary(self, tmp_path: Path):
        """AC (a): a reversed 3+ member facing-row bundle -> rotate_180 on the
        secondary (reversed) facing part."""
        pcb = _load(tmp_path, _facing_rows_bundle_board(reversed_rows=True))
        diag = _diag(pcb, "DQ2")
        assert diag.classification is StuckClass.PLACEMENT_BOUND
        assert diag.topology == "self_crossing_bundle"
        assert diag.bundle_orientation is not None
        assert diag.bundle_orientation.verdict == "reversed"

        delta = delta_from_diagnosis(pcb, diag)
        assert delta is not None
        assert delta.kind == "rotate_180"
        assert delta.target_ref == diag.bundle_orientation.secondary_ref == "UB"
        assert delta.rotation_delta == 180.0

    def test_co_oriented_bundle_emits_translate_not_rotate(self, tmp_path: Path):
        """AC (b): a co-oriented saturated bundle -> translate (NOT rotate --
        de-reversing a co-oriented bundle would create crossings)."""
        pcb = _load(tmp_path, _facing_rows_bundle_board(reversed_rows=False))
        diag = _diag(pcb, "DQ2")
        assert diag.topology == "co_oriented_bundle"
        assert diag.recommendation[0].action is RecommendedAction.MOVE_PART

        delta = delta_from_diagnosis(pcb, diag)
        assert delta is not None
        assert delta.kind == "translate"
        assert delta.rotation_delta == 0.0
        assert (delta.dx, delta.dy) != (0.0, 0.0)

    def test_foreign_cluster_emits_translate(self, tmp_path: Path):
        """AC (c): a foreign-cluster PLACEMENT_BOUND -> translate."""
        pcb = _load(tmp_path, _foreign_cluster_board())
        diag = _diag(pcb, "TGT")
        assert diag.classification is StuckClass.PLACEMENT_BOUND
        assert diag.topology == "foreign_cluster"

        delta = delta_from_diagnosis(pcb, diag)
        assert delta is not None
        assert delta.kind == "translate"
        assert delta.target_ref  # a concrete crowding component was named
        assert (delta.dx, delta.dy) != (0.0, 0.0)

    def test_translate_step_is_bounded(self, tmp_path: Path):
        """The translate magnitude never exceeds the minimal bound."""
        import math

        pcb = _load(tmp_path, _foreign_cluster_board())
        delta = delta_from_diagnosis(pcb, _diag(pcb, "TGT"))
        assert delta is not None
        assert math.hypot(delta.dx, delta.dy) <= MAX_TRANSLATE_MM + 1e-9


# ---------------------------------------------------------------------------
# Serialization + purity
# ---------------------------------------------------------------------------


class TestSerializationAndPurity:
    def test_to_dict_is_json_serializable(self, tmp_path: Path):
        pcb = _load(tmp_path, _facing_rows_bundle_board(reversed_rows=True))
        delta = delta_from_diagnosis(pcb, _diag(pcb, "DQ2"))
        assert delta is not None
        blob = json.dumps(delta.to_dict())  # must not raise
        loaded = json.loads(blob)
        assert loaded["kind"] == "rotate_180"
        assert loaded["target_ref"] == "UB"
        assert set(loaded.keys()) == {
            "net_name",
            "target_ref",
            "kind",
            "dx",
            "dy",
            "rotation_delta",
            "source_action",
            "rationale",
            "confidence",
        }

    def test_delta_from_diagnosis_does_not_mutate_pcb(self, tmp_path: Path):
        pcb = _load(tmp_path, _foreign_cluster_board())
        before = (len(pcb.footprints), len(pcb.segments), len(pcb.vias))
        fp_positions_before = [fp.position for fp in pcb.footprints]
        delta_from_diagnosis(pcb, _diag(pcb, "TGT"))
        after = (len(pcb.footprints), len(pcb.segments), len(pcb.vias))
        assert before == after
        assert [fp.position for fp in pcb.footprints] == fp_positions_before


# ---------------------------------------------------------------------------
# Committed board-07 acceptance evidence (issue #4466 primary AC)
# ---------------------------------------------------------------------------

_BOARD07_ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "boards"
    / "07-matchgroup-test"
    / "output"
    / "matchgroup_test_routed.kicad_pcb"
)


@pytest.mark.skipif(
    not _BOARD07_ARTIFACT.exists(), reason="board-07 committed artifact not present"
)
class TestBoard07:
    @pytest.fixture(scope="class")
    def pcb(self) -> PCB:
        return PCB.load(str(_BOARD07_ARTIFACT))

    def test_ddr_self_crossing_nets_emit_rotate_180(self, pcb: PCB):
        """The reversed DDR byte (DQ3/DQ4, inversion_fraction ~= 1.0) each emit a
        rotate_180 targeting the reversed facing QFN."""
        result = classify_stuck_nets_from_pcb(pcb)
        ddr = [
            d
            for d in result.diagnoses
            if d.match_group == "DDR_DATA" and d.topology == "self_crossing_bundle"
        ]
        assert ddr, "expected reversed DDR nets on the board-07 artifact"
        for diag in ddr:
            assert diag.bundle_orientation is not None
            assert diag.bundle_orientation.verdict == "reversed"
            assert diag.bundle_orientation.inversion_fraction == pytest.approx(1.0)
            delta = delta_from_diagnosis(pcb, diag)
            assert delta is not None
            assert delta.kind == "rotate_180"
            assert delta.rotation_delta == 180.0
            assert delta.target_ref == diag.bundle_orientation.secondary_ref

    def test_tmds_co_oriented_lanes_emit_translate(self, pcb: PCB):
        """The co-oriented TMDS lanes emit translate, never rotate_180."""
        result = classify_stuck_nets_from_pcb(pcb)
        tmds = [d for d in result.diagnoses if d.net_name.startswith("TMDS_")]
        assert tmds, "expected stuck TMDS nets on the board-07 artifact"
        for diag in tmds:
            assert diag.topology == "co_oriented_bundle"
            delta = delta_from_diagnosis(pcb, diag)
            assert delta is not None
            assert delta.kind == "translate"
            assert delta.rotation_delta == 0.0

    def test_deltas_from_result_covers_all_placement_actions(self, pcb: PCB):
        """Every diagnosis whose top action is MOVE_PART/DE_REVERSE_BUNDLE yields
        a delta; the aggregate emits both kinds and nothing else non-null."""
        result = classify_stuck_nets_from_pcb(pcb)
        deltas = deltas_from_result(pcb, result)
        kinds = {d.kind for d in deltas}
        assert "rotate_180" in kinds
        assert "translate" in kinds
        assert all(isinstance(d, PlacementDelta) for d in deltas)
        # JSON round-trips for the whole batch.
        json.dumps([d.to_dict() for d in deltas])
