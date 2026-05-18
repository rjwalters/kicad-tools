"""Minimal synthetic fixture for ``--strict-in-pad-clearance`` tests.

Issue #3062: provides a programmatic ``PackageInfo`` plus a foreign-net
neighbour pad placed close enough that an in-pad dead-centre via on the
primary pad clips its neighbour by ~0.05 mm.  This is the failure mode
the strict-mode flag (Issue #3033) exists to defer.

The fixture is intentionally minimal (single primary pad + one
neighbour) and NOT derived from board 04 -- the OSC_OUT-cluster
lateral-recovery dependency that makes board 04 a real customer of the
flag belongs to sub-B (#3063), not to this tests-only sub-issue.

Geometry chosen so that:

* Primary pad: 0.3 x 1.5 mm (LQFP-48-shaped: short axis along the row).
* Foreign neighbour: identical shape, offset along the short axis at
  0.5 mm pitch.  Centre-to-centre = 0.50 mm.
* Via:           0.6 mm diameter dead-centre on the primary pad.
* Clearance:    0.15 mm (board-04 production value).

Distance from via centre to nearest neighbour-pad edge:

    pitch - via_radius - (neighbour_short / 2)
  = 0.50 - 0.30        - 0.15
  = 0.05 mm     -- violates 0.15 mm by 0.10 mm.

The long-axis nudge cannot rescue this because the violation is on the
SHORT axis perpendicular to the nudge direction.  ``_try_in_pad_escape``
must therefore either return None (strict=True) or commit the violating
via with a warning (strict=False, legacy).
"""

from __future__ import annotations

from kicad_tools.router.escape import PackageInfo, PackageType
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# Constants pinned to the geometric reasoning above.  Changing any of
# these without re-deriving the violation magnitude will silently
# weaken the test.
PAD_SHORT: float = 0.30
PAD_LONG: float = 1.50
PITCH: float = 0.50  # centre-to-centre along the short axis
CLEARANCE: float = 0.15
VIA_DRILL: float = 0.30
VIA_DIAMETER: float = 0.60
TRACE_WIDTH: float = 0.20
GRID_RES: float = 0.05


def make_violating_pair(
    ref: str = "U1",
    primary_net: int = 1,
    neighbour_net: int = 2,
) -> tuple[Pad, Pad]:
    """Return ``(primary, neighbour)`` -- two LQFP-shape pads 0.50 mm apart.

    The primary pad sits at ``(0.0, 0.0)`` on F.Cu; the neighbour is
    offset +PITCH along the short axis (Y).  Both pads have unique
    nets so the escape router treats the neighbour as foreign.
    """
    primary = Pad(
        x=0.0,
        y=0.0,
        width=PAD_LONG,  # long axis along X
        height=PAD_SHORT,  # short axis along Y
        net=primary_net,
        net_name=f"NET{primary_net}",
        ref=ref,
        pin="1",
        layer=Layer.F_CU,
    )
    neighbour = Pad(
        x=0.0,
        y=PITCH,
        width=PAD_LONG,
        height=PAD_SHORT,
        net=neighbour_net,
        net_name=f"NET{neighbour_net}",
        ref=ref,
        pin="2",
        layer=Layer.F_CU,
    )
    return primary, neighbour


def make_package(
    ref: str = "U1",
    primary_net: int = 1,
    neighbour_net: int = 2,
) -> PackageInfo:
    """Build a ``PackageInfo`` containing just the violating pair.

    The package_type is set to ``QFP`` and ``pin_pitch`` to the actual
    0.50 mm so callers exercising the QFP-alternating dispatcher's
    pre-conditions see the same classification as a real LQFP-48 pad.
    """
    primary, neighbour = make_violating_pair(
        ref=ref,
        primary_net=primary_net,
        neighbour_net=neighbour_net,
    )
    pads = [primary, neighbour]
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    return PackageInfo(
        ref=ref,
        package_type=PackageType.QFP,
        center=(sum(xs) / len(xs), sum(ys) / len(ys)),
        pads=pads,
        pin_count=len(pads),
        pin_pitch=PITCH,
        bounding_box=(min(xs), min(ys), max(xs), max(ys)),
        is_dense=True,
    )


def make_rules(manufacturer: str | None = "jlcpcb-tier1") -> DesignRules:
    """Build DesignRules with via-in-pad-capable manufacturer.

    The default is ``jlcpcb-tier1`` (Capability+) so
    ``via_in_pad_supported`` evaluates to True; pass ``None`` or
    ``"jlcpcb"`` to exercise the unsupported branch.
    """
    return DesignRules(
        trace_width=TRACE_WIDTH,
        trace_clearance=CLEARANCE,
        via_drill=VIA_DRILL,
        via_diameter=VIA_DIAMETER,
        grid_resolution=GRID_RES,
        manufacturer=manufacturer,
    )


def make_grid(rules: DesignRules) -> RoutingGrid:
    """Build a small 4-layer routing grid centred on the violating pair."""
    return RoutingGrid(
        width=10.0,
        height=10.0,
        rules=rules,
        origin_x=-5.0,
        origin_y=-5.0,
        layer_stack=LayerStack.four_layer_sig_sig_gnd_pwr(),
    )
