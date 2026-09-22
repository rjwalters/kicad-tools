"""Private clearance identities must not become escape copper net IDs."""

import pytest

from kicad_tools.router.escape import PackageType
from kicad_tools.router.subgrid import SubGridRouter
from tests.test_adaptive_grid import make_grid_and_rules, make_pad
from tests.test_escape_pad_clearance_2756 import _make_dense_qfp_pads, _make_router


@pytest.mark.parametrize("kind", list(PackageType))
def test_obstacle_only_package_keeps_geometry_but_emits_no_copper(kind):
    pads = _make_dense_qfp_pads(pins_per_side=4)
    router = _make_router()
    for pad in pads:
        pad.obstacle_only = True
    package = router.analyze_package(pads)
    package.package_type = kind
    before = list(package.pads)
    assert router.generate_escapes(package) == []
    assert router.generate_in_pad_rescues_only(package) == []
    assert package.pads == before
    assert all(pad.net > 0 for pad in pads)


def test_mixed_package_preserves_obstacles_and_signal_escape():
    pads = _make_dense_qfp_pads(pins_per_side=4)
    router = _make_router()
    package = router.analyze_package(pads)
    ordinary = router.generate_escapes(package)
    assert ordinary
    excluded = ordinary[0].pad
    excluded.obstacle_only = True
    escapes = router.generate_escapes(package)
    assert escapes
    assert all(escape.pad is not excluded for escape in escapes)
    assert excluded in package.pads
    # Even a retained pre-policy escape cannot commit copper or overrides.
    old = [escape for escape in ordinary if escape.pad is excluded]
    assert old
    assert router.apply_escape_routes(old) == []
    assert old == []


def test_subgrid_retains_obstacle_in_geometry_without_attempting_escape(monkeypatch):
    grid, rules = make_grid_and_rules()
    pads = [make_pad(5.04, 5.04, 13, "U1", "1"), make_pad(5.54, 5.04, 1, "U1", "2")]
    pads[0].obstacle_only = True
    router = SubGridRouter(grid, rules)
    analysis = router.analyze_pads(pads)
    assert analysis.component_centers["U1"] == pytest.approx((5.29, 5.04))
    assert any(sgp.pad is pads[0] for sgp in analysis.off_grid_pads)
    attempted = []

    def capture(sgp):
        attempted.append(sgp.pad)
        return None, "test"

    monkeypatch.setattr(router, "_find_escape_for_pad", capture)
    result = router.generate_escape_segments(analysis)
    assert attempted == [pads[1]]
    assert result.failed_pads == [pads[1]]


@pytest.mark.parametrize("excluded", [0, 1, 2])
def test_paired_escape_does_not_launch_for_obstacle_only_member(excluded):
    pads = _make_dense_qfp_pads(pins_per_side=4)
    pads[0].net_name, pads[1].net_name = "DP_P", "DP_N"
    router = _make_router()
    router.diff_pair_map = {"DP_P": "DP_N", "DP_N": "DP_P"}
    if excluded in (0, 2):
        pads[0].obstacle_only = True
    if excluded in (1, 2):
        pads[1].obstacle_only = True
    package = router.analyze_package(pads)
    escapes, keys = router._generate_paired_escapes(package)
    assert escapes == []
    assert not keys
