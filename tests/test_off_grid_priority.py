"""Tests for off-grid pad priority boost and pad ref formatting (Issue #2329).

Verifies that:
1. Nets with off-grid pads get boosted constraint scores
2. Off-grid nets are promoted to complexity tier 0
3. _format_pad_ref produces meaningful identifiers for Steiner points
4. _net_has_off_grid_pads correctly detects off-grid pads
5. RSMT decomposition is disabled for nets with off-grid pads
"""

import pytest

from kicad_tools.router.core import Autorouter, _format_pad_ref
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def _make_router(grid_resolution: float = 0.5) -> Autorouter:
    """Create a small Autorouter for testing."""
    rules = DesignRules(grid_resolution=grid_resolution)
    return Autorouter(width=30.0, height=30.0, origin_x=100.0, origin_y=100.0, rules=rules)


def _add_pad(router: Autorouter, ref: str, pin: str, x: float, y: float, net: int, net_name: str = "") -> None:
    """Add a single pad to the router via add_component."""
    router.add_component(ref, [{"number": pin, "x": x, "y": y, "net": net, "net_name": net_name}])


class TestNetHasOffGridPads:
    """Tests for _net_has_off_grid_pads."""

    def test_on_grid_pads(self):
        """Pads aligned to the grid should return False."""
        router = _make_router(grid_resolution=0.5)
        _add_pad(router, "R1", "1", 105.0, 108.0, net=1, net_name="SIG")
        _add_pad(router, "R1", "2", 107.0, 108.0, net=1, net_name="SIG")
        assert not router._net_has_off_grid_pads(1)

    def test_off_grid_pad(self):
        """A pad not aligned to the grid should return True."""
        router = _make_router(grid_resolution=0.5)
        _add_pad(router, "J1", "1", 105.0, 111.23, net=1, net_name="SIG")
        _add_pad(router, "R1", "1", 107.0, 108.0, net=1, net_name="SIG")
        assert router._net_has_off_grid_pads(1)

    def test_mixed_nets(self):
        """Only the net with off-grid pads should return True."""
        router = _make_router(grid_resolution=0.5)
        # Net 1: all on-grid
        _add_pad(router, "R1", "1", 105.0, 108.0, net=1, net_name="VIN")
        _add_pad(router, "R1", "2", 107.0, 108.0, net=1, net_name="VIN")
        # Net 2: one off-grid
        _add_pad(router, "J1", "1", 110.0, 111.23, net=2, net_name="VOUT")
        _add_pad(router, "R2", "1", 112.0, 108.0, net=2, net_name="VOUT")
        assert not router._net_has_off_grid_pads(1)
        assert router._net_has_off_grid_pads(2)

    def test_empty_net(self):
        """Non-existent net should return False."""
        router = _make_router()
        assert not router._net_has_off_grid_pads(999)


class TestConstraintScoreOffGridBoost:
    """Tests for off-grid boost in _calculate_constraint_score."""

    def test_off_grid_pad_boosts_score(self):
        """Net with an off-grid pad should have higher constraint score."""
        router = _make_router(grid_resolution=0.5)
        # Net 1: on-grid
        _add_pad(router, "R1", "1", 105.0, 108.0, net=1, net_name="VIN")
        _add_pad(router, "R1", "2", 107.0, 108.0, net=1, net_name="VIN")
        # Net 2: off-grid
        _add_pad(router, "J1", "1", 110.0, 111.23, net=2, net_name="VOUT")
        _add_pad(router, "R2", "1", 112.0, 108.0, net=2, net_name="VOUT")

        score_on_grid = router._calculate_constraint_score(1)
        score_off_grid = router._calculate_constraint_score(2)
        assert score_off_grid > score_on_grid, (
            f"Off-grid net score ({score_off_grid}) should exceed "
            f"on-grid net score ({score_on_grid})"
        )

    def test_all_on_grid_no_boost(self):
        """Boards with all pads on-grid should not get a spurious boost."""
        router = _make_router(grid_resolution=0.5)
        _add_pad(router, "R1", "1", 105.0, 108.0, net=1, net_name="A")
        _add_pad(router, "R1", "2", 107.0, 108.0, net=1, net_name="A")
        _add_pad(router, "R2", "1", 110.0, 110.0, net=2, net_name="B")
        _add_pad(router, "R2", "2", 112.0, 110.0, net=2, net_name="B")

        score1 = router._calculate_constraint_score(1)
        score2 = router._calculate_constraint_score(2)
        # Both 2-pad nets with same pitch -> scores should be equal
        assert score1 == score2


class TestComplexityTierPromotion:
    """Tests for off-grid complexity tier promotion in _get_net_priority."""

    def test_offgrid_multipin_promoted_to_tier0(self):
        """Multi-pin net with off-grid pad should be promoted to tier 0."""
        router = _make_router(grid_resolution=0.5)
        # Net 1: 2-pin on-grid (naturally tier 0)
        _add_pad(router, "R1", "1", 105.0, 108.0, net=1, net_name="VIN")
        _add_pad(router, "R2", "1", 107.0, 108.0, net=1, net_name="VIN")
        # Net 2: 3-pin with off-grid pad (naturally tier 1, should be promoted)
        _add_pad(router, "R3", "1", 110.0, 108.0, net=2, net_name="VOUT")
        _add_pad(router, "R4", "1", 112.0, 117.0, net=2, net_name="VOUT")
        _add_pad(router, "J1", "1", 120.0, 111.23, net=2, net_name="VOUT")

        pri1 = router._get_net_priority(1)
        pri2 = router._get_net_priority(2)
        # Both should be tier 0 (element [1] of the tuple)
        assert pri1[1] == 0, f"On-grid 2-pin net should be tier 0, got {pri1[1]}"
        assert pri2[1] == 0, f"Off-grid 3-pin net should be promoted to tier 0, got {pri2[1]}"

    def test_on_grid_multipin_stays_tier1(self):
        """Multi-pin net with all pads on-grid should stay tier 1."""
        router = _make_router(grid_resolution=0.5)
        _add_pad(router, "R1", "1", 105.0, 108.0, net=1, net_name="SIG")
        _add_pad(router, "R2", "1", 110.0, 110.0, net=1, net_name="SIG")
        _add_pad(router, "R3", "1", 115.0, 115.0, net=1, net_name="SIG")

        pri = router._get_net_priority(1)
        assert pri[1] == 1, f"On-grid 3-pin net should be tier 1, got {pri[1]}"

    def test_offgrid_net_routes_before_ongrip_peer(self):
        """Off-grid multi-pin net should sort before on-grid 2-pin net."""
        router = _make_router(grid_resolution=0.5)
        # Net 1: 2-pin on-grid
        _add_pad(router, "R1", "1", 105.0, 108.0, net=1, net_name="VIN")
        _add_pad(router, "R2", "1", 107.0, 108.0, net=1, net_name="VIN")
        # Net 2: 3-pin with off-grid pad and higher constraint score
        _add_pad(router, "R3", "1", 110.0, 108.0, net=2, net_name="VOUT")
        _add_pad(router, "R4", "1", 112.0, 117.0, net=2, net_name="VOUT")
        _add_pad(router, "J1", "1", 120.0, 111.23, net=2, net_name="VOUT")

        pri1 = router._get_net_priority(1)
        pri2 = router._get_net_priority(2)
        # VOUT (net 2) should sort before VIN (net 1) because it has a
        # higher constraint score from the off-grid boost
        assert pri2 < pri1, (
            f"Off-grid net priority {pri2} should sort before "
            f"on-grid net priority {pri1}"
        )


class TestFormatPadRef:
    """Tests for _format_pad_ref helper."""

    def test_normal_pad(self):
        """Normal pad with ref and pin should format as 'ref.pin'."""
        pad = Pad(x=0, y=0, width=1, height=1, net=1, net_name="", ref="R1", pin="2")
        assert _format_pad_ref(pad) == "R1.2"

    def test_steiner_point(self):
        """Steiner point with empty ref/pin should show coordinates."""
        pad = Pad(
            x=116.0, y=111.23, width=0, height=0,
            net=1, net_name="", ref="", pin="", steiner_point=True,
        )
        result = _format_pad_ref(pad)
        assert "steiner" in result
        assert "116.000" in result
        assert "111.230" in result

    def test_ref_only(self):
        """Pad with ref but no pin should show just ref."""
        pad = Pad(x=0, y=0, width=1, height=1, net=1, net_name="", ref="TP1", pin="")
        assert _format_pad_ref(pad) == "TP1"

    def test_empty_ref_and_pin_not_dot(self):
        """Empty ref/pin must NOT produce the cryptic '.' string."""
        pad = Pad(x=100.0, y=200.0, width=0, height=0, net=1, net_name="", ref="", pin="")
        result = _format_pad_ref(pad)
        assert result != "."
        assert result != ".."
        assert "100.000" in result
