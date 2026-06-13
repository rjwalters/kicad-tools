"""Tests for the sum-of-clearances area-estimation heuristic (Issue #3403).

Covers the pre-route geometric estimator that lets ``--auto-pcb-size`` skip
doomed routing attempts when the current envelope is clearly too small:

  - :func:`estimate_required_area` math (footprint area, perimeter halo,
    routing-channel term, packing-overhead multiplier).
  - :func:`envelope_meets_area_estimate` decision boundary.
  - Edge cases: empty PCB, footprints without pads, ``packing_overhead=0``
    kill switch.
  - Integration: the EscalationPolicy.packing_overhead field flows
    through.
  - Calibration sanity: ratios match the expected envelope-vs-required
    pattern for synthetic small-board and large-board fixtures.

No router behaviour is exercised here -- this is a pure-logic unit-test
boundary.  The route-loop integration (``route_with_size_escalation``) is
covered by :mod:`tests.test_auto_pcb_size_integration` (or its own
follow-on integration suite).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kicad_tools.router.auto_pcb_size import (
    DEFAULT_PACKING_OVERHEAD,
    DEFAULT_ROUTING_CHANNEL_PER_NET_MM2,
    AreaEstimate,
    envelope_meets_area_estimate,
    estimate_required_area,
)
from kicad_tools.router.mfr_limits import MFR_JLCPCB, MFR_OSHPARK
from kicad_tools.spec.schema import EscalationPolicy

# ---------------------------------------------------------------------------
# Lightweight stand-in fixtures
#
# The estimator only consults pad geometry and net names; we avoid the
# PCB-load round-trip by passing in a tiny duck-typed stand-in.  This
# keeps the unit tests fast and self-contained -- no .kicad_pcb fixture
# files needed.
# ---------------------------------------------------------------------------


@dataclass
class _StubPad:
    position: tuple[float, float]
    size: tuple[float, float]


@dataclass
class _StubFootprint:
    pads: list = field(default_factory=list)


@dataclass
class _StubNet:
    name: str


@dataclass
class _StubPCB:
    footprints: list = field(default_factory=list)
    nets: dict = field(default_factory=dict)


def _make_pcb(
    *,
    fps: list[_StubFootprint] | None = None,
    nets: dict[int, _StubNet] | None = None,
) -> _StubPCB:
    return _StubPCB(
        footprints=fps or [],
        nets=nets or {},
    )


def _square_footprint(side_mm: float) -> _StubFootprint:
    """Build a single-pad footprint that contributes ``side_mm x side_mm`` to the bbox."""
    return _StubFootprint(pads=[_StubPad(position=(0.0, 0.0), size=(side_mm, side_mm))])


# ---------------------------------------------------------------------------
# AreaEstimate value class
# ---------------------------------------------------------------------------


class TestAreaEstimate:
    def test_total_cm2_conversion(self):
        est = AreaEstimate(
            footprint_area_mm2=100.0,
            clearance_halo_mm2=50.0,
            routing_channel_mm2=20.0,
            packing_overhead=2.5,
            total_mm2=425.0,
            signal_net_count=1,
            footprint_count=1,
        )
        assert est.total_cm2 == pytest.approx(4.25)


# ---------------------------------------------------------------------------
# estimate_required_area: formula correctness
# ---------------------------------------------------------------------------


class TestEstimateRequiredAreaMath:
    """Verify the estimator's arithmetic against hand-computed values."""

    def test_empty_pcb(self):
        """No footprints, no nets -> all terms zero, total zero."""
        pcb = _make_pcb()
        est = estimate_required_area(pcb, MFR_JLCPCB)
        assert est.footprint_area_mm2 == 0.0
        assert est.clearance_halo_mm2 == 0.0
        assert est.routing_channel_mm2 == 0.0
        assert est.total_mm2 == 0.0
        assert est.signal_net_count == 0
        assert est.footprint_count == 0

    def test_single_square_footprint_no_nets(self):
        """One 5x5 mm SMD, no signal nets.

        Hand math:
          - footprint area = 25 mm^2
          - halo = 2 * (5 + 5) * 0.127 = 2.54 mm^2
          - channel = 0 (no nets)
          - total = 2.5 * (25 + 2.54 + 0) = 68.85 mm^2
        """
        pcb = _make_pcb(fps=[_square_footprint(5.0)])
        est = estimate_required_area(pcb, MFR_JLCPCB)
        assert est.footprint_area_mm2 == pytest.approx(25.0)
        assert est.clearance_halo_mm2 == pytest.approx(2.54)
        assert est.routing_channel_mm2 == pytest.approx(0.0)
        assert est.footprint_count == 1
        # 2.5 * (25 + 2.54 + 0) = 68.85
        assert est.total_mm2 == pytest.approx(68.85)

    def test_signal_net_contributes_channel_area(self):
        """One signal net -> one routing-channel allocation (default 20 mm^2)."""
        pcb = _make_pcb(
            fps=[_square_footprint(5.0)],
            nets={1: _StubNet(name="SIGNAL_A")},
        )
        est = estimate_required_area(pcb, MFR_JLCPCB)
        assert est.signal_net_count == 1
        assert est.routing_channel_mm2 == pytest.approx(DEFAULT_ROUTING_CHANNEL_PER_NET_MM2)
        # 2.5 * (25 + 2.54 + 20) = 118.85
        assert est.total_mm2 == pytest.approx(118.85)

    def test_pour_nets_excluded(self):
        """GND / +3V3 / -12V / VCC / etc. don't contribute to channel term."""
        pour_names = ["GND", "+3V3", "+5V", "VCC", "VDD", "AGND", "DGND", "-12V"]
        pcb = _make_pcb(
            fps=[_square_footprint(5.0)],
            nets={i: _StubNet(name=n) for i, n in enumerate(pour_names, start=1)},
        )
        est = estimate_required_area(pcb, MFR_JLCPCB)
        # All pour nets excluded -> signal count 0.
        assert est.signal_net_count == 0
        assert est.routing_channel_mm2 == 0.0

    def test_mixed_nets(self):
        """Signal + pour nets correctly classified."""
        pcb = _make_pcb(
            fps=[_square_footprint(5.0)],
            nets={
                1: _StubNet(name="SIGNAL_A"),
                2: _StubNet(name="GND"),
                3: _StubNet(name="SIGNAL_B"),
                4: _StubNet(name="+3V3"),
                5: _StubNet(name=""),  # unconnected
            },
        )
        est = estimate_required_area(pcb, MFR_JLCPCB)
        assert est.signal_net_count == 2
        assert est.routing_channel_mm2 == pytest.approx(2 * DEFAULT_ROUTING_CHANNEL_PER_NET_MM2)

    def test_multi_pad_footprint_bbox(self):
        """Multi-pad footprint's bbox spans the pad-array extent."""
        # Two pads at (0,0) and (10,5), each 2x2 mm
        # bbox extent: x in [-1, 11], y in [-1, 6]
        # -> W = 12, H = 7
        # -> bbox area = 84 mm^2
        # -> halo = 2 * (12 + 7) * 0.127 = 4.826 mm^2
        fp = _StubFootprint(
            pads=[
                _StubPad(position=(0.0, 0.0), size=(2.0, 2.0)),
                _StubPad(position=(10.0, 5.0), size=(2.0, 2.0)),
            ]
        )
        pcb = _make_pcb(fps=[fp])
        est = estimate_required_area(pcb, MFR_JLCPCB)
        assert est.footprint_area_mm2 == pytest.approx(84.0)
        assert est.clearance_halo_mm2 == pytest.approx(2 * (12 + 7) * 0.127)
        assert est.footprint_count == 1

    def test_footprint_no_pads_skipped(self):
        """Mechanical-only footprints (no pads) contribute nothing."""
        fp_empty = _StubFootprint(pads=[])
        fp_real = _square_footprint(5.0)
        pcb = _make_pcb(fps=[fp_empty, fp_real])
        est = estimate_required_area(pcb, MFR_JLCPCB)
        # Only the real footprint contributes.
        assert est.footprint_count == 1
        assert est.footprint_area_mm2 == pytest.approx(25.0)

    def test_mfr_clearance_scales_halo(self):
        """Tighter manufacturer clearance shrinks the halo proportionally."""
        pcb = _make_pcb(fps=[_square_footprint(5.0)])
        # JLCPCB: 0.127 mm -- halo = 2 * 10 * 0.127 = 2.54 mm^2
        est_jlc = estimate_required_area(pcb, MFR_JLCPCB)
        # OSHPark: 0.152 mm -- halo = 2 * 10 * 0.152 = 3.04 mm^2
        est_osh = estimate_required_area(pcb, MFR_OSHPARK)
        assert est_osh.clearance_halo_mm2 > est_jlc.clearance_halo_mm2
        ratio = est_osh.clearance_halo_mm2 / est_jlc.clearance_halo_mm2
        assert ratio == pytest.approx(0.152 / 0.127, rel=1e-6)

    def test_packing_overhead_multiplier(self):
        """packing_overhead scales the final total linearly."""
        pcb = _make_pcb(
            fps=[_square_footprint(5.0)],
            nets={1: _StubNet(name="SIG")},
        )
        # base sum (per arithmetic above) = 25 + 2.54 + 20 = 47.54
        est_25 = estimate_required_area(pcb, MFR_JLCPCB, packing_overhead=2.5)
        est_30 = estimate_required_area(pcb, MFR_JLCPCB, packing_overhead=3.0)
        assert est_25.total_mm2 == pytest.approx(2.5 * 47.54)
        assert est_30.total_mm2 == pytest.approx(3.0 * 47.54)
        # Ratio is exactly the multiplier ratio.
        assert est_30.total_mm2 / est_25.total_mm2 == pytest.approx(3.0 / 2.5)

    def test_packing_overhead_zero_disables_total(self):
        """packing_overhead=0 yields total_mm2=0 (kill switch)."""
        pcb = _make_pcb(
            fps=[_square_footprint(5.0)],
            nets={1: _StubNet(name="SIG")},
        )
        est = estimate_required_area(pcb, MFR_JLCPCB, packing_overhead=0.0)
        # The individual terms are still computed (for inspection).
        assert est.footprint_area_mm2 == pytest.approx(25.0)
        assert est.routing_channel_mm2 == pytest.approx(20.0)
        # Total is forced to zero.
        assert est.total_mm2 == 0.0


# ---------------------------------------------------------------------------
# envelope_meets_area_estimate decision boundary
# ---------------------------------------------------------------------------


class TestEnvelopeMeetsAreaEstimate:
    def _est(self, total: float) -> AreaEstimate:
        return AreaEstimate(
            footprint_area_mm2=0.0,
            clearance_halo_mm2=0.0,
            routing_channel_mm2=0.0,
            packing_overhead=2.5,
            total_mm2=total,
            signal_net_count=0,
            footprint_count=0,
        )

    def test_envelope_strictly_greater_meets(self):
        assert envelope_meets_area_estimate(200.0, self._est(100.0)) is True

    def test_envelope_equal_meets(self):
        """Boundary: equal counts as meeting the requirement."""
        assert envelope_meets_area_estimate(100.0, self._est(100.0)) is True

    def test_envelope_less_does_not_meet(self):
        assert envelope_meets_area_estimate(99.0, self._est(100.0)) is False

    def test_zero_estimate_always_meets(self):
        """packing_overhead=0 kill switch -> always meets (estimator disabled)."""
        assert envelope_meets_area_estimate(0.0, self._est(0.0)) is True
        assert envelope_meets_area_estimate(1.0, self._est(0.0)) is True


# ---------------------------------------------------------------------------
# EscalationPolicy.packing_overhead integration
# ---------------------------------------------------------------------------


class TestPackingOverheadPolicyField:
    def test_default_matches_constant(self):
        """The schema default tracks the auto_pcb_size module constant."""
        policy = EscalationPolicy()
        assert policy.packing_overhead == DEFAULT_PACKING_OVERHEAD

    def test_custom_packing_overhead_accepted(self):
        policy = EscalationPolicy(packing_overhead=3.0)
        assert policy.packing_overhead == 3.0

    def test_zero_packing_overhead_accepted(self):
        """Zero is the documented kill-switch value."""
        policy = EscalationPolicy(packing_overhead=0.0)
        assert policy.packing_overhead == 0.0

    def test_negative_packing_overhead_rejected(self):
        with pytest.raises(ValueError):
            EscalationPolicy(packing_overhead=-1.0)


# ---------------------------------------------------------------------------
# Calibration sanity: synthetic small-board vs. large-board ratios
# ---------------------------------------------------------------------------


class TestCalibrationSanity:
    """Lightweight calibration check.

    Issue #3403 acceptance criterion: ratio of current_area to
    estimated_required_area should track empirical routability.  We can't
    run the real router from a unit test, but we can verify the estimate
    produces sensible ratios for known-shape synthetic designs.
    """

    def test_tiny_envelope_below_estimate(self):
        """A 10x10 board with 5 fat ICs is clearly over-constrained."""
        pcb = _make_pcb(
            fps=[_square_footprint(10.0) for _ in range(5)],
            nets={i: _StubNet(name=f"N{i}") for i in range(1, 11)},
        )
        est = estimate_required_area(pcb, MFR_JLCPCB)
        envelope_mm2 = 10.0 * 10.0  # 100 mm^2
        # 5 x 10x10 footprints = 500 mm^2 of pads alone -> total way above 100
        assert est.total_mm2 > envelope_mm2 * 4
        assert envelope_meets_area_estimate(envelope_mm2, est) is False

    def test_large_envelope_meets_estimate(self):
        """A 100x100 board with 5 small SMDs has ample room."""
        pcb = _make_pcb(
            fps=[_square_footprint(3.0) for _ in range(5)],
            nets={i: _StubNet(name=f"N{i}") for i in range(1, 6)},
        )
        est = estimate_required_area(pcb, MFR_JLCPCB)
        envelope_mm2 = 100.0 * 100.0  # 10 000 mm^2
        assert envelope_meets_area_estimate(envelope_mm2, est) is True
        # Ratio should be comfortably above 1.0 for a roomy board.
        ratio = envelope_mm2 / est.total_mm2
        assert ratio > 10.0, f"expected loose board ratio > 10, got {ratio:.2f}"
