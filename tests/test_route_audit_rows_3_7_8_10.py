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
from kicad_tools.router.primitives import Route, Segment, Via
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


def _two_pad_bus_board_text() -> str:
    """Two-pad ``BUS`` net in open space (authored netclass fixture).

    ``BUS`` carries no name-classification width, so only the project's
    ``netclass_assignments`` can say the net is supposed to be wide.
    """
    header = _HEADER.replace("(end 24 28)", "(end 40 40)")
    body = ""
    for i, (pin, x, y) in enumerate((("1", 5.0, 5.0), ("2", 30.0, 30.0))):
        body += (
            f'(pad "{pin}" smd rect (at {x - 10.0:g} {y - 10.0:g}) (size 1 1) '
            f'(layers "F.Cu") (net 1 "BUS") '
            f'(uuid "00000000-0000-0000-0000-00000000001{i + 1}"))\n    '
        )
    return (
        header
        + '(footprint "Test:U1" (layer "F.Cu") (at 10 10)\n'
        + '  (property "Reference" "U1" (at 0 -2) (layer "F.SilkS"))\n'
        + f"  {body})\n"
        + ")\n"
    )


def _wide_bus_project() -> dict:
    """Schema-3 project assigning ``BUS`` to a 0.6 mm ``Wide`` netclass."""
    return {
        "board": {"design_settings": {}},
        "net_settings": {
            "meta": {"version": 3},
            "classes": [
                {
                    "name": "Wide",
                    "clearance": 0.2,
                    "track_width": 0.6,
                    "via_diameter": 0.8,
                    "via_drill": 0.4,
                },
                {
                    "name": "Default",
                    "clearance": 0.2,
                    "track_width": 0.2,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                },
            ],
            "netclass_assignments": {"BUS": "Wide"},
        },
    }


@pytest.mark.parametrize(
    ("command", "extra"),
    [
        (["route", "--layers", "2", "--force"], []),
        (["route-auto", "--net", "BUS"], []),
    ],
    ids=["kct-route", "route-auto"],
)
def test_row3_census_uses_authored_netclass_assignment(tmp_path: Path, capsys, command, extra):
    """Row 3 review: an authored netclass assignment defines the class width.

    The reviewer's case: ``BUS`` assigned to ``Wide`` (0.6 mm) in a
    schema-3 ``.kicad_pro``, emitted copper at the 0.2 mm board default.
    Name classification knows nothing about ``BUS``, so a census that only
    consults the router's class map stays silent -- on both commands.
    """
    from kicad_tools.cli import main as kct_main

    board = tmp_path / "bus_two_pad.kicad_pcb"
    board.write_text(_two_pad_bus_board_text())
    (tmp_path / "bus_two_pad.kicad_pro").write_text(json.dumps(_wide_bus_project()))

    out_path = tmp_path / "out.kicad_pcb"
    rc = kct_main([*command, str(board), "-o", str(out_path), "--format", "json", *extra])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    entries = doc["necked_copper"] if "necked_copper" in doc else doc["nets"][0]["necked_copper"]
    [neck] = entries
    assert neck["net_name"] == "BUS"
    assert neck["min_width_mm"] == pytest.approx(0.2), (
        "the 0.2 mm default-width copper must be measured against the "
        f"authored 0.6 mm Wide class, not ignored: {neck}"
    )
    assert neck["beyond_taper"] is True


def test_row3_class_width_lookup_priority():
    """Authored assignment > classification map > global default."""
    from kicad_tools.router.neck_report import class_width_lookup

    class _NC:
        trace_width = 0.3

    lookup = class_width_lookup({"BUS": _NC()}, 0.2, {"BUS": 0.6})
    assert lookup("BUS") == 0.6
    assert lookup("UNROUTED") == 0.2
    unauthored = class_width_lookup({"BUS": _NC()}, 0.2)
    assert unauthored("BUS") == 0.3


def _necked_run_routes(variant: str) -> list[Route]:
    """Two 0.8 mm necked segments of identical copper, arranged per ``variant``.

    The reviewer's probe geometry: a straight 1.6 mm track whose second
    half is reversed, split across routes, or moved to another layer.
    """

    def seg(x1: float, y1: float, x2: float, y2: float, layer=Layer.F_CU) -> Segment:
        return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.8, layer=layer, net=1, net_name="BUS")

    first = seg(0.0, 0.0, 0.0, 0.8)
    if variant == "straight":
        return [Route(net=1, net_name="BUS", segments=[first, seg(0.0, 0.8, 0.0, 1.6)])]
    if variant == "reversed":
        return [Route(net=1, net_name="BUS", segments=[first, seg(0.0, 1.6, 0.0, 0.8)])]
    if variant == "split-routes":
        return [
            Route(net=1, net_name="BUS", segments=[first]),
            Route(net=1, net_name="BUS", segments=[seg(0.0, 0.8, 0.0, 1.6)]),
        ]
    if variant == "other-layer":
        return [Route(net=1, net_name="BUS", segments=[first, seg(0.0, 0.8, 0.0, 1.6, Layer.B_CU)])]
    if variant == "other-layer-via":
        route = Route(
            net=1,
            net_name="BUS",
            segments=[first, seg(0.0, 0.8, 0.0, 1.6, Layer.B_CU)],
        )
        route.vias.append(
            Via(
                x=0.0,
                y=0.8,
                drill=0.3,
                diameter=0.6,
                layers=(Layer.F_CU, Layer.B_CU),
                net=1,
                net_name="BUS",
            )
        )
        return [route]
    raise ValueError(variant)


class TestRow3ContiguousRunMeasurement:
    """Row 3 review: runs come from the copper geometry, not emission order."""

    @staticmethod
    def _longest_run(routes: list[Route]) -> float:
        from kicad_tools.router.neck_report import collect_necked_copper

        necks = collect_necked_copper(routes, lambda _name: 1.0)
        return necks["BUS"].longest_run_mm

    @pytest.mark.parametrize(
        "variant",
        ["straight", "reversed", "split-routes", "other-layer-via"],
    )
    def test_touching_copper_accumulates_regardless_of_emission(self, variant: str):
        """Identical copper measures identically: one 1.6 mm run.

        Endpoint orientation, the route boundary and a via-joined layer
        change must not split (or join) what is electrically one neck.
        """
        assert self._longest_run(_necked_run_routes(variant)) == pytest.approx(1.6)

    def test_layer_change_without_via_does_not_join(self):
        """Copper on B.Cu with no via is a separate piece: 0.8 mm each."""
        assert self._longest_run(_necked_run_routes("other-layer")) == pytest.approx(0.8)

    def test_beyond_taper_follows_the_run(self):
        """The long-run warning tracks the geometric run in every case."""
        from kicad_tools.router.neck_report import collect_necked_copper, necked_net_to_dict

        for variant in ("straight", "reversed", "split-routes", "other-layer-via"):
            necks = collect_necked_copper(_necked_run_routes(variant), lambda _name: 1.0)
            entry = necked_net_to_dict(necks["BUS"], long_threshold_mm=1.1)
            assert entry["beyond_taper"] is True, variant
        entry = necked_net_to_dict(
            collect_necked_copper(_necked_run_routes("other-layer"), lambda _name: 1.0)["BUS"],
            long_threshold_mm=1.1,
        )
        assert entry["beyond_taper"] is False


def test_row3_authored_netclass_widths_resolution():
    """``authored_netclass_widths`` mirrors the clearance resolver's rules."""
    from kicad_tools.core.project_clearance import authored_netclass_widths

    project = _wide_bus_project()
    resolved = authored_netclass_widths(project, ["BUS", "OTHER"])
    assert list(resolved) == ["BUS"]
    assert resolved["BUS"].width == pytest.approx(0.6)
    assert resolved["BUS"].source_class == "Wide"
    # Pattern-matched membership resolves too, with the same semantics.
    project["net_settings"]["netclass_patterns"] = [{"netclass": "Wide", "pattern": "VBUS*"}]
    resolved = authored_netclass_widths(project, ["VBUS_A", "OTHER"])
    assert resolved["VBUS_A"].width == 0.6
    assert "OTHER" not in resolved  # Default-sourced widths are excluded
    # A project whose named classes declare no track_width resolves to nothing.
    bare = {
        "net_settings": {
            "meta": {"version": 3},
            "classes": [{"name": "Wide", "clearance": 0.2}],
            "netclass_assignments": {"BUS": "Wide"},
        }
    }
    assert authored_netclass_widths(bare, ["BUS"]) == {}


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

    The sealed pad is declared FIRST in the fixture, so the RSMT edge loop
    attempts the IMPOSSIBLE link before any reachable one (#9600 review:
    with the sealed link last, reintroducing the audit's early-stop bug
    passed this test unchanged).  Every link routed after that failure is
    evidence the engine continued; the reachable pads' copper is asserted
    by geometry, not just by route count.  "Report each result" is the
    per-net pad accounting the CLI prints (asserted in the row-10 test)
    plus this failure record.
    """
    from kicad_tools.router.io import load_pcb_for_routing

    router, net_map = load_pcb_for_routing(str(_SEALED_BOARD))
    assert net_map["BUS"] in router.nets
    # Sanity: the fixture orders the sealed pad first, or the
    # impossible-link-before-reachable-links arrangement is gone.
    assert router.nets[net_map["BUS"]][0] == ("U1", "4"), (
        "sealed pad U1.4 must be the first declared pad of the fixture"
    )
    routes = router.route_net(net_map["BUS"])
    assert len(routes) >= 3, (
        f"routing stopped at the first failed link: only {len(routes)} link(s) "
        "routed for a 4-pad net whose impossible link comes first"
    )
    assert router.routing_failures, "the sealed link must be recorded as a failure"

    def _names_pad_four(failure) -> bool:
        return "4" in (failure.source_pad[1] or "") or "4" in (failure.target_pad[1] or "")

    sealed = [f for f in router.routing_failures if _names_pad_four(f)]
    assert sealed, "no failure record names the sealed pad U1.4; recorded: " + "; ".join(
        f"{f.source_pad}->{f.target_pad}" for f in router.routing_failures
    )

    # The reachable pads got their copper: each pad centre (0.5 mm inside
    # its 1 mm pad) is touched by the routed tree's endpoints.
    reachable = {"U1.1": (10.0, 2.0), "U1.2": (2.0, 10.0), "U1.3": (18.0, 10.0)}
    for name, (px, py) in reachable.items():
        nearest = min(
            ((ex - px) ** 2 + (ey - py) ** 2) ** 0.5
            for route in routes
            for seg in route.segments
            for ex, ey in ((seg.x1, seg.y1), (seg.x2, seg.y2))
        )
        assert nearest <= 0.6, (
            f"pad {name} has no routed copper nearer than {nearest:.3f} mm -- "
            "the link to it was abandoned after the sealed pad's failure"
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
    (row 10).  The fixture declares the sealed pad FIRST (#9600 review),
    so the impossible link is attempted before the reachable ones and the
    written copper for the reachable pads is evidence the engine
    continued past that failure -- asserted per pad by geometry.
    """
    import re

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
    # Per-pad: every reachable pad centre is touched by written copper
    # (pad centres are 0.5 mm inside their 1 mm pads; U1 sits at (10, 10)).
    endpoints = [
        (float(m[0]), float(m[1]))
        for m in re.findall(r"\((?:start|end) (-?[\d.]+) (-?[\d.]+)\)", written)
    ]
    assert endpoints, "no segment endpoints parsed from the written board"
    for name, (px, py) in {"U1.1": (10.0, 2.0), "U1.2": (2.0, 10.0), "U1.3": (18.0, 10.0)}.items():
        nearest = min(((x - px) ** 2 + (y - py) ** 2) ** 0.5 for x, y in endpoints)
        assert nearest <= 0.6, (
            f"pad {name} has no written copper nearer than {nearest:.3f} mm -- "
            "its link was abandoned after the sealed pad's failure"
        )


# The sealed pad (U1.4, inside the GND ring) is declared FIRST on purpose
# (#9600 row 8 review): the RSMT edge loop attempts links in pad-declaration
# order, so the IMPOSSIBLE link comes first and every later link is only
# routed if the engine continues past the failure -- exactly the early-stop
# regression this fixture exists to catch.  Keep it first when editing.
_SEALED_BOARD = Path(__file__).parent / "fixtures" / "route_audit_sealed_box.kicad_pcb"
