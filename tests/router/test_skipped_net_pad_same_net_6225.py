"""Preserved skipped-net copper vs its own pad is not a short (issue #6225).

Background
----------
``kct route boards/06-diffpair-test/output/diffpair_test.kicad_pcb
--preserve-existing`` exited 3 ("NOT manufacturable as written") with 16
``[pad]`` shorts of exactly 0.075 mm, e.g. ``Net 1 vs +3V3 at (31.525,
22.095)``, while kicad-cli DRC and ``kct check`` reported 0 shorts.

The flagged pairs were the board generator's power fanout: a 0.6 mm
+3V3 via at (30.325, 22.095) next to U1 pad 1 (+3V3, 1.95 x 0.6 mm
roundrect centred at (31.525, 22.095)).  The pad's west edge sits at
31.525 - 1.95/2 = 30.55 and the via's east edge at 30.325 + 0.3 = 30.625,
so the copper genuinely overlaps by 0.075 mm -- but both are the SAME net.

``load_pcb_for_routing`` rewrites the net id of every pad on a skipped
pour net (GND, +3V3) to ``0`` while keeping ``net_name``; the preserved
via keeps its real board id (1).  ``validate_routes`` skipped same-net
pads by id only, so ``0 != 1`` made the pad look foreign.  The fix
treats a ``net == 0`` pad as same-net when its name matches the route's
net name.  Name matching is restricted to skipped-net pads, so a real
cross-net overlap is still reported.
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import shorting_violations, validate_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

# Geometry lifted from boards/06-diffpair-test U1 pad 1 and its fanout via.
PAD_X, PAD_Y = 31.525, 22.095
PAD_W, PAD_H = 1.95, 0.6
VIA_X = 30.325
VIA_D = 0.6


def _router() -> Autorouter:
    return Autorouter(
        width=80,
        height=80,
        rules=DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1),
    )


def _skipped_net_pad(name: str = "+3V3", ref: str = "U1", pin: str = "1") -> Pad:
    """A pad on a skipped pour net, as ``load_pcb_for_routing`` leaves it."""
    return Pad(
        x=PAD_X,
        y=PAD_Y,
        width=PAD_W,
        height=PAD_H,
        net=0,
        net_name=name,
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
    )


def _fanout(net: int, name: str) -> Route:
    """Preserved fanout: stub from pad centre to an overlapping via."""
    return Route(
        net=net,
        net_name=name,
        segments=[
            Segment(x1=PAD_X, y1=PAD_Y, x2=VIA_X, y2=PAD_Y, layer=Layer.F_CU, width=0.3, net=net)
        ],
        vias=[
            Via(
                x=VIA_X,
                y=PAD_Y,
                drill=0.3,
                diameter=VIA_D,
                layers=(Layer.F_CU, Layer.B_CU),
                net=net,
            )
        ],
    )


class TestSkippedNetSameNetIsNotAShort:
    def test_board06_geometry_overlaps(self) -> None:
        """Sanity: the reproduced shape really does overlap by 0.075 mm."""
        overlap = (VIA_X + VIA_D / 2) - (PAD_X - PAD_W / 2)
        assert abs(overlap - 0.075) < 1e-9

    def test_preserved_via_on_own_skipped_net_pad(self) -> None:
        router = _router()
        router.pads[("U1", "1")] = _skipped_net_pad("+3V3")
        router.existing_routes.append(_fanout(1, "+3V3"))

        violations = validate_routes(router)

        assert shorting_violations(violations) == [], (
            "A +3V3 via touching a +3V3 pad is same-net copper; the pad's "
            "net id being rewritten to 0 (skipped pour net) must not make "
            "it look foreign (issue #6225)."
        )
        assert [v for v in violations if v.obstacle_type == "pad"] == []

    def test_routed_segment_on_own_skipped_net_pad(self) -> None:
        """Segment-to-pad quadrant uses the same same-net predicate."""
        router = _router()
        router.pads[("U1", "1")] = _skipped_net_pad("GND")
        router.routes.append(_fanout(2, "GND"))

        violations = validate_routes(router)

        assert [v for v in violations if v.obstacle_type == "pad"] == []

    def test_name_resolved_from_router_net_names(self) -> None:
        """A route without its own name falls back to ``router.net_names``."""
        router = _router()
        router.pads[("U1", "1")] = _skipped_net_pad("GND")
        router.net_names[2] = "GND"
        router.existing_routes.append(_fanout(2, ""))

        assert shorting_violations(validate_routes(router)) == []


class TestRealCrossNetShortsStillFlagged:
    def test_preserved_via_on_foreign_skipped_net_pad(self) -> None:
        """Same geometry, but the via is GND and the pad +3V3: a real short."""
        router = _router()
        router.pads[("U1", "1")] = _skipped_net_pad("+3V3")
        router.existing_routes.append(_fanout(2, "GND"))

        shorts = shorting_violations(validate_routes(router))

        assert shorts, "GND copper overlapping a +3V3 pad is a genuine short"
        via_short = [v for v in shorts if v.segment_index == -1]
        assert len(via_short) == 1
        assert abs(-via_short[0].distance - 0.075) < 1e-6
        assert via_short[0].net_name == "GND"
        assert via_short[0].obstacle_net_name == "+3V3"

    def test_routed_trace_through_foreign_skipped_net_pad(self) -> None:
        """A newly routed signal trace across a GND pad is still a short."""
        router = _router()
        router.pads[("U1", "1")] = _skipped_net_pad("GND")
        router.net_names[5] = "IN1"
        router.routes.append(
            Route(
                net=5,
                net_name="IN1",
                segments=[
                    Segment(
                        x1=PAD_X - 3.0,
                        y1=PAD_Y,
                        x2=PAD_X + 3.0,
                        y2=PAD_Y,
                        layer=Layer.F_CU,
                        width=0.2,
                        net=5,
                    )
                ],
                vias=[],
            )
        )

        shorts = shorting_violations(validate_routes(router))

        assert len(shorts) == 1
        assert shorts[0].obstacle_type == "pad"
        assert shorts[0].obstacle_net_name == "GND"

    def test_routed_trace_through_foreign_regular_pad(self) -> None:
        """Ordinary (non-skipped) cross-net pad overlap is unaffected."""
        router = _router()
        router.pads[("U1", "1")] = Pad(
            x=PAD_X, y=PAD_Y, width=PAD_W, height=PAD_H, net=7, net_name="OUT1", ref="U1", pin="1"
        )
        router.net_names.update({5: "IN1", 7: "OUT1"})
        router.routes.append(_fanout(5, "IN1"))

        shorts = shorting_violations(validate_routes(router))

        assert {v.segment_index for v in shorts} == {-1, 0}
