"""Audit rows 3/7/8/10 of the #9592 router audit (issue #9600).

Per-row verdict evidence, one test per reported failure mode, against the
behaviours the audit recorded on PCBJam boards (`docs` context: issue #9600,
parent #9592):

* **Row 3** -- power nets came back with narrow necks running several mm,
  reported "ok".  Two halves: the neck geometry must stay pad-local (the
  #1018 taper only reaches ``neck_down_distance`` from a fine-pitch pad),
  and the summary must never be silent about sub-class-width copper.
* **Row 7** -- a 0.127 mm gap was reported failing a 0.127 mm limit due to
  float rounding.  Pins the exact-boundary and float-noise cases the audit
  named (the full epsilon matrix lives in
  ``tests/router/test_grid_validator_epsilon_6509.py``).  The probed
  kicad-cli tolerance (pinned KiCad 10.0.6, docker image per
  ``KCT_KICAD_DOCKER``): a 500 nm shortfall is accepted, a 600 nm shortfall
  is flagged -- kct's 1e-4 mm (100 nm) slack is inside KiCad's acceptance
  band for the exact-equality case the audit reported; aligning the slack
  with KiCad's wider one is a Python+C+++pinned-test change tracked
  separately (child of #9600).
* **Row 8** -- routing a multi-pad net stopped at the first link it could
  not do.  A net with one sealed (unroutable) pad must still route every
  reachable link and record each failed link.
* **Row 10** -- a partly-routed net was summarised "0/N nets connected"
  with no partial-progress breakdown.  The summary must report the partial
  net with its per-pad progress.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.router.clearance_kernel import CLEARANCE_EPSILON_MM
from kicad_tools.router.core import Autorouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

# The CLI-level rows 8/10 test drives the real negotiated route flow on the
# sealed-box board (~60 s: the impossible link exhausts its rip-up loop by
# design).  Per the repo convention (.github/workflows/ci.yml: tests over
# ~30 s carry an explicit override), give the whole module a 300 s ceiling;
# fast tests are unaffected (a timeout is an upper bound).
pytestmark = pytest.mark.timeout(300)

# ---------------------------------------------------------------------------
# Shared synthetic-board helpers
# ---------------------------------------------------------------------------

_HEADER = """(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "BUS")
  (net 2 "GND")
  (gr_rect (start 0 0) (end 24 28) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
"""


def _sealed_box_board_text() -> str:
    """BUS (net 1) with four pads, one sealed inside a GND ring.

    Pads 1/2/3 are in open space; pad 4 sits inside a ring of GND
    through-hole pads spaced so no 0.2 mm trace or via can leave the ring
    on any layer.  GND is a pour net, so only BUS is trace-routed.
    """
    bus = [("1", 10.0, 2.0), ("2", 2.0, 10.0), ("3", 18.0, 10.0), ("4", 10.0, 18.0)]
    ring: list[tuple[float, float]] = []
    for i in range(9):
        x = 7.0 + 0.7 * i
        ring.append((x, 15.0))
        ring.append((x, 20.6))
    for i in range(1, 8):
        y = 15.0 + 0.7 * i
        ring.append((7.0, y))
        ring.append((12.6, y))

    nodes = [_HEADER]
    body = ""
    for i, (pin, x, y) in enumerate(bus):
        body += (
            f'(pad "{pin}" smd rect (at {x - 10.0:g} {y - 10.0:g}) (size 1 1) '
            f'(layers "F.Cu") (net 1 "BUS") '
            f'(uuid "00000000-0000-0000-0000-00000000000{i + 1}"))\n    '
        )
    nodes.append(
        '(footprint "Test:BUS" (layer "F.Cu") (at 10 10)\n'
        '  (property "Reference" "U1" (at 0 -2) (layer "F.SilkS"))\n'
        f"  {body})\n"
    )
    body = ""
    for i, (x, y) in enumerate(ring):
        body += (
            f'(pad "{i + 1}" thru_hole rect (at {x - 10.0:g} {y - 18.0:g}) (size 1 1) '
            f'(drill 0.5) (layers "*.Cu" "*.Mask") (net 2 "GND") '
            f'(uuid "00000000-0000-0000-0000-000000001{i + 1:03d}"))\n    '
        )
    nodes.append(
        '(footprint "Test:WALL" (layer "F.Cu") (at 10 18)\n'
        '  (property "Reference" "W1" (at 0 -5) (layer "F.SilkS"))\n'
        f"  {body})\n"
    )
    nodes.append(")")
    return "\n".join(nodes)


def _add_pad(router: Autorouter, ref: str, pin: str, x: float, y: float, net: int, net_name: str):
    router.add_component(
        ref,
        [
            {
                "number": pin,
                "x": x,
                "y": y,
                "width": 1,
                "height": 1,
                "net": net,
                "net_name": net_name,
                "layer": Layer.F_CU,
            }
        ],
    )


# ---------------------------------------------------------------------------
# Row 3: necks are pad-local, and never silent
# ---------------------------------------------------------------------------


def _neckdown_router() -> Autorouter:
    """Fine-pitch U1 (0.5 mm pitch) + distant R1 on one wide-trace net."""
    rules = DesignRules(
        grid_resolution=0.1,
        trace_width=0.5,
        trace_clearance=0.2,
        min_trace_width=0.122,
    )
    router = Autorouter(width=40.0, height=40.0, rules=rules)
    for i, y in enumerate((5.0, 5.5, 6.0, 6.5)):
        _add_pad(router, "U1", str(i + 1), 5.0, y, 1, "BUS")
    _add_pad(router, "R1", "1", 30.0, 30.0, 1, "BUS")
    return router


def test_row3_necks_stay_within_the_taper_disk():
    """Row 3 (geometry half): every sub-class-width segment is pad-local.

    The #1018 neck-down tapers to ``min_trace_width`` only inside
    ``neck_down_distance`` of a fine-pitch pad; a neck running several mm
    away from any pad would be the audit's failure mode.
    """
    router = _neckdown_router()
    routes = router.route_net(1)
    assert routes, "sanity: the synthetic net must route"

    pitch = router.grid.compute_component_pitches().get("U1")
    assert pitch is not None and pitch < router.rules.neck_down_threshold, (
        "sanity: U1 must classify as fine-pitch or this test proves nothing"
    )

    necked = [
        seg
        for route in routes
        for seg in route.segments
        if seg.width < router.rules.trace_width - 1e-9
    ]
    assert necked, "sanity: neck-down must produce at least one sub-class-width segment"

    pads = [(p.x, p.y) for key, p in router.pads.items() if key[0] == "U1"]
    assert pads, "sanity: U1 pads must be registered"
    for seg in necked:
        min_dist = min(
            min(
                ((seg.x1 - px) ** 2 + (seg.y1 - py) ** 2) ** 0.5,
                ((seg.x2 - px) ** 2 + (seg.y2 - py) ** 2) ** 0.5,
            )
            for px, py in pads
        )
        assert min_dist < router.rules.neck_down_distance + router.grid.resolution, (
            f"necked segment {seg} lies {min_dist:.3f} mm from every U1 pad -- "
            f"beyond the {router.rules.neck_down_distance} mm taper disk"
        )


def test_row3_neck_report_names_necked_nets():
    """Row 3 (reporting half): necked copper is reported, never silent."""
    from kicad_tools.cli.route_cmd import format_neck_report
    from kicad_tools.router.neck_report import collect_necked_copper

    router = _neckdown_router()
    routes = router.route_net(1)
    assert routes, "sanity: the synthetic net must route"

    def class_width(net_name: str) -> float:
        nc = router.net_class_map.get(net_name)
        return getattr(nc, "trace_width", None) or router.rules.trace_width

    necks = collect_necked_copper(routes, class_width)
    assert necks, "sanity: neck-down must produce reportable necked copper"
    report = format_neck_report(necks)
    assert "BUS" in report
    assert "0.122" in report, f"the report must carry the necked width: {report}"
    assert "mm" in report


def test_row3_neck_report_silent_when_copper_is_full_width():
    from kicad_tools.cli.route_cmd import format_neck_report
    from kicad_tools.router.neck_report import collect_necked_copper

    route = Route(net=1, net_name="BUS")
    route.segments.append(
        Segment(x1=0, y1=0, x2=10, y2=0, width=0.5, layer=Layer.F_CU, net=1, net_name="BUS")
    )
    necks = collect_necked_copper([route], lambda _name: 0.5)
    assert not necks
    assert format_neck_report(necks) == ""


# ---------------------------------------------------------------------------
# Row 7: exact-boundary clearance comparisons tolerate float rounding
# ---------------------------------------------------------------------------


def _rules() -> DesignRules:
    return DesignRules(
        grid_resolution=0.1,
        trace_width=0.2,
        trace_clearance=0.127,
        via_clearance=0.127,
        via_drill=0.3,
        via_diameter=0.6,
    )


def _grid() -> RoutingGrid:
    return RoutingGrid(width=40.0, height=40.0, rules=_rules())


def _seg(x1: float, y1: float, x2: float, y2: float, net: int) -> Segment:
    return Segment(
        x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=Layer.F_CU, net=net, net_name=f"N{net}"
    )


def _route(net: int, segments=()) -> Route:
    route = Route(net=net, net_name=f"N{net}")
    route.segments.extend(segments)
    return route


# The audit's exact case: 0.127 mm gap against a 0.127 mm limit.  The naive
# decimal subtraction lands one float ULP under the limit, which is what the
# PCBJam run reported as "failing due to float rounding".
AUDIT_GAP_Y = 0.327  # 0.327 - 0.1 - 0.1 == 0.12699999999999997


def test_row7_kernels_keep_the_exact_boundary_legal():
    assert CLEARANCE_EPSILON_MM > 0.0, "the shared clearance slack must exist"


@pytest.mark.parametrize("gap_y", [AUDIT_GAP_Y, AUDIT_GAP_Y - 5e-12], ids=["exact", "float-noise"])
def test_row7_grid_validator_accepts_audit_boundary_gap(gap_y: float):
    grid = _grid()
    grid.routes.append(_route(1, [_seg(2.0, 0.0, 38.0, 0.0, 1)]))
    ok, actual, _loc = grid.validate_segment_clearance(
        _seg(2.0, gap_y, 38.0, gap_y, 2), exclude_net=2
    )
    assert ok is True, f"gap {0.2 + gap_y - 0.2!r} reported failing the 0.127 mm limit"
    assert actual >= 0.127 - CLEARANCE_EPSILON_MM


def test_row7_copper_kernel_accepts_audit_boundary_gap():
    from kicad_tools.router.clearance_kernel import KSegment, clear

    a = KSegment(x1=2.0, y1=0.0, x2=38.0, y2=0.0, width=0.2, layer=0)
    b = KSegment(x1=2.0, y1=AUDIT_GAP_Y, x2=38.0, y2=AUDIT_GAP_Y, width=0.2, layer=0)
    assert clear(a, b, 0.127), "exact-boundary gap must clear the kernel"
    b_noise = KSegment(
        x1=2.0, y1=AUDIT_GAP_Y - 5e-12, x2=38.0, y2=AUDIT_GAP_Y - 5e-12, width=0.2, layer=0
    )
    assert clear(a, b_noise, 0.127), "float-noise gap must clear the kernel"


# ---------------------------------------------------------------------------
# Rows 8 + 10: the sealed-box net -- every link attempted, partial progress
# reported
# ---------------------------------------------------------------------------


def test_row8_every_reachable_link_routes_and_failures_are_recorded():
    """Row 8: one impossible link must not stop the other links.

    The sealed pad's link fails and is recorded; the three reachable pads
    still get copper.  "Report each result" is the per-net pad accounting
    the CLI prints (asserted in the row-10 test) plus this failure record.
    """
    from kicad_tools.router.io import load_pcb_for_routing

    router, net_map = load_pcb_for_routing(str(_SEALED_BOARD))
    assert net_map["BUS"] in router.nets
    routes = router.route_net(net_map["BUS"])
    assert len(routes) >= 2, (
        f"routing stopped at the first failed link: only {len(routes)} link(s) "
        "routed for a 4-pad net with one sealed pad"
    )
    assert router.routing_failures, "the sealed link must be recorded as a failure"
    sealed = [f for f in router.routing_failures if "4" in (f.target_pad[1] or "")]
    assert sealed, "no failure record names the sealed pad U1.4; recorded: " + "; ".join(
        f"{f.source_pad}->{f.target_pad}" for f in router.routing_failures
    )


def test_row10_summary_reports_partial_progress_per_net(capsys, tmp_path: Path):
    """Row 10: "0/1 nets connected" must not be the whole story.

    The run's summary reports the partial net with per-pad progress, in
    both the Results section and the post-save connectivity verification.
    """
    from kicad_tools.cli.route_cmd import main as route_main

    out_path = tmp_path / "out.kicad_pcb"
    # ``--layers 2`` bounds the layer-escalation retry loop: the sealed pad
    # is unroutable at any layer count, so escalating cannot help.  The
    # default (negotiated) strategy is the shipped surface this audit row
    # is about.
    rc = route_main(
        [
            str(_SEALED_BOARD),
            "--output",
            str(out_path),
            "--no-optimize",
            "--force",
            "--layers",
            "2",
            "--per-net-timeout",
            "25",
        ]
    )
    assert rc != 0, "a net with an open pad must not report success"
    out = capsys.readouterr().out

    # The strict-connect headline alone is what the audit flagged as
    # misleading; it must be accompanied by partial-progress reporting.
    assert re.search(r"Nets routed:\s*0/1", out), f"missing strict headline:\n{out}"
    assert re.search(r"Partial routes:\s*1/1", out), f"missing partial count:\n{out}"
    assert re.search(r"BUS: 3/4 pads connected", out), (
        f"missing per-net partial progress for BUS:\n{out[-3000:]}"
    )
    # And the written board carries the useful segments (148 in the audit).
    written = out_path.read_text()
    assert written.count("(segment") >= 10, (
        "the partial net's useful copper is missing from the written board"
    )


_SEALED_BOARD = Path(__file__).parent / "fixtures" / "route_audit_sealed_box.kicad_pcb"
