"""HV/creepage and keepout guards on the placement-delta loop (Issue #5890).

Epic #5511 Phase 3 turns the classifier-driven loop on by default when the
routing plan is infeasible, which means a bounded placement move can now land
on a board nobody asked to have moved.  Two failure modes the pre-#5890 accept
test structurally could not see:

1. **Creepage.** ``require_no_clearance_regression`` (#4468) and the router's
   own in-loop pairwise gate both score *copper the router produced*.  Two
   cross-domain **pads** pushed inside each other's declared creepage
   requirement produce no trace violation at all -- the move passes reach, it
   passes routed clearance, and it destroys the isolation the voltage map
   declares.
2. **Keepouts.** A ``(zone ... (keepout (tracks not_allowed)))`` rule area is
   respected while *routing* (#4605) but nothing stopped a move from parking
   the footprint's pads inside one.

Split deliberately: the measurement functions are tested against real geometry
(so they can actually fail on a real defect), the loop wiring is tested against
a controlled count sequence (so the revert + its reason are pinned without
hand-building an HV board whose physics would dominate the assertion).
"""

from __future__ import annotations

import pytest

from kicad_tools.router.pairwise_clearance import (
    PadGeometry,
    PairwiseClearanceTable,
    board_pad_geometry,
    pad_pairwise_violations,
    pcb_pad_geometry,
)
from kicad_tools.schema.pcb import PCB
from tests.router.test_placement_delta import _load
from tests.router.test_placement_delta_feedback import (
    FakePad,
    _make_loop,
    _rotate_delta,
    _rotate_fixes_net2,
)

shapely = pytest.importorskip("shapely")


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

#: Two single-pad footprints on different nets, 1.0 mm apart edge-to-edge
#: (centres 2.0 mm apart, 1.0 mm square pads).
_HV_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "HV")
  (net 2 "LV")
  (footprint "R" (layer "F.Cu") (at 10 10)
    (property "Reference" "HV1")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "HV"))
  )
  (footprint "R" (layer "F.Cu") (at 12 10)
    (property "Reference" "LV1")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "LV"))
  )
)
"""


def _table(required_mm: float) -> PairwiseClearanceTable:
    return PairwiseClearanceTable(
        dru=0.2,
        net_voltages={"HV": 300.0, "LV": 0.0},
        required_by_pair={("HV", "LV"): required_mm},
    )


# --------------------------------------------------------------------------- #
# pad_pairwise_violations / pcb_pad_geometry                                   #
# --------------------------------------------------------------------------- #


class TestPadPairwiseViolations:
    def test_gap_below_requirement_is_a_violation(self, tmp_path):
        pcb = _load(tmp_path, _HV_BOARD)
        pads = pcb_pad_geometry(pcb)
        # Pads are 1.0 mm apart edge-to-edge; require 1.6 mm.
        violations = pad_pairwise_violations(pads, _table(1.6))
        assert len(violations) == 1
        v = violations[0]
        assert {v.net_a, v.net_b} == {"HV", "LV"}
        assert v.required_mm == pytest.approx(1.6)
        assert v.actual_mm == pytest.approx(1.0, abs=1e-6)

    def test_gap_meeting_the_requirement_is_clean(self, tmp_path):
        pcb = _load(tmp_path, _HV_BOARD)
        assert pad_pairwise_violations(pcb_pad_geometry(pcb), _table(0.8)) == []

    def test_no_table_is_dormant(self, tmp_path):
        pcb = _load(tmp_path, _HV_BOARD)
        assert pad_pairwise_violations(pcb_pad_geometry(pcb), None) == []

    def test_requirement_at_or_below_the_floor_is_skipped(self, tmp_path):
        """A pair that needs no widening never reaches the geometry at all."""
        pcb = _load(tmp_path, _HV_BOARD)
        table = PairwiseClearanceTable(
            dru=0.2, net_voltages={}, required_by_pair={("HV", "LV"): 0.2}
        )
        assert pad_pairwise_violations(pcb_pad_geometry(pcb), table) == []

    def test_same_net_pads_never_conflict(self):
        poly = shapely.geometry.box(0, 0, 1, 1)
        near = shapely.geometry.box(1.05, 0, 2.05, 1)
        pads = [
            PadGeometry(net_name="HV", layers=frozenset({"F.Cu"}), polygon=poly),
            PadGeometry(net_name="/HV", layers=frozenset({"F.Cu"}), polygon=near),
        ]
        # ``/HV`` and ``HV`` are the same net under KiCad's hierarchical naming.
        assert pad_pairwise_violations(pads, _table(5.0)) == []

    def test_pads_with_no_shared_layer_are_skipped(self):
        poly = shapely.geometry.box(0, 0, 1, 1)
        near = shapely.geometry.box(1.05, 0, 2.05, 1)
        pads = [
            PadGeometry(net_name="HV", layers=frozenset({"F.Cu"}), polygon=poly),
            PadGeometry(net_name="LV", layers=frozenset({"B.Cu"}), polygon=near),
        ]
        assert pad_pairwise_violations(pads, _table(5.0)) == []
        # ...but the same pair on one shared layer IS reported.
        shared = [pads[0], PadGeometry("LV", frozenset({"F.Cu"}), near)]
        assert len(pad_pairwise_violations(shared, _table(5.0))) == 1

    def test_moving_a_footprint_closer_raises_the_count(self, tmp_path):
        """The measurement is a function of PLACEMENT -- the whole point."""
        pcb = _load(tmp_path, _HV_BOARD)
        table = _table(1.6)
        assert len(pad_pairwise_violations(pcb_pad_geometry(pcb), table)) == 1
        # Push LV1 well clear: the same board, the same table, no fail.
        lv = next(fp for fp in pcb.footprints if fp.reference == "LV1")
        lv.position = (20.0, 10.0)
        assert pad_pairwise_violations(pcb_pad_geometry(pcb), table) == []

    def test_in_memory_and_on_disk_geometry_agree(self, tmp_path):
        """``pcb_pad_geometry`` must not drift from the on-disk audit (#4507)."""
        path = tmp_path / "hv.kicad_pcb"
        path.write_text(_HV_BOARD)
        from_disk = board_pad_geometry(path)
        in_memory = pcb_pad_geometry(PCB.load(str(path)))
        assert [p.net_name for p in from_disk] == [p.net_name for p in in_memory]
        for a, b in zip(from_disk, in_memory, strict=True):
            assert a.polygon.equals(b.polygon)
            assert a.layers == b.layers


# --------------------------------------------------------------------------- #
# Loop wiring: a regression reverts, with its own reason                       #
# --------------------------------------------------------------------------- #


def _improving_loop():
    """The canonical "a 180 about (10,10) reflects UB's pad x=12 -> x=8" fixture.

    Identical geometry to
    ``TestDeltaFeedbackKeepRevert.test_applied_rotate_that_improves_reach_is_kept``
    so the baseline is a KEPT delta: every assertion below is therefore about
    the new guard flipping that keep into a revert, not about reach physics.
    """
    return _make_loop(
        footprint_refs=[("UB", 10.0, 10.0)],
        pads=[FakePad(12.0, 10.0, "UB", "2")],
        total_nets=2,
        failed_predicate=_rotate_fixes_net2,
        proposer=lambda _pcb: [_rotate_delta()],
    )


def _loop_with(monkeypatch, counts, attr):
    """Build the improving loop with ``attr`` returning ``counts`` in order."""
    loop, _router, _pcb = _improving_loop()
    series = list(counts)

    def _next(*_a, **_kw):
        return series.pop(0) if len(series) > 1 else series[0]

    monkeypatch.setattr(type(loop), attr, _next)
    return loop


class TestCreepageGuard:
    def test_delta_reverted_when_pad_creepage_regresses(self, monkeypatch):
        """Reach improves, but the move costs a creepage pair -- revert."""
        loop = _loop_with(monkeypatch, [0, 1], "_creepage_violation_count")
        result = loop.run_delta(max_adjustments=1, use_negotiated=True)
        assert result.applied_deltas == []
        assert len(result.reverted_deltas) == 1
        assert "pairwise creepage fails 0 -> 1" in result.reverted_reasons[0]

    def test_delta_kept_when_creepage_is_unchanged(self, monkeypatch):
        loop = _loop_with(monkeypatch, [1, 1], "_creepage_violation_count")
        result = loop.run_delta(max_adjustments=1, use_negotiated=True)
        assert len(result.applied_deltas) == 1, "an unchanged count must not block a win"

    def test_guard_can_be_disabled(self, monkeypatch):
        loop = _loop_with(monkeypatch, [0, 1], "_creepage_violation_count")
        result = loop.run_delta(
            max_adjustments=1, use_negotiated=True, require_no_creepage_regression=False
        )
        assert len(result.applied_deltas) == 1

    def test_dormant_without_a_pairwise_table(self):
        """No ``--voltage-map`` -> no measurement, no behaviour change."""
        loop, _router, _pcb = _improving_loop()
        assert loop._pairwise_table() is None
        assert loop._creepage_violation_count() is None
        result = loop.run_delta(max_adjustments=1, use_negotiated=True)
        assert len(result.applied_deltas) == 1


class TestKeepoutGuard:
    def test_delta_reverted_when_more_pads_land_in_a_keepout(self, monkeypatch):
        loop = _loop_with(monkeypatch, [0, 2], "_keepout_intrusion_count")
        result = loop.run_delta(max_adjustments=1, use_negotiated=True)
        assert result.applied_deltas == []
        assert len(result.reverted_deltas) == 1
        reason = result.reverted_reasons[0]
        assert "keepout rule area 0 -> 2" in reason
        assert "UB" in reason

    def test_leaving_a_keepout_is_not_a_regression(self, monkeypatch):
        loop = _loop_with(monkeypatch, [2, 0], "_keepout_intrusion_count")
        result = loop.run_delta(max_adjustments=1, use_negotiated=True)
        assert len(result.applied_deltas) == 1

    def test_guard_can_be_disabled(self, monkeypatch):
        loop = _loop_with(monkeypatch, [0, 2], "_keepout_intrusion_count")
        result = loop.run_delta(
            max_adjustments=1, use_negotiated=True, require_no_keepout_intrusion=False
        )
        assert len(result.applied_deltas) == 1

    def test_dormant_without_declared_rule_areas(self):
        loop, _router, _pcb = _improving_loop()
        assert loop._keepout_intrusion_count("UB") is None


# --------------------------------------------------------------------------- #
# The measurements themselves, driven through the loop's own accessors         #
# --------------------------------------------------------------------------- #


class TestLoopMeasuresRealGeometry:
    """The guards above are wired to counts; these pin the counts to geometry.

    Without this pair, every guard assertion above would be satisfied by a
    method that always returns ``0``.
    """

    def test_creepage_count_tracks_the_live_placement(self, tmp_path):
        loop, router, _mock_pcb = _improving_loop()
        loop.pcb = _load(tmp_path, _HV_BOARD)

        class _Rules:
            pairwise_clearance = _table(1.6)

        router.rules = _Rules()
        assert loop._pairwise_table() is not None
        assert loop._creepage_violation_count() == 1
        # Move LV1 clear and the SAME accessor reports the pair resolved.
        lv = next(fp for fp in loop.pcb.footprints if fp.reference == "LV1")
        lv.position = (20.0, 10.0)
        assert loop._creepage_violation_count() == 0

    def test_keepout_count_tracks_the_live_pad_positions(self):
        from kicad_tools.router.core import KeepoutRuleArea

        loop, router, _pcb = _improving_loop()
        area = KeepoutRuleArea(
            polygon=((11.0, 9.0), (13.0, 9.0), (13.0, 11.0), (11.0, 11.0)),
            layers=frozenset({0}),
            blocks_tracks=True,
            blocks_vias=True,
            name="HV_KEEPOUT",
        )
        router._keepout_rule_area_polygons = lambda: [area]

        # UB's only pad starts at (12, 10) -- inside the area.
        assert loop._keepout_intrusion_count("UB") == 1
        # A pad-centre outside it is not counted...
        router.pads[("UB", "2")].x = 8.0
        assert loop._keepout_intrusion_count("UB") == 0
        # ...and a pour-void-only area (tracks allowed) is ignored entirely.
        router.pads[("UB", "2")].x = 12.0
        router._keepout_rule_area_polygons = lambda: [
            KeepoutRuleArea(
                polygon=area.polygon,
                layers=area.layers,
                blocks_tracks=False,
                blocks_vias=False,
                name="POUR_VOID",
            )
        ]
        assert loop._keepout_intrusion_count("UB") is None


# --------------------------------------------------------------------------- #
# Physical identity: duplicate references (Issue #5902)                        #
# --------------------------------------------------------------------------- #


def _duplicate_ref_loop():
    """Two footprints both authored ``UB``; pads carry their physical key.

    ``component_keys()`` synthesises a distinct ``component_id`` per footprint
    when references repeat, so ``delta.target_key`` is that synthetic key while
    every router ``pad.ref`` stays the raw duplicate ``"UB"``.  The first UB's
    pad sits at (12, 10) exactly as in :func:`_improving_loop`; the second UB is
    parked far away, inside nothing.
    """
    from kicad_tools.router.placement_delta import PlacementDelta
    from kicad_tools.schema.physical_identity import footprint_keys
    from tests.router.test_placement_delta_feedback import MockFootprint

    keys = footprint_keys([MockFootprint("UB", 10.0, 10.0), MockFootprint("UB", 30.0, 30.0)])
    assert keys[0] != "UB" and keys[0] != keys[1], keys

    first = FakePad(12.0, 10.0, "UB", "2")
    first.component_id = keys[0]
    second = FakePad(32.0, 30.0, "UB", "9")
    second.component_id = keys[1]
    delta = PlacementDelta(
        net_name="DQ2",
        target_ref="UB",
        kind="rotate_180",
        rotation_delta=180.0,
        source_action="de_reverse_bundle",
        component_id=keys[0],
    )
    loop, router, pcb = _make_loop(
        footprint_refs=[("UB", 10.0, 10.0), ("UB", 30.0, 30.0)],
        pads=[first, second],
        total_nets=2,
        failed_predicate=_rotate_fixes_net2,
        proposer=lambda _pcb: [delta],
    )
    return loop, router, keys


def _keepout_area(polygon):
    from kicad_tools.router.core import KeepoutRuleArea

    return KeepoutRuleArea(
        polygon=polygon,
        layers=frozenset({0}),
        blocks_tracks=True,
        blocks_vias=True,
        name="HV_KEEPOUT",
    )


class TestKeepoutGuardPhysicalIdentity:
    """The guard keys on ``component_id or ref``, not the authored ref."""

    def test_count_matches_pads_by_physical_key(self):
        loop, router, keys = _duplicate_ref_loop()
        # Covers BOTH UB pads: only the targeted footprint's pad may count.
        area = _keepout_area(((11.0, 9.0), (33.0, 9.0), (33.0, 31.0), (11.0, 31.0)))
        router._keepout_rule_area_polygons = lambda: [area]
        assert loop._keepout_intrusion_count(keys[0]) == 1
        assert loop._keepout_intrusion_count(keys[1]) == 1

    def test_delta_into_keepout_reverted_on_duplicate_reference_board(self):
        """Pre-#5902 this was 0 -> 0 (no pad matched) and the move was kept."""
        loop, router, keys = _duplicate_ref_loop()
        # The 180 about (10, 10) moves the first UB's pad from x=12 to x=8.
        area = _keepout_area(((7.0, 9.0), (9.0, 9.0), (9.0, 11.0), (7.0, 11.0)))
        router._keepout_rule_area_polygons = lambda: [area]
        assert loop._keepout_intrusion_count(keys[0]) == 0

        result = loop.run_delta(max_adjustments=1, use_negotiated=True)
        assert result.applied_deltas == []
        assert len(result.reverted_deltas) == 1
        assert "keepout rule area 0 -> 1" in result.reverted_reasons[0]
        # The revert restored the pad to its pre-move position.
        assert router.pads[("UB", "2")].x == pytest.approx(12.0)
