"""Unit tests for the stuck-net classifier (issue #3863, M1 of #3862).

Each test builds a small synthetic ``.kicad_pcb`` that isolates ONE failure
mode and asserts the classifier labels it correctly:

* ESCAPE_BLOCKED      -- a stranded pad walled in by foreign copper on all sides
* CONGESTION_SATURATED -- a reachable pad boxed in by committed strict-net copper
* PLACEMENT_BOUND     -- a reachable pad, no rippable copper, MODERATE congestion
* BUDGET_STARVED      -- a reachable pad, no rippable copper, SPARSE neighbourhood

The classifier is pure geometry, so these synthetic boards exercise the real
decision tree without needing the router.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.stuck_classifier import (
    StuckClass,
    classify_stuck_nets,
)

# ---------------------------------------------------------------------------
# Synthetic boards
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


def _ring_pads(cx: float, cy: float, radius: float, net: int, name: str) -> str:
    """16 foreign pads in a tight ring around (cx, cy) -- a wall with no lane."""
    import math

    out = []
    for i in range(16):
        ang = 2 * math.pi * i / 16
        px = cx + radius * math.cos(ang)
        py = cy + radius * math.sin(ang)
        out.append(
            f'  (footprint "wall{i}" (layer "F.Cu") (at {px:.4f} {py:.4f})\n'
            f'    (property "Reference" "W{i}")\n'
            f'    (pad "1" smd circle (at 0 0) (size 0.1 0.1) '
            f'(layers "F.Cu") (net {net} "{name}"))\n'
            f"  )\n"
        )
    return "".join(out)


# --- ESCAPE_BLOCKED ---------------------------------------------------------
# Net TGT has a connected island (R1.1 <-> R1.2 routed) plus a STRANDED pad
# (U1.1) at (50, 50) that is surrounded on all sides by a tight ring of foreign
# pads (net WALL) at 0.15mm -- no open escape lane.


def _escape_blocked_board() -> str:
    body = (
        _HEADER
        + (
            '  (net 0 "")\n'
            '  (net 1 "TGT")\n'
            '  (net 2 "WALL")\n'
            # connected island for TGT so the net is "incomplete", not "unrouted"
            '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
            '    (property "Reference" "R1")\n'
            '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
            '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
            "  )\n"
            # the stranded TGT pad, far from the island and walled in
            '  (footprint "U_QFN" (layer "F.Cu") (at 50 50)\n'
            '    (property "Reference" "U1")\n'
            '    (pad "1" smd circle (at 0 0) (size 0.1 0.1) (layers "F.Cu") (net 1 "TGT"))\n'
            "  )\n"
            + _ring_pads(50, 50, 0.15, 2, "WALL")
            # route the island so R1.1-R1.2 are connected (=> 1 stranded pad U1.1)
            + '  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
            ")\n"
        )
    )
    return body


# --- CONGESTION_SATURATED ---------------------------------------------------
# Net TGT has a connected island and one stranded pad (U1.1) with an OPEN escape
# lane (no surrounding wall), but a fully-connected STRICT net (BLK) runs its
# committed copper ~0.2mm from the stranded pad -- a rippable blocker.


def _congestion_saturated_board() -> str:
    body = (
        _HEADER
        + (
            '  (net 0 "")\n'
            '  (net 1 "TGT")\n'
            '  (net 2 "BLK")\n'
            # TGT island
            '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
            '    (property "Reference" "R1")\n'
            '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
            '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
            "  )\n"
            # stranded TGT pad with an open lane (nothing within escape clearance)
            '  (footprint "U_SOT" (layer "F.Cu") (at 50 50)\n'
            '    (property "Reference" "U1")\n'
            '    (pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net 1 "TGT"))\n'
            "  )\n"
            # BLK is a complete 2-pad signal net (strict) whose copper passes 0.2mm
            # from the stranded TGT pad at (50,50): segment y=50.2.
            '  (footprint "R_0402" (layer "F.Cu") (at 48 50.2)\n'
            '    (property "Reference" "R2")\n'
            '    (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "F.Cu") (net 2 "BLK"))\n'
            "  )\n"
            '  (footprint "R_0402" (layer "F.Cu") (at 52 50.2)\n'
            '    (property "Reference" "R3")\n'
            '    (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "F.Cu") (net 2 "BLK"))\n'
            "  )\n"
            # route TGT island and the full BLK net
            + '  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
            + '  (segment (start 48 50.2) (end 52 50.2) (width 0.25) (layer "F.Cu") (net 2))\n'
            ")\n"
        )
    )
    return body


# --- BUDGET_STARVED ---------------------------------------------------------
# Net TGT has a connected island and one stranded pad (U1.1) far out in open
# space: open escape lane, NO strict copper anywhere near, ZERO foreign
# obstructions.  Geometrically nothing is in the way -- the batch router simply
# never committed it.  This is the exact "0 foreign obstructions -- ripping
# cannot help, a part must move" false positive from the bug report (issue
# #4158): it looks routable on current copper, so it must NOT be escalated to a
# placement problem.


def _budget_starved_board() -> str:
    body = _HEADER + (
        '  (net 0 "")\n'
        '  (net 1 "TGT")\n'
        # TGT island
        '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
        '    (property "Reference" "R1")\n'
        '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
        '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
        "  )\n"
        # stranded TGT pad alone in open space -- 0 foreign obstructions
        '  (footprint "U_SOT" (layer "F.Cu") (at 80 80)\n'
        '    (property "Reference" "U1")\n'
        '    (pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net 1 "TGT"))\n'
        "  )\n" + '  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
        ")\n"
    )
    return body


# --- PLACEMENT_BOUND --------------------------------------------------------
# Net TGT has a connected island and one stranded pad (U1.1) with an OPEN escape
# lane (nothing within the escape ring), NO rippable strict copper, but a
# genuinely busy neighbourhood: a partial ring of foreign single-pad nets at
# ~1.5mm packs the 2.0mm congestion radius (moderate, >= congestion_threshold
# but below the dense-cluster line) while leaving a > 45 deg gap so the pad
# still escapes.  Reachable but locally busy enough that a solo route is not a
# safe default -- a part must move.  These foreign pads are on distinct
# single-pad (unrouted, non-strict) nets so none is a rippable blocker.


def _placement_bound_ring(cx: float, cy: float, radius: float, count: int, gap: int) -> str:
    """`count` foreign single-pad nets around (cx, cy), leaving a `gap`-slot arc.

    Pads sit at *radius* mm (outside the 1.0mm escape ring, inside the 2.0mm
    congestion ring) so they raise local_congestion without closing the escape
    lane.  Each pad is its own single-pad net (never fully connected => never a
    strict rippable blocker).  The angular gap keeps a wide-open escape arc.
    """
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


def _placement_bound_board() -> str:
    body = (
        _HEADER
        + (
            '  (net 0 "")\n'
            '  (net 1 "TGT")\n'
            # TGT island
            '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
            '    (property "Reference" "R1")\n'
            '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
            '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
            "  )\n"
            # stranded TGT pad with a busy-but-passable neighbourhood
            '  (footprint "U_SOT" (layer "F.Cu") (at 80 80)\n'
            '    (property "Reference" "U1")\n'
            '    (pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net 1 "TGT"))\n'
            "  )\n"
            # 10 foreign obstructions at 1.5mm, leaving a 4-slot (~103 deg) open arc
            + _placement_bound_ring(80, 80, 1.5, count=10, gap=4)
            + '  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
            ")\n"
        )
    )
    return body


# --- DENSE CLUSTER (Defect 1 negative test) ---------------------------------
# An all-digital dense cluster: a stranded pad surrounded by >= 20 foreign
# single-pad nets (dense-cluster PLACEMENT_BOUND) with an open escape gap.  No
# net on this board carries any analog/codec signal, so the classifier must not
# emit the old hardcoded "(analog/codec cluster)" parenthetical (issue #4261
# Defect 1).


def _dense_cluster_board() -> str:
    body = (
        _HEADER
        + (
            '  (net 0 "")\n'
            '  (net 1 "TGT")\n'
            # TGT island
            '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
            '    (property "Reference" "R1")\n'
            '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
            '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
            "  )\n"
            # stranded TGT pad in a very busy neighbourhood
            '  (footprint "U_SOT" (layer "F.Cu") (at 80 80)\n'
            '    (property "Reference" "U1")\n'
            '    (pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net 1 "TGT"))\n'
            "  )\n"
            # 24 foreign single-pad nets at 1.5mm packing the congestion radius
            # (dense, >= dense_cluster_threshold=20), leaving a small open arc so
            # the pad still escapes.
            + _placement_bound_ring(80, 80, 1.5, count=24, gap=2)
            + '  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
            ")\n"
        )
    )
    return body


@pytest.fixture
def dense_cluster_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "dense.kicad_pcb"
    p.write_text(_dense_cluster_board())
    return p


@pytest.fixture
def escape_blocked_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "escape.kicad_pcb"
    p.write_text(_escape_blocked_board())
    return p


@pytest.fixture
def congestion_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "congestion.kicad_pcb"
    p.write_text(_congestion_saturated_board())
    return p


@pytest.fixture
def placement_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "placement.kicad_pcb"
    p.write_text(_placement_bound_board())
    return p


@pytest.fixture
def budget_starved_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "budget.kicad_pcb"
    p.write_text(_budget_starved_board())
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEscapeBlocked:
    def test_stranded_pad_walled_in_is_escape_blocked(self, escape_blocked_pcb: Path):
        result = classify_stuck_nets(escape_blocked_pcb)
        tgt = [d for d in result.diagnoses if d.net_name == "TGT"]
        assert len(tgt) == 1
        d = tgt[0]
        assert d.classification is StuckClass.ESCAPE_BLOCKED
        assert "U1.1" in d.unconnected_pads
        # the open arc must be below the escape threshold
        assert d.escape_lane_deg < 45.0
        assert "no escape lane" in d.evidence


class TestCongestionSaturated:
    def test_reachable_pad_boxed_by_strict_copper(self, congestion_pcb: Path):
        result = classify_stuck_nets(congestion_pcb)
        tgt = [d for d in result.diagnoses if d.net_name == "TGT"]
        assert len(tgt) == 1
        d = tgt[0]
        assert d.classification is StuckClass.CONGESTION_SATURATED
        assert "BLK" in d.blocking_nets
        assert d.nearest_blocker_mm is not None
        assert d.nearest_blocker_mm < 0.5
        assert "1:1 trade" in d.evidence


class TestPlacementBound:
    def test_reachable_pad_no_rippable_copper_but_congested(self, placement_pcb: Path):
        """Moderate congestion, no blocker, open lane -> PLACEMENT_BOUND (not
        budget-starved): reachable but locally busy enough that a solo route is
        not a safe default assumption."""
        result = classify_stuck_nets(placement_pcb)
        tgt = [d for d in result.diagnoses if d.net_name == "TGT"]
        assert len(tgt) == 1
        d = tgt[0]
        assert d.classification is StuckClass.PLACEMENT_BOUND
        assert d.blocking_nets == []
        # neighbourhood must be moderately congested (>= congestion_threshold=6)
        # but below the dense-cluster line (20), and the lane must still be open.
        assert 6 <= d.local_congestion < 20
        assert d.escape_lane_deg >= 45.0
        assert "a part must move" in d.evidence


class TestDefect1NoAnalogCodecString:
    def test_dense_cluster_evidence_has_no_analog_codec(self, dense_cluster_pcb: Path):
        """Defect 1 (#4261): the dense-cluster PLACEMENT_BOUND branch must NOT
        append the hardcoded "(analog/codec cluster)" parenthetical.  This board
        is all-digital, so an analog cause is factually wrong."""
        result = classify_stuck_nets(dense_cluster_pcb)
        tgt = next(d for d in result.diagnoses if d.net_name == "TGT")
        assert tgt.classification is StuckClass.PLACEMENT_BOUND
        assert tgt.local_congestion >= 20
        assert "analog" not in tgt.evidence.lower()
        assert "codec" not in tgt.evidence.lower()


class TestBudgetStarved:
    def test_sparse_open_lane_no_blocker_is_budget_starved(self, budget_starved_pcb: Path):
        """The bug-report false positive: a stranded pad with an open lane, no
        rippable copper, and 0 foreign obstructions must be BUDGET_STARVED, not
        PLACEMENT_BOUND -- it looks routable on current copper and just needs a
        re-route / bigger budget (issue #4158)."""
        result = classify_stuck_nets(budget_starved_pcb)
        tgt = [d for d in result.diagnoses if d.net_name == "TGT"]
        assert len(tgt) == 1
        d = tgt[0]
        assert d.classification is StuckClass.BUDGET_STARVED
        assert d.blocking_nets == []
        assert d.local_congestion < 6
        assert d.escape_lane_deg >= 45.0
        # evidence must disclaim confidence (no route was attempted) and must NOT
        # claim a part must move.
        assert "not confirmed" in d.evidence
        assert "a part must move" not in d.evidence
        assert "re-route" in d.evidence.lower()

    def test_failure_cause_is_routing_order(self, budget_starved_pcb: Path):
        result = classify_stuck_nets(budget_starved_pcb)
        d = next(x for x in result.diagnoses if x.net_name == "TGT")
        data = d.to_dict()
        assert data["classification"] == "budget_starved"
        # same FailureCause bucket as PLACEMENT_BOUND: a negotiation/ordering
        # artifact, not congestion or pin access.
        assert data["failure_cause"] == "routing_order"


def _pour_discontinuous_board() -> str:
    """VCC (power net) is incomplete: R1.1/R1.2 connected, U1.1 stranded."""
    return _HEADER + (
        '  (net 0 "")\n'
        '  (net 1 "VCC")\n'
        '  (net 2 "SIG")\n'
        '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
        '    (property "Reference" "R1")\n'
        '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))\n'
        '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))\n'
        "  )\n"
        '  (footprint "U_SOT" (layer "F.Cu") (at 80 80)\n'
        '    (property "Reference" "U1")\n'
        '    (pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net 1 "VCC"))\n'
        "  )\n"
        # connect R1.1-R1.2 so VCC is "incomplete" (not "unrouted")
        '  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
        ")\n"
    )


class TestPourDiscontinuous:
    def test_power_net_stranded_pad_is_pour_discontinuous(self, tmp_path: Path):
        p = tmp_path / "pour.kicad_pcb"
        p.write_text(_pour_discontinuous_board())
        result = classify_stuck_nets(p)
        vcc = [d for d in result.diagnoses if d.net_name == "VCC"]
        assert len(vcc) == 1
        d = vcc[0]
        assert d.classification is StuckClass.POUR_DISCONTINUOUS
        assert "U1.1" in d.unconnected_pads
        assert "pour-carried" in d.evidence

    def test_pour_discontinuous_not_in_signal_diagnoses(self, tmp_path: Path):
        """VCC must not appear under any signal failure class."""
        p = tmp_path / "pour.kicad_pcb"
        p.write_text(_pour_discontinuous_board())
        result = classify_stuck_nets(p)
        signal_classes = {
            StuckClass.ESCAPE_BLOCKED,
            StuckClass.CONGESTION_SATURATED,
            StuckClass.PLACEMENT_BOUND,
            StuckClass.BUDGET_STARVED,
        }
        for d in result.diagnoses:
            if d.net_name == "VCC":
                assert d.classification not in signal_classes

    def test_counts_include_pour_discontinuous(self, tmp_path: Path):
        p = tmp_path / "pour.kicad_pcb"
        p.write_text(_pour_discontinuous_board())
        result = classify_stuck_nets(p)
        assert "pour_discontinuous" in result.counts
        assert result.counts["pour_discontinuous"] >= 1


class TestClassifierAggregate:
    def test_counts_and_to_dict(self, congestion_pcb: Path):
        result = classify_stuck_nets(congestion_pcb)
        counts = result.counts
        assert set(counts) == {
            "escape_blocked",
            "congestion_saturated",
            "placement_bound",
            "budget_starved",
            "pour_discontinuous",
        }
        assert counts["congestion_saturated"] == 1
        data = result.to_dict()
        assert data["summary"]["stuck_nets"] == 1
        net = data["nets"][0]
        assert net["classification"] == "congestion_saturated"
        # failure_cause maps onto the shared FailureCause enum
        assert net["failure_cause"] == "congestion"

    def test_no_stuck_nets_on_complete_board(self, tmp_path: Path):
        # A fully-routed 2-pad net -> no incomplete signal nets.
        pcb = tmp_path / "done.kicad_pcb"
        pcb.write_text(
            _HEADER
            + (
                '  (net 0 "")\n'
                '  (net 1 "DONE")\n'
                '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
                '    (property "Reference" "R1")\n'
                '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "DONE"))\n'
                "  )\n"
                '  (footprint "R_0402" (layer "F.Cu") (at 20 10)\n'
                '    (property "Reference" "R2")\n'
                '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "DONE"))\n'
                "  )\n"
                + '  (segment (start 9.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
                ")\n"
            )
        )
        result = classify_stuck_nets(pcb)
        assert result.diagnoses == []


class TestReadOnly:
    def test_classifier_does_not_mutate_board(self, congestion_pcb: Path):
        before = congestion_pcb.read_bytes()
        classify_stuck_nets(congestion_pcb)
        after = congestion_pcb.read_bytes()
        assert before == after
