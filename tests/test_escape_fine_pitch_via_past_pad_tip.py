"""Fine-pitch dual-row escape vias must clear the pad they escape.

``_create_fine_pitch_row_escapes`` places odd-pin vias inward (toward the IC
body) at a clearance-derived offset. That offset is a gap from the pad's
inner tip; it used to be measured from the pad centre, so on a TSSOP whose
pads are longer than twice the offset (1.475 mm pads on a 0.65 mm-pitch
TSSOP-20) the drill landed inside the pad and the escape emitted a via-in-pad
on tiers that do not support one.
"""

from kicad_tools.router.escape import EscapeRouter, PackageType
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.rules import DesignRules

from tests.test_escape_fine_pitch_clearance import make_dual_row_ssop


def _escape_vias(pad_height: float):
    rules = DesignRules(
        trace_width=0.1524,
        trace_clearance=0.1016,
        via_drill=0.2,
        via_diameter=0.45,
        grid_resolution=0.05,
        fine_pitch_clearance=0.14,
        fine_pitch_threshold=0.8,
    )
    grid = RoutingGrid(width=20.0, height=20.0, rules=rules, origin_x=-10.0, origin_y=-10.0)
    router = EscapeRouter(grid, rules, component_holes=())
    pads = make_dual_row_ssop(pin_count=20, pitch=0.65, pad_width=0.4,
                              pad_height=pad_height, row_spacing=5.72)
    package = router.analyze_package(pads)
    assert package.package_type in (PackageType.SSOP, PackageType.TSSOP)
    return rules, [e for e in router.generate_escapes(package) if e.via_pos is not None]


def test_tssop_escape_via_drill_clears_long_pad():
    rules, escapes = _escape_vias(pad_height=1.475)
    assert escapes, "expected odd pins to escape through vias"
    for e in escapes:
        gap = abs(e.via_pos[1] - e.pad.y) - e.pad.height / 2
        assert gap >= rules.via_drill / 2, (
            f"pin {e.pad.pin}: via drill reaches {-(gap - rules.via_drill / 2):.3f} mm into the pad"
        )


def test_escape_via_stays_inward_of_its_pad():
    _, escapes = _escape_vias(pad_height=1.475)
    for e in escapes:
        # Rows sit at y = +/-2.86; inward means toward y = 0.
        assert abs(e.via_pos[1]) < abs(e.pad.y)
