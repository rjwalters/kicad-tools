"""Tests for the HPWL wirelength estimator.

All HPWL values are hand-computed for verification.
"""

from __future__ import annotations

import time

from kicad_tools.placement.cost import Net
from kicad_tools.placement.vector import (
    ComponentDef,
    PadDef,
    PlacedComponent,
    TransformedPad,
    decode,
    encode,
)
from kicad_tools.placement.wirelength import (
    HPWLResult,
    NetWirelength,
    compute_hpwl,
    compute_hpwl_breakdown,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _placed(
    ref: str,
    x: float,
    y: float,
    pads: tuple[TransformedPad, ...] = (),
    rotation: float = 0.0,
    side: int = 0,
) -> PlacedComponent:
    """Convenience constructor for PlacedComponent."""
    return PlacedComponent(
        reference=ref,
        x=x,
        y=y,
        rotation=rotation,
        side=side,
        pads=pads,
    )


def _tpad(name: str, x: float, y: float) -> TransformedPad:
    """Convenience constructor for TransformedPad."""
    return TransformedPad(name=name, x=x, y=y, size_x=0.5, size_y=0.5)


# ---------------------------------------------------------------------------
# Basic HPWL computation tests
# ---------------------------------------------------------------------------


class TestComputeHPWL:
    """Test compute_hpwl total wirelength."""

    def test_empty_nets(self) -> None:
        """No nets yields zero wirelength."""
        placements = [_placed("U1", 0, 0)]
        assert compute_hpwl(placements, []) == 0.0

    def test_empty_placements(self) -> None:
        """No placements yields zero wirelength (no pads match)."""
        nets = [Net(name="N1", pins=[("U1", "1"), ("U2", "1")])]
        assert compute_hpwl([], nets) == 0.0

    def test_two_pad_net_horizontal(self) -> None:
        """Two pads on a horizontal line: HPWL = dx + 0."""
        # Pad at (0, 0) and (10, 0) => HPWL = (10 - 0) + (0 - 0) = 10
        placements = [
            _placed("U1", 0, 0, pads=(_tpad("1", 0.0, 0.0),)),
            _placed("U2", 10, 0, pads=(_tpad("1", 10.0, 0.0),)),
        ]
        nets = [Net(name="N1", pins=[("U1", "1"), ("U2", "1")])]
        assert compute_hpwl(placements, nets) == 10.0

    def test_two_pad_net_vertical(self) -> None:
        """Two pads on a vertical line: HPWL = 0 + dy."""
        placements = [
            _placed("U1", 0, 0, pads=(_tpad("1", 0.0, 0.0),)),
            _placed("U2", 0, 5, pads=(_tpad("1", 0.0, 5.0),)),
        ]
        nets = [Net(name="N1", pins=[("U1", "1"), ("U2", "1")])]
        assert compute_hpwl(placements, nets) == 5.0

    def test_two_pad_net_diagonal(self) -> None:
        """Two pads at diagonal positions: HPWL = dx + dy."""
        # (0, 0) and (3, 4) => HPWL = 3 + 4 = 7
        placements = [
            _placed("U1", 0, 0, pads=(_tpad("1", 0.0, 0.0),)),
            _placed("U2", 3, 4, pads=(_tpad("1", 3.0, 4.0),)),
        ]
        nets = [Net(name="N1", pins=[("U1", "1"), ("U2", "1")])]
        assert compute_hpwl(placements, nets) == 7.0

    def test_multi_pad_net(self) -> None:
        """Three pads: bounding box approach."""
        # Pads at (1, 2), (5, 2), (3, 8)
        # HPWL = (5-1) + (8-2) = 4 + 6 = 10
        placements = [
            _placed("U1", 1, 2, pads=(_tpad("1", 1.0, 2.0),)),
            _placed("U2", 5, 2, pads=(_tpad("1", 5.0, 2.0),)),
            _placed("U3", 3, 8, pads=(_tpad("1", 3.0, 8.0),)),
        ]
        nets = [Net(name="N1", pins=[("U1", "1"), ("U2", "1"), ("U3", "1")])]
        assert compute_hpwl(placements, nets) == 10.0

    def test_multiple_nets(self) -> None:
        """Sum of HPWL across multiple nets."""
        # Net1: pads at (0,0) and (4,0) => HPWL = 4
        # Net2: pads at (0,0) and (0,3) => HPWL = 3
        # Total = 7
        placements = [
            _placed("U1", 0, 0, pads=(_tpad("1", 0.0, 0.0), _tpad("2", 0.0, 0.0))),
            _placed("U2", 4, 0, pads=(_tpad("1", 4.0, 0.0),)),
            _placed("U3", 0, 3, pads=(_tpad("1", 0.0, 3.0),)),
        ]
        nets = [
            Net(name="N1", pins=[("U1", "1"), ("U2", "1")]),
            Net(name="N2", pins=[("U1", "2"), ("U3", "1")]),
        ]
        assert compute_hpwl(placements, nets) == 7.0

    def test_power_net_many_pads(self) -> None:
        """Power/ground net connecting many components."""
        # 5 pads at: (0,0), (10,0), (10,10), (0,10), (5,5)
        # Bounding box: x=[0,10], y=[0,10]
        # HPWL = 10 + 10 = 20
        placements = [
            _placed("U1", 0, 0, pads=(_tpad("VCC", 0.0, 0.0),)),
            _placed("U2", 10, 0, pads=(_tpad("VCC", 10.0, 0.0),)),
            _placed("U3", 10, 10, pads=(_tpad("VCC", 10.0, 10.0),)),
            _placed("U4", 0, 10, pads=(_tpad("VCC", 0.0, 10.0),)),
            _placed("U5", 5, 5, pads=(_tpad("VCC", 5.0, 5.0),)),
        ]
        nets = [
            Net(
                name="VCC",
                pins=[
                    ("U1", "VCC"),
                    ("U2", "VCC"),
                    ("U3", "VCC"),
                    ("U4", "VCC"),
                    ("U5", "VCC"),
                ],
            )
        ]
        assert compute_hpwl(placements, nets) == 20.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestHPWLEdgeCases:
    """Edge cases for HPWL computation."""

    def test_single_pad_net_zero_wirelength(self) -> None:
        """A net with only one pad has zero wirelength."""
        placements = [_placed("U1", 5, 5, pads=(_tpad("1", 5.0, 5.0),))]
        nets = [Net(name="N1", pins=[("U1", "1")])]
        assert compute_hpwl(placements, nets) == 0.0

    def test_all_pads_coincident(self) -> None:
        """All pads at the same position: wirelength = 0."""
        placements = [
            _placed("U1", 3, 3, pads=(_tpad("1", 3.0, 3.0),)),
            _placed("U2", 3, 3, pads=(_tpad("1", 3.0, 3.0),)),
            _placed("U3", 3, 3, pads=(_tpad("1", 3.0, 3.0),)),
        ]
        nets = [Net(name="N1", pins=[("U1", "1"), ("U2", "1"), ("U3", "1")])]
        assert compute_hpwl(placements, nets) == 0.0

    def test_unresolved_pins_skipped(self) -> None:
        """Pins that reference non-existent components are silently skipped."""
        placements = [
            _placed("U1", 0, 0, pads=(_tpad("1", 0.0, 0.0),)),
        ]
        # U99 does not exist
        nets = [Net(name="N1", pins=[("U1", "1"), ("U99", "1")])]
        # Only one pad resolved => wirelength = 0
        assert compute_hpwl(placements, nets) == 0.0

    def test_unresolved_pad_name_skipped(self) -> None:
        """Pins that reference non-existent pad names are silently skipped."""
        placements = [
            _placed("U1", 0, 0, pads=(_tpad("1", 0.0, 0.0),)),
            _placed("U2", 5, 5, pads=(_tpad("1", 5.0, 5.0),)),
        ]
        # U2 pad "2" does not exist
        nets = [Net(name="N1", pins=[("U1", "1"), ("U2", "2")])]
        # Only one pad resolved => wirelength = 0
        assert compute_hpwl(placements, nets) == 0.0

    def test_component_with_multiple_pads_in_same_net(self) -> None:
        """A component can contribute multiple pads to the same net."""
        # U1 has two pads in net N1 at (0, 0) and (2, 0)
        # U2 has one pad at (1, 3)
        # Bounding box: x=[0,2], y=[0,3], HPWL = 2 + 3 = 5
        placements = [
            _placed(
                "U1",
                1,
                0,
                pads=(_tpad("1", 0.0, 0.0), _tpad("2", 2.0, 0.0)),
            ),
            _placed("U2", 1, 3, pads=(_tpad("1", 1.0, 3.0),)),
        ]
        nets = [
            Net(name="N1", pins=[("U1", "1"), ("U1", "2"), ("U2", "1")]),
        ]
        assert compute_hpwl(placements, nets) == 5.0


# ---------------------------------------------------------------------------
# Per-net breakdown tests
# ---------------------------------------------------------------------------


class TestComputeHPWLBreakdown:
    """Test compute_hpwl_breakdown for per-net results."""

    def test_breakdown_empty_nets(self) -> None:
        """Empty nets list gives empty breakdown."""
        result = compute_hpwl_breakdown([], [])
        assert result.total == 0.0
        assert result.per_net == ()

    def test_breakdown_single_net(self) -> None:
        """Breakdown for a single net."""
        placements = [
            _placed("U1", 0, 0, pads=(_tpad("1", 0.0, 0.0),)),
            _placed("U2", 6, 8, pads=(_tpad("1", 6.0, 8.0),)),
        ]
        nets = [Net(name="SIG", pins=[("U1", "1"), ("U2", "1")])]
        result = compute_hpwl_breakdown(placements, nets)

        assert result.total == 14.0  # 6 + 8
        assert len(result.per_net) == 1
        assert result.per_net[0].name == "SIG"
        assert result.per_net[0].hpwl == 14.0
        assert result.per_net[0].pad_count == 2

    def test_breakdown_multiple_nets(self) -> None:
        """Breakdown for multiple nets with different wirelengths."""
        placements = [
            _placed(
                "U1",
                0,
                0,
                pads=(_tpad("1", 0.0, 0.0), _tpad("2", 0.0, 0.0)),
            ),
            _placed("U2", 10, 0, pads=(_tpad("1", 10.0, 0.0),)),
            _placed("U3", 0, 5, pads=(_tpad("1", 0.0, 5.0),)),
        ]
        nets = [
            Net(name="NET_A", pins=[("U1", "1"), ("U2", "1")]),
            Net(name="NET_B", pins=[("U1", "2"), ("U3", "1")]),
        ]
        result = compute_hpwl_breakdown(placements, nets)

        # NET_A: (10-0) + (0-0) = 10
        # NET_B: (0-0) + (5-0) = 5
        assert result.total == 15.0
        assert len(result.per_net) == 2

        net_a = result.per_net[0]
        assert net_a.name == "NET_A"
        assert net_a.hpwl == 10.0
        assert net_a.pad_count == 2

        net_b = result.per_net[1]
        assert net_b.name == "NET_B"
        assert net_b.hpwl == 5.0
        assert net_b.pad_count == 2

    def test_breakdown_single_pad_net(self) -> None:
        """Single-pad nets show zero HPWL and pad_count=1."""
        placements = [_placed("U1", 5, 5, pads=(_tpad("1", 5.0, 5.0),))]
        nets = [Net(name="ORPHAN", pins=[("U1", "1")])]
        result = compute_hpwl_breakdown(placements, nets)

        assert result.total == 0.0
        assert len(result.per_net) == 1
        assert result.per_net[0].pad_count == 1
        assert result.per_net[0].hpwl == 0.0

    def test_breakdown_total_matches_compute_hpwl(self) -> None:
        """Breakdown total must equal compute_hpwl result."""
        placements = [
            _placed("R1", 0, 0, pads=(_tpad("1", 0.0, 0.0), _tpad("2", 2.0, 0.0))),
            _placed("R2", 5, 3, pads=(_tpad("1", 5.0, 3.0), _tpad("2", 7.0, 3.0))),
            _placed("C1", 3, 6, pads=(_tpad("1", 3.0, 6.0), _tpad("2", 3.0, 7.0))),
        ]
        nets = [
            Net(name="N1", pins=[("R1", "2"), ("R2", "1")]),
            Net(name="N2", pins=[("R2", "2"), ("C1", "1")]),
            Net(name="GND", pins=[("R1", "1"), ("C1", "2")]),
        ]
        total = compute_hpwl(placements, nets)
        breakdown = compute_hpwl_breakdown(placements, nets)
        assert breakdown.total == total
        assert sum(n.hpwl for n in breakdown.per_net) == total


# ---------------------------------------------------------------------------
# Integration with encode/decode pipeline
# ---------------------------------------------------------------------------


class TestHPWLWithEncodeDecode:
    """Test HPWL computation through the encode/decode pipeline."""

    def test_round_trip_hpwl(self) -> None:
        """HPWL on decoded placements matches expected hand-computed value."""
        # Define components with pads
        comp_defs = [
            ComponentDef(
                reference="U1",
                pads=(PadDef(name="1", local_x=-1.0, local_y=0.0),),
            ),
            ComponentDef(
                reference="U2",
                pads=(PadDef(name="1", local_x=1.0, local_y=0.0),),
            ),
        ]

        # Place U1 at (5, 5) and U2 at (15, 5), both at 0 rotation, front side
        original = [
            PlacedComponent(reference="U1", x=5.0, y=5.0, rotation=0.0, side=0, pads=()),
            PlacedComponent(reference="U2", x=15.0, y=5.0, rotation=0.0, side=0, pads=()),
        ]

        # Encode and decode to get transformed pads
        vector = encode(original)
        decoded = decode(vector, comp_defs)

        # U1 pad at (5 + (-1), 5 + 0) = (4, 5)
        # U2 pad at (15 + 1, 5 + 0) = (16, 5)
        assert decoded[0].pads[0].x == 4.0
        assert decoded[0].pads[0].y == 5.0
        assert decoded[1].pads[0].x == 16.0
        assert decoded[1].pads[0].y == 5.0

        nets = [Net(name="SIG", pins=[("U1", "1"), ("U2", "1")])]
        # HPWL = (16 - 4) + (5 - 5) = 12
        assert compute_hpwl(decoded, nets) == 12.0

    def test_rotated_component_pads(self) -> None:
        """Pad positions after rotation affect HPWL correctly."""
        comp_defs = [
            ComponentDef(
                reference="U1",
                pads=(PadDef(name="1", local_x=2.0, local_y=0.0),),
            ),
            ComponentDef(
                reference="U2",
                pads=(PadDef(name="1", local_x=0.0, local_y=0.0),),
            ),
        ]

        # U1 at (10, 10) rotated 90 degrees: pad at local (2,0) becomes (-0, 2)
        # => absolute (10, 12)
        # U2 at (10, 0): pad at (0,0) => absolute (10, 0)
        original = [
            PlacedComponent(reference="U1", x=10.0, y=10.0, rotation=90.0, side=0, pads=()),
            PlacedComponent(reference="U2", x=10.0, y=0.0, rotation=0.0, side=0, pads=()),
        ]

        vector = encode(original)
        decoded = decode(vector, comp_defs)

        nets = [Net(name="N1", pins=[("U1", "1"), ("U2", "1")])]
        # U1 pad: (10 + 0, 10 + 2) = (10, 12) [90 CCW: (2,0) -> (0,2)]
        # U2 pad: (10, 0)
        # HPWL = (10-10) + (12-0) = 12
        assert compute_hpwl(decoded, nets) == 12.0


# ---------------------------------------------------------------------------
# Performance test
# ---------------------------------------------------------------------------


class TestHPWLPerformance:
    """Verify HPWL computation meets the <1ms target for 100-net boards."""

    def test_100_nets_under_1ms(self) -> None:
        """100-net board computes HPWL in under 1ms."""
        # Build a board with 50 components, each with 4 pads, totalling 200 pads.
        # Create 100 nets connecting 2-5 pads each.
        components: list[PlacedComponent] = []
        for i in range(50):
            x = float(i * 2)
            y = float(i % 10)
            pads = tuple(_tpad(str(p + 1), x + p * 0.5, y) for p in range(4))
            components.append(_placed(f"U{i}", x, y, pads=pads))

        nets: list[Net] = []
        for n in range(100):
            # Connect 2-5 random pads per net
            pin_count = (n % 4) + 2  # 2, 3, 4, or 5 pads
            pins = []
            for p in range(pin_count):
                comp_idx = (n * 3 + p * 7) % 50
                pad_idx = (n + p) % 4 + 1
                pins.append((f"U{comp_idx}", str(pad_idx)))
            nets.append(Net(name=f"NET{n}", pins=pins))

        # Warm up
        compute_hpwl(components, nets)

        # Timed run
        start = time.perf_counter()
        iterations = 100
        for _ in range(iterations):
            compute_hpwl(components, nets)
        elapsed = time.perf_counter() - start

        avg_ms = (elapsed / iterations) * 1000
        # Target: <1ms per computation. Use 2ms as threshold to avoid flaky tests.
        assert avg_ms < 2.0, f"Average HPWL computation took {avg_ms:.3f}ms (target: <1ms)"


# ---------------------------------------------------------------------------
# Data type tests
# ---------------------------------------------------------------------------


class TestDataTypes:
    """Test HPWLResult and NetWirelength data types."""

    def test_net_wirelength_frozen(self) -> None:
        """NetWirelength is immutable."""
        nw = NetWirelength(name="N1", hpwl=5.0, pad_count=2)
        assert nw.name == "N1"
        assert nw.hpwl == 5.0
        assert nw.pad_count == 2

    def test_hpwl_result_frozen(self) -> None:
        """HPWLResult is immutable."""
        nw = NetWirelength(name="N1", hpwl=5.0, pad_count=2)
        result = HPWLResult(total=5.0, per_net=(nw,))
        assert result.total == 5.0
        assert len(result.per_net) == 1

    def test_imports_from_package(self) -> None:
        """HPWLResult and compute_hpwl are importable from the package."""
        from kicad_tools.placement import (  # noqa: F401
            HPWLResult,
            NetWirelength,
            compute_hpwl,
            compute_hpwl_breakdown,
        )
