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

``route-auto`` verdicts (the orchestrator path -- ``RoutingOrchestrator``
via ``EscapeRouter``/``AdaptiveAutorouter`` -- never calls the CLI's
``_finalize_routes``, so rows 3/8/10 are asserted on it separately, as
#9600 asks "for ``kct route`` and separately for ``route-auto``"):

* **Row 3** -- ``route-auto`` CAN emit sub-class-width copper (its global
  corridor runs at the board default width, not the net's class width)
  and must not be silent about it:
  ``test_row3_route_auto_reports_necked_copper``.
* **Rows 8 + 10** -- the orchestrator's per-pad completion verification
  reports the honest k/n and persists the reachable links' copper:
  ``test_rows_8_10_route_auto_partial_progress_is_reported``.
"""

from __future__ import annotations

import json
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

    def _nearest_pad_dist(x: float, y: float) -> float:
        return min(((x - px) ** 2 + (y - py) ** 2) ** 0.5 for px, py in pads)

    for seg in necked:
        # The FAR endpoint: a 0.122 mm segment that merely *starts* on a U1
        # pad and runs 5 mm away must fail this check, not pass because one
        # endpoint sits on the pad (the audit's several-mm neck had an
        # on-pad endpoint -- min() over both endpoints cannot see it).
        far_dist = max(_nearest_pad_dist(seg.x1, seg.y1), _nearest_pad_dist(seg.x2, seg.y2))
        assert far_dist < router.rules.neck_down_distance + router.grid.resolution, (
            f"necked segment {seg} reaches {far_dist:.3f} mm from every U1 pad -- "
            f"beyond the {router.rules.neck_down_distance} mm taper disk"
        )


def test_row3_neck_report_names_necked_nets():
    """Row 3 (reporting half): necked copper is reported, never silent."""
    from kicad_tools.router.neck_report import collect_necked_copper, format_neck_report

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
    from kicad_tools.router.neck_report import collect_necked_copper, format_neck_report

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


# ---------------------------------------------------------------------------
# route-auto: the orchestrator path carries its own row 3 / 8 / 10 verdicts
# ---------------------------------------------------------------------------


def _high_current_board_text() -> str:
    """Two-pad ``PHASE_A`` net in open space (route-auto neck fixture).

    ``PHASE_A`` auto-classifies as HIGH_CURRENT_SIGNAL (0.4 mm class
    width) while the board declares no track constraints, so the rules
    default to a 0.2 mm trace width -- route-auto's global corridor emits
    the net at 0.2 mm, i.e. half its class width: real necked copper.
    """
    header = _HEADER.replace('(net 1 "BUS")\n  (net 2 "GND")', '(net 1 "PHASE_A")')
    header = header.replace("(end 24 28)", "(end 40 40)")
    body = ""
    for i, (pin, x, y) in enumerate((("1", 5.0, 5.0), ("2", 30.0, 30.0))):
        body += (
            f'(pad "{pin}" smd rect (at {x - 10.0:g} {y - 10.0:g}) (size 1 1) '
            f'(layers "F.Cu") (net 1 "PHASE_A") '
            f'(uuid "00000000-0000-0000-0000-00000000000{i + 1}"))\n    '
        )
    return (
        header
        + '(footprint "Test:U1" (layer "F.Cu") (at 10 10)\n'
        + '  (property "Reference" "U1" (at 0 -2) (layer "F.SilkS"))\n'
        + f"  {body})\n"
        + ")\n"
    )


def test_row3_route_auto_reports_necked_copper(tmp_path: Path, capsys):
    """Row 3 on route-auto: sub-class-width copper is reported, not silent.

    The orchestrator never calls the CLI's ``_finalize_routes`` (where
    ``kct route`` prints its neck report), so without this the route-auto
    surface can emit necked copper and say nothing -- the audit's failure
    mode, on the second code path.  Asserted on both surfaces route-auto
    owns: the ``--format json`` document and the text report.
    """
    from kicad_tools.cli import main as kct_main
    from kicad_tools.router.net_class import classify_and_apply_rules

    board = tmp_path / "high_current.kicad_pcb"
    board.write_text(_high_current_board_text())

    # Sanity: the fixture's class width really is wider than the board
    # default, or "necked" would be vacuous.
    class_width = classify_and_apply_rules({1: "PHASE_A"})["PHASE_A"].trace_width
    assert class_width > 0.3, f"PHASE_A must classify wide, got {class_width}"

    # JSON surface: the per-net document carries the neck census.
    out_json = tmp_path / "out_json.kicad_pcb"
    rc = kct_main(
        ["route-auto", str(board), "--net", "PHASE_A", "-o", str(out_json), "--format", "json"]
    )
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    [entry] = doc["nets"]
    assert entry["success"] is True
    [neck] = entry["necked_copper"]
    assert neck["net_name"] == "PHASE_A"
    assert neck["min_width_mm"] == pytest.approx(0.2), (
        "the corridor runs at the 0.2 mm board default, below the 0.4 mm class width"
    )
    assert neck["segment_count"] >= 1
    # A 25 mm corridor necked along its whole length is far beyond the
    # 1.0 mm taper distance: the entry says so.
    assert neck["beyond_taper"] is True

    # Text surface: the same census, rendered in the run's report.
    out_text = tmp_path / "out_text.kicad_pcb"
    rc = kct_main(["route-auto", str(board), "--net", "PHASE_A", "-o", str(out_text)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Necked copper:" in out, f"route-auto text output is silent about necks:\n{out}"
    assert "PHASE_A" in out and "long, verify intent" in out


def test_rows_8_10_route_auto_partial_progress_is_reported(tmp_path: Path):
    """Rows 8 + 10 on route-auto: honest k/n, reachable links keep copper.

    The orchestrator's per-pad completion verification (not the
    strategy's self-reported status) decides the verdict: one sealed pad
    must not stop the reachable links from routing (row 8), and the
    partial net is reported with per-pad progress, never a bare failure
    (row 10).
    """
    from kicad_tools.mcp.tools.routing import route_net_auto

    out_path = tmp_path / "out.kicad_pcb"
    result = route_net_auto(
        pcb_path=str(_SEALED_BOARD),
        net_name="BUS",
        output_path=str(out_path),
        allow_partial=True,
    )
    assert result["success"] is False, "a net with an open pad must not report success"
    assert result["partial"] is True, "copper plus a stranded pad is partial, not failed"
    assert result["pads_connected"] == 3, result.get("error_message")
    assert result["pads_total"] == 4
    written = out_path.read_text()
    assert written.count("(segment") >= 10, (
        "the reachable links' copper is missing from the written board"
    )


_SEALED_BOARD = Path(__file__).parent / "fixtures" / "route_audit_sealed_box.kicad_pcb"
