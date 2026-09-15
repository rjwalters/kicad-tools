"""Physical grouping and authored clearance policy remain separate."""

from unittest.mock import Mock

import pytest

from kicad_tools.router.adaptive_grid import AdaptiveGridRouter, FinePitchEscapeFailure
from kicad_tools.router.subgrid import SubGridResult, SubGridRouter
from tests.test_adaptive_grid import make_grid_and_rules, make_pad
from tests.test_escape_pad_clearance_2756 import _make_dense_qfp_pads, _make_router


def _pads(ref):
    pads = []
    for index, origin in enumerate((5, 15)):
        for pin, offset in enumerate((0.04, 0.54)):
            pad = make_pad(origin + offset, 5.04, pin + 1, ref, str(pin))
            pad.component_id = f"physical-{index}"
            pads.append(pad)
    return pads


@pytest.mark.parametrize("ref", ["DUP", ""])
def test_subgrid_centers_and_adaptive_filter_use_each_physical_component(ref):
    grid, rules = make_grid_and_rules()
    pads = _pads(ref)
    subgrid = SubGridRouter(grid, rules)
    analysis = subgrid.analyze_pads(pads)
    assert analysis.component_centers == {
        "physical-0": pytest.approx((5.29, 5.04)),
        "physical-1": pytest.approx((15.29, 5.04)),
    }
    assert [p.escape_direction[0] for p in analysis.off_grid_pads] == [-1, 1, -1, 1]
    adaptive = AdaptiveGridRouter(grid, rules)
    adaptive._subgrid.route_with_subgrid = Mock(return_value=SubGridResult())
    adaptive._subgrid.get_escape_routes = Mock(return_value=[])
    _, _, fine = adaptive._phase1_pad_escape({pad.key: pad for pad in pads})
    assert set(fine) == {"physical-0", "physical-1"}
    assert adaptive._subgrid.route_with_subgrid.call_args.args[0] == pads
    # A successful duplicate must not mask its physically distinct sibling's
    # total failure, despite their identical authored reference.
    result = SubGridResult(analysis=analysis, failed_pads=pads[:2])
    with pytest.raises(FinePitchEscapeFailure) as failure:
        adaptive._raise_if_component_fully_failed(result, fine)
    assert failure.value.component_ref == ref


def test_subgrid_pitch_extends_candidates_for_physical_duplicate():
    grid, rules = make_grid_and_rules()
    pads = _pads("DUP")
    for pad in pads:
        grid.add_pad(pad)
    subgrid = SubGridRouter(grid, rules)
    sgp = subgrid.analyze_pads(pads).off_grid_pads[0]
    candidates = subgrid._generate_fine_grid_candidates(sgp, 0.05)
    assert candidates
    # Half-millimetre pitch requires searching beyond the ordinary 0.3mm
    # radius to reach outside this package's tight pad corridor.
    assert max(abs(candidate[3] - sgp.snap_x) for candidate in candidates) > 0.3


@pytest.mark.parametrize(
    "method", ["_escape_qfp_alternating", "generate_in_pad_rescues_only", "_escape_radial"]
)
def test_escape_geometry_looks_up_authored_clearance_for_physical_package(monkeypatch, method):
    pads = _make_dense_qfp_pads(pins_per_side=4, ref="DUP")
    for pad in pads:
        pad.component_id = "physical-duplicate"
    router = _make_router()
    router.via_in_pad_supported = True
    router.rules.component_clearances["DUP"] = 0.19
    package = router.analyze_package(pads)
    assert package.ref == "physical-duplicate"
    original = type(router.rules).get_clearance_for_component
    calls = []

    def checked(self, ref, *args, **kwargs):
        value = original(self, ref, *args, **kwargs)
        calls.append((ref, value))
        return value

    monkeypatch.setattr(type(router.rules), "get_clearance_for_component", checked)
    getattr(router, method)(package)
    assert calls
    assert all(ref == "DUP" and value == 0.19 for ref, value in calls)
