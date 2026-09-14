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
    ENDPOINT_ALIGN_ROTATIONS,
    ENDPOINT_ALIGN_SOURCE,
    MAX_TRANSLATE_MM,
    PlacementDelta,
    delta_from_diagnosis,
    deltas_from_result,
    endpoint_align_deltas,
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
    def test_de_reverse_maps_to_mirror_on_secondary_ref(self):
        """#4560: DE_REVERSE_BUNDLE proposes a MIRROR (layer flip), replacing
        the earlier rotate_180 -- rotation preserves the pin column's chirality
        and structurally cannot un-reverse it (board-07 CI: routed 25 -> 14)."""
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
        delta = delta_from_diagnosis(None, diag)  # pcb unused for mirror
        assert delta is not None
        assert delta.kind == "mirror"
        assert delta.target_ref == "UB"
        # A mirror is parameterless -- a left/right flip about the anchor.
        assert delta.rotation_delta == 0.0
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
    def test_reversed_bundle_emits_mirror_on_secondary(self, tmp_path: Path):
        """AC (a): a reversed 3+ member facing-row bundle -> mirror on the
        secondary (reversed) facing part (#4560)."""
        pcb = _load(tmp_path, _facing_rows_bundle_board(reversed_rows=True))
        diag = _diag(pcb, "DQ2")
        assert diag.classification is StuckClass.PLACEMENT_BOUND
        assert diag.topology == "self_crossing_bundle"
        assert diag.bundle_orientation is not None
        assert diag.bundle_orientation.verdict == "reversed"

        delta = delta_from_diagnosis(pcb, diag)
        assert delta is not None
        assert delta.kind == "mirror"
        assert delta.target_ref == diag.bundle_orientation.secondary_ref == "UB"
        assert delta.rotation_delta == 0.0

    def test_co_oriented_bundle_emits_translate_not_mirror(self, tmp_path: Path):
        """AC (b): a co-oriented saturated bundle -> translate (NOT mirror --
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
# Endpoint-orientation alignment candidates (issue #4968)
# ---------------------------------------------------------------------------
#
# A deliberately tiny, fast geometric fixture: one stuck net whose two endpoint
# footprints present a horizontal pad ROW (J1) against a vertical pad COLUMN
# (U3), with a foreign cluster around the stranded pad so the ladder's top rung
# is MOVE_PART.  Board-07 stays OPTIONAL integration evidence -- nothing here
# routes anything.

_ALIGN_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (gr_rect (start 0 0) (end 100 100) (stroke (width 0.1) (type solid))
    (layer "Edge.Cuts"))
"""


def _pad_line(
    ref: str,
    cx: float,
    cy: float,
    offsets: list[tuple[float, float]],
    net_pads: int,
    *,
    locked: bool = False,
) -> str:
    """A footprint whose pads sit at ``offsets`` (local mm) around ``(cx, cy)``.

    The first ``net_pads`` pads carry net 1 ("TGT"); the rest are unassigned so
    they still shape the pad-array axis without joining the stuck net.
    """
    pads = "".join(
        f'    (pad "{i + 1}" smd rect (at {ox:.2f} {oy:.2f}) (size 0.3 0.3) '
        f'(layers "F.Cu") '
        + (f'(net 1 "TGT"))\n' if i < net_pads else '(net 0 ""))\n')
        for i, (ox, oy) in enumerate(offsets)
    )
    lock = "    (locked yes)\n" if locked else ""
    return (
        f'  (footprint "fp_{ref}" (layer "F.Cu") (at {cx:.2f} {cy:.2f})\n'
        f'    (property "Reference" "{ref}")\n' + lock + pads + "  )\n"
    )


# Pad-offset templates.  The FIRST entry is always the footprint anchor so the
# net-carrying pads land at a predictable place (the sink's TGT pad has to sit
# at the centre of the foreign ring for the ladder to read MOVE_PART).
_ROW = [(0.0, 0.0), (-2.0, 0.0), (-1.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
_COLUMN = [(0.0, 0.0), (0.0, -2.0), (0.0, -1.0), (0.0, 1.0), (0.0, 2.0)]
_SQUARE = [(0.0, 0.0), (-2.0, 0.0), (0.0, -2.0), (2.0, 0.0), (0.0, 2.0)]


def _endpoint_board(
    *,
    source_offsets: list[tuple[float, float]] = _ROW,
    sink_offsets: list[tuple[float, float]] = _COLUMN,
    source_at: tuple[float, float] = (20.0, 50.0),
    source_locked: bool = False,
) -> str:
    """TGT spans J1 (source) and U3 (sink); U3's pad is walled by foreign copper."""
    sx, sy = source_at
    return _ALIGN_HEADER + (
        '  (net 0 "")\n'
        '  (net 1 "TGT")\n'
        + _pad_line("J1", sx, sy, source_offsets, net_pads=2, locked=source_locked)
        + _pad_line("U3", 80.0, 50.0, sink_offsets, net_pads=1)
        + _foreign_ring(80.0, 50.0, 1.5, count=10, gap=4)
        + f'  (segment (start {sx:.2f} {sy:.2f}) (end {sx - 2:.2f} {sy:.2f}) '
        '(width 0.25) (layer "F.Cu") (net 1))\n'
        ")\n"
    )


class TestEndpointAlignment:
    """AC: bounded +/-90 endpoint-orientation candidates for a row/column mismatch."""

    def test_row_vs_column_mismatch_emits_bounded_quarter_turn(self, tmp_path: Path):
        pcb = _load(tmp_path, _endpoint_board())
        diag = _diag(pcb, "TGT")
        assert diag.recommendation[0].action is RecommendedAction.MOVE_PART

        deltas = endpoint_align_deltas(pcb, diag)
        assert deltas, "expected an endpoint-orientation candidate for a row/column mismatch"
        for delta in deltas:
            assert delta.kind == "rotate_align"
            # BOUNDED: exactly the two quarter turns, nothing else.
            assert delta.rotation_delta in ENDPOINT_ALIGN_ROTATIONS
            assert delta.dx == 0.0 and delta.dy == 0.0
            assert delta.source_action == ENDPOINT_ALIGN_SOURCE
            assert delta.net_name == "TGT"
        # At most one candidate per endpoint, and never more than two.
        assert len(deltas) <= 2
        assert len({d.target_ref for d in deltas}) == len(deltas)
        # The smaller-disturbance endpoint (tie -> reference ascending) leads.
        assert deltas[0].target_ref == "J1"

    def test_candidate_carries_auditable_pad_alignment_rationale(self, tmp_path: Path):
        pcb = _load(tmp_path, _endpoint_board())
        delta = endpoint_align_deltas(pcb, _diag(pcb, "TGT"))[0]
        rationale = delta.rationale
        assert "pad-row/column mismatch" in rationale
        assert "J1 pad axis" in rationale and "U3 pad axis" in rationale
        assert "move_part" in rationale  # the ladder rung that triggered the search
        # Connectivity is explicitly NOT a manufacturability verdict (#4968).
        assert "CONNECTIVITY candidate only" in rationale
        assert "skew" in rationale and "coupling" in rationale
        # Never graded "high" -- the proposer measures pad geometry only.
        assert delta.confidence in ("low", "medium")

    def test_candidates_are_appended_after_the_primary_delta(self, tmp_path: Path):
        """The ladder's own proposal still comes first -- #4968 is additive."""
        pcb = _load(tmp_path, _endpoint_board())
        result = classify_stuck_nets_from_pcb(pcb)
        deltas = deltas_from_result(pcb, result)
        kinds = [d.kind for d in deltas]
        assert kinds[0] == "translate"
        assert "rotate_align" in kinds
        assert kinds.index("translate") < kinds.index("rotate_align")
        # Opting out restores the exact pre-#4968 output.
        assert [d.kind for d in deltas_from_result(pcb, result, include_endpoint_alignment=False)] == [
            "translate"
        ]

    def test_rotation_realigns_the_two_pad_axes(self, tmp_path: Path):
        """The proposed quarter turn actually makes the pad arrays parallel."""
        from kicad_tools.router.placement_delta import (
            _axis_separation_deg,
            _pad_axis,
            _rotate_about,
        )

        pcb = _load(tmp_path, _endpoint_board())
        delta = endpoint_align_deltas(pcb, _diag(pcb, "TGT"))[0]
        pads = {ref: [] for ref in ("J1", "U3")}
        from kicad_tools.router.stuck_classifier import _iter_board_pads

        for ref, _net, point, _size in _iter_board_pads(pcb):
            if ref in pads:
                pads[ref].append(point)

        before = _axis_separation_deg(
            _pad_axis("J1", pads["J1"]).angle_deg, _pad_axis("U3", pads["U3"]).angle_deg
        )
        fp = next(f for f in pcb.footprints if f.reference == delta.target_ref)
        moved = _rotate_about(
            pads[delta.target_ref], fp.position[0], fp.position[1], delta.rotation_delta
        )
        other = "U3" if delta.target_ref == "J1" else "J1"
        after = _axis_separation_deg(
            _pad_axis(delta.target_ref, moved).angle_deg, _pad_axis(other, pads[other]).angle_deg
        )
        assert before == pytest.approx(90.0, abs=1.0)
        assert after == pytest.approx(0.0, abs=1.0)

    def test_matched_axes_emit_no_candidate(self, tmp_path: Path):
        """Two parallel rows are already aligned -- nothing to propose."""
        pcb = _load(tmp_path, _endpoint_board(sink_offsets=_ROW))
        diag = _diag(pcb, "TGT")
        # Non-vacuous: the ladder still says MOVE_PART; only the axes changed.
        assert diag.recommendation[0].action is RecommendedAction.MOVE_PART
        assert endpoint_align_deltas(pcb, diag) == []

    def test_square_pad_array_emits_no_candidate(self, tmp_path: Path):
        """A pad ring has no dominant axis, so a quarter turn aligns nothing."""
        pcb = _load(tmp_path, _endpoint_board(source_offsets=_SQUARE))
        diag = _diag(pcb, "TGT")
        assert diag.recommendation[0].action is RecommendedAction.MOVE_PART
        assert endpoint_align_deltas(pcb, diag) == []

    def test_locked_endpoint_is_never_proposed(self, tmp_path: Path):
        """``(locked yes)`` is an explicit fixed placement the proposer honours."""
        pcb = _load(tmp_path, _endpoint_board(source_locked=True))
        diag = _diag(pcb, "TGT")
        assert diag.recommendation[0].action is RecommendedAction.MOVE_PART
        targets = {d.target_ref for d in endpoint_align_deltas(pcb, diag)}
        assert "J1" not in targets
        # The unlocked endpoint is still offered -- the lock is scoped, not fatal.
        assert targets == {"U3"}

    def test_fixed_refs_endpoints_emit_no_candidate(self, tmp_path: Path):
        """Both endpoints anchored -> no delta at all (not a malformed one)."""
        pcb = _load(tmp_path, _endpoint_board())
        diag = _diag(pcb, "TGT")
        assert endpoint_align_deltas(pcb, diag)  # unanchored: candidates exist
        assert endpoint_align_deltas(pcb, diag, fixed_refs={"J1", "U3"}) == []

    def test_edge_mounted_connector_is_not_rotated(self, tmp_path: Path):
        """Connector access: a part flush with the board outline keeps its facing."""
        pcb = _load(tmp_path, _endpoint_board(source_at=(2.5, 50.0)))
        diag = _diag(pcb, "TGT")
        assert diag.recommendation[0].action is RecommendedAction.MOVE_PART
        targets = {d.target_ref for d in endpoint_align_deltas(pcb, diag)}
        assert "J1" not in targets

    def test_non_move_part_ladder_emits_no_candidate(self, tmp_path: Path):
        """A reversed bundle is a pad-ORDER defect; a rotation cannot fix it (#4560)."""
        pcb = _load(tmp_path, _facing_rows_bundle_board(reversed_rows=True))
        diag = _diag(pcb, "DQ2")
        assert diag.recommendation[0].action is RecommendedAction.DE_REVERSE_BUNDLE
        assert endpoint_align_deltas(pcb, diag) == []

    def test_candidate_search_does_not_mutate_pcb(self, tmp_path: Path):
        pcb = _load(tmp_path, _endpoint_board())
        before = [(fp.reference, fp.position, fp.rotation, fp.layer) for fp in pcb.footprints]
        endpoint_align_deltas(pcb, _diag(pcb, "TGT"))
        after = [(fp.reference, fp.position, fp.rotation, fp.layer) for fp in pcb.footprints]
        assert before == after

    def test_candidate_round_trips_through_to_dict(self, tmp_path: Path):
        pcb = _load(tmp_path, _endpoint_board())
        delta = endpoint_align_deltas(pcb, _diag(pcb, "TGT"))[0]
        restored = PlacementDelta.from_dict(json.loads(json.dumps(delta.to_dict())))
        assert restored.kind == "rotate_align"
        assert restored.rotation_delta == delta.rotation_delta
        assert restored.target_ref == delta.target_ref
        assert restored.rationale == delta.rationale


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
        assert loaded["kind"] == "mirror"
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
    / "regression-fixture"
    / "matchgroup_test_routed.kicad_pcb"
)


@pytest.mark.skipif(
    not _BOARD07_ARTIFACT.exists(), reason="board-07 committed artifact not present"
)
class TestBoard07:
    @pytest.fixture(scope="class")
    def pcb(self) -> PCB:
        return PCB.load(str(_BOARD07_ARTIFACT))

    def test_ddr_self_crossing_nets_emit_mirror(self, pcb: PCB):
        """The reversed DDR byte (DQ3/DQ4, inversion_fraction ~= 1.0) each emit a
        mirror targeting the reversed facing QFN (#4560)."""
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
            assert delta.kind == "mirror"
            assert delta.rotation_delta == 0.0
            assert delta.target_ref == diag.bundle_orientation.secondary_ref

    def test_tmds_co_oriented_lanes_emit_translate(self, pcb: PCB):
        """The co-oriented TMDS lanes emit translate, never mirror."""
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
        assert "mirror" in kinds
        assert "translate" in kinds
        assert all(isinstance(d, PlacementDelta) for d in deltas)
        # JSON round-trips for the whole batch.
        json.dumps([d.to_dict() for d in deltas])
