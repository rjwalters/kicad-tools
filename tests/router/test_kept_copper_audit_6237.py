"""Kept input copper vs kept copper, and kept-copper clearance (Issue #6237).

Background
----------
Issue #6229 taught ``kct route --preserve-existing``'s post-route check to
compare copper kept from the input board against other nets' PADS, for
shorts only.  Two gaps remained, each reproduced with ``kicad-cli pcb drc``:

* kept copper was never compared with other nets' KEPT copper, so two kept
  traces of different nets crossing (or a kept trace through a kept via, or
  two overlapping kept vias) printed SUCCESS while kicad-cli reported
  ``tracks_crossing`` / ``shorting_items``;
* a kept trace 0.075 mm from a foreign pad passed, while kicad-cli reported
  ``clearance``.

Shorts are decided in :func:`validate_routes` (``audit_kept_copper=True``).
Near-misses are :func:`kept_copper_clearance_violations`, judged with the
shared clearance kernel under the rules the WRITTEN board ships with, and
gated by its project's ``clearance`` rule severity.
"""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import (
    _audit_kept_copper_clearance,
    _print_short_failure_banner,
)
from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.router.board_clearance_rules import BoardClearanceRules
from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import (
    ClearanceViolation,
    describe_input_clearance,
    describe_short,
    kept_copper_clearance_violations,
    kicad_rule_severity,
    shorting_violations,
    validate_routes,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def _router() -> Autorouter:
    return Autorouter(
        width=40,
        height=40,
        rules=DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1),
    )


def _trace(
    net: int,
    name: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    layer: Layer = Layer.F_CU,
    width: float = 0.25,
) -> Route:
    return Route(
        net=net,
        net_name=name,
        segments=[Segment(x1=x1, y1=y1, x2=x2, y2=y2, layer=layer, width=width, net=net)],
        vias=[],
    )


def _via(net: int, name: str, x: float, y: float, *, diameter: float = 0.6) -> Route:
    return Route(
        net=net,
        net_name=name,
        segments=[],
        vias=[
            Via(
                x=x,
                y=y,
                drill=0.3,
                diameter=diameter,
                layers=(Layer.F_CU, Layer.B_CU),
                net=net,
            )
        ],
    )


def _pad(net: int, name: str, x: float, y: float, *, ref: str = "J3") -> Pad:
    return Pad(
        x=x, y=y, width=1.0, height=1.0, net=net, net_name=name, layer=Layer.F_CU, ref=ref, pin="1"
    )


def _kept_shorts(router: Autorouter) -> list[ClearanceViolation]:
    return shorting_violations(validate_routes(router, audit_kept_copper=True))


# Horizontal +3V3 trace along y=10, x 5..19 (edges at y=9.875 / 10.125).
def _v3v3() -> Route:
    return _trace(1, "+3V3", 5.0, 10.0, 19.0, 10.0)


class TestKeptVsKeptShorts:
    def test_trace_crossing_trace_is_an_input_short(self) -> None:
        router = _router()
        router.existing_routes += [_v3v3(), _trace(2, "GND", 12.0, 5.0, 12.0, 15.0)]

        shorts = _kept_shorts(router)

        assert len(shorts) == 1, "reported once per pair, not once per side"
        short = shorts[0]
        assert short.origin == "input"
        assert short.obstacle_type == "segment"
        assert short.segment_index == 0
        assert {short.net_name, short.obstacle_net_name} == {"+3V3", "GND"}
        assert short.layer == Layer.F_CU
        text = describe_short(short)
        assert text.startswith("pre-existing short in input board (kept by --preserve-existing): ")
        assert " trace vs " in text and text.endswith("overlap 0.250mm")

    def test_traces_on_different_layers_do_not_short(self) -> None:
        router = _router()
        router.existing_routes += [
            _v3v3(),
            _trace(2, "GND", 12.0, 5.0, 12.0, 15.0, layer=Layer.B_CU),
        ]

        assert _kept_shorts(router) == []

    def test_trace_through_via_is_an_input_short(self) -> None:
        router = _router()
        # Via first, so the trace must still be the reported copper.
        router.existing_routes += [_via(2, "GND", 12.0, 10.3), _v3v3()]

        shorts = _kept_shorts(router)

        assert len(shorts) == 1
        short = shorts[0]
        assert short.obstacle_type == "via"
        assert short.segment_index == 0, "segment-vs-via contract: the trace is the copper"
        assert (short.net_name, short.obstacle_net_name) == ("+3V3", "GND")
        assert abs(-short.distance - 0.125) < 1e-9
        assert "+3V3 trace vs GND via" in describe_short(short)

    def test_via_overlapping_via_is_an_input_short(self) -> None:
        router = _router()
        router.existing_routes += [_via(1, "+3V3", 8.0, 20.0), _via(2, "GND", 8.4, 20.0)]

        shorts = _kept_shorts(router)

        assert len(shorts) == 1
        short = shorts[0]
        assert short.obstacle_type == "via"
        assert short.segment_index == -1
        assert short.x1 == short.x2 and short.y1 == short.y2
        assert abs(-short.distance - 0.2) < 1e-9
        assert "+3V3 via vs GND via" in describe_short(short)

    def test_same_net_kept_copper_is_never_flagged(self) -> None:
        router = _router()
        router.existing_routes += [
            _v3v3(),
            _trace(1, "+3V3", 12.0, 5.0, 12.0, 15.0),
            _via(1, "+3V3", 12.0, 10.0),
            _via(1, "+3V3", 12.2, 10.0),
        ]

        assert _kept_shorts(router) == []
        assert kept_copper_clearance_violations(router, board_rules=BoardClearanceRules()) == []

    def test_same_net_by_name_across_ids_is_never_flagged(self) -> None:
        """A kept skipped-pour-net copper id may differ; the name decides (#6225)."""
        router = _router()
        router.existing_routes += [_v3v3(), _trace(7, "+3V3", 12.0, 5.0, 12.0, 15.0)]

        assert _kept_shorts(router) == []

    def test_netless_kept_copper_is_skipped(self) -> None:
        router = _router()
        router.existing_routes += [_v3v3(), _trace(0, "", 12.0, 5.0, 12.0, 15.0)]

        assert _kept_shorts(router) == []

    def test_kept_vs_kept_needs_the_audit_flag(self) -> None:
        """In-router repair loops (default call) never see kept-vs-kept pairs."""
        router = _router()
        router.existing_routes += [_v3v3(), _trace(2, "GND", 12.0, 5.0, 12.0, 15.0)]

        assert shorting_violations(validate_routes(router)) == []

    def test_rerouted_net_stale_copper_is_not_audited(self) -> None:
        router = _router()
        router.existing_routes += [_v3v3(), _trace(2, "GND", 12.0, 5.0, 12.0, 15.0)]
        router.routes.append(_trace(2, "GND", 30.0, 5.0, 30.0, 15.0))

        assert _kept_shorts(router) == []


class TestKeptClearanceNearMisses:
    def test_kept_trace_near_foreign_pad_is_flagged(self) -> None:
        """The motivating case: 0.075 mm edge gap, 0.2 mm clearance."""
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND", 12.0, 10.7)  # pad edge at y=10.2
        router.existing_routes.append(_v3v3())

        near = kept_copper_clearance_violations(router, board_rules=BoardClearanceRules())

        assert len(near) == 1
        v = near[0]
        assert v.origin == "input" and not v.is_short
        assert v.obstacle_type == "pad" and v.obstacle_pad == "J3.1"
        assert abs(v.distance - 0.075) < 1e-9
        assert v.required == pytest.approx(0.2)
        assert describe_input_clearance(v) == (
            "pre-existing clearance violation in input board (kept by --preserve-existing): "
            "+3V3 trace vs GND pad J3.1 at (12.000, 10.700) on F.Cu: "
            "gap 0.075 < required 0.200 mm"
        )
        # The post-route short check itself never reports a kept near-miss.
        assert [
            v for v in validate_routes(router, audit_kept_copper=True) if v.origin == "input"
        ] == []

    @pytest.mark.parametrize("gap", [0.2, 0.25])
    def test_gap_at_or_above_clearance_is_not_flagged(self, gap: float) -> None:
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND", 12.0, 10.125 + gap + 0.5)
        router.existing_routes.append(_v3v3())

        assert kept_copper_clearance_violations(router, board_rules=BoardClearanceRules()) == []

    def test_requirement_follows_the_board_rules(self, tmp_path: Path) -> None:
        """Under a 0.05 mm Default class the same 0.075 mm gap is legal."""
        pcb = tmp_path / "out.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        pcb.with_suffix(".kicad_pro").write_text(
            json.dumps({"net_settings": {"classes": [{"name": "Default", "clearance": 0.05}]}})
        )
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND", 12.0, 10.7)
        router.existing_routes.append(_v3v3())

        rules = BoardClearanceRules.from_board(pcb)
        assert kept_copper_clearance_violations(router, board_rules=rules) == []

    def test_round_pad_is_measured_as_its_real_shape(self) -> None:
        """A circle's corner region is not copper: the kernel, not the AABB."""
        router = _router()
        pad = _pad(2, "GND", 12.4, 10.196)
        pad.shape = "circle"
        router.pads[("J3", "1")] = pad
        # Diagonal kept trace on x + y = 21.5 whose closest approach is toward
        # the pad's bounding-box corner, which a circle does not fill: the
        # box would overlap the trace, the circle sits 0.15 mm clear.
        router.existing_routes.append(_trace(1, "+3V3", 9.0, 12.5, 13.0, 8.5))

        near = kept_copper_clearance_violations(router, board_rules=BoardClearanceRules())

        assert len(near) == 1
        line_dist = abs(12.4 + 10.196 - 21.5) / math.hypot(1.0, 1.0)
        assert near[0].distance == pytest.approx(line_dist - 0.5 - 0.125)
        assert near[0].distance == pytest.approx(0.15, abs=1e-3)
        assert _kept_shorts(router) == [], "the AABB would call this a short"

    def test_kept_trace_near_kept_trace_is_flagged_once(self) -> None:
        router = _router()
        router.existing_routes += [_v3v3(), _trace(2, "GND", 8.0, 10.4, 16.0, 10.4)]

        near = kept_copper_clearance_violations(router, board_rules=BoardClearanceRules())

        assert len(near) == 1
        assert near[0].obstacle_type == "segment"
        assert abs(near[0].distance - 0.15) < 1e-9
        assert "+3V3 trace vs GND trace" in describe_input_clearance(near[0])

    def test_kept_via_near_kept_via_is_flagged(self) -> None:
        router = _router()
        router.existing_routes += [_via(1, "+3V3", 8.0, 20.0), _via(2, "GND", 8.7, 20.0)]

        near = kept_copper_clearance_violations(router, board_rules=BoardClearanceRules())

        assert [(v.obstacle_type, v.segment_index) for v in near] == [("via", -1)]
        assert abs(near[0].distance - 0.1) < 1e-9

    def test_shorts_are_not_near_misses(self) -> None:
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND", 12.0, 10.0)
        router.existing_routes += [_v3v3(), _trace(2, "GND", 12.0, 5.0, 12.0, 15.0)]

        assert kept_copper_clearance_violations(router, board_rules=BoardClearanceRules()) == []

    def test_without_board_rules_uses_the_routed_copper_requirement(self) -> None:
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND", 12.0, 10.7)
        router.existing_routes.append(_v3v3())

        near = kept_copper_clearance_violations(router)

        assert len(near) == 1 and near[0].required >= 0.2


class TestRuleSeverity:
    @staticmethod
    def _board(tmp_path: Path, project: object | None) -> Path:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        if project is not None:
            text = project if isinstance(project, str) else json.dumps(project)
            pcb.with_suffix(".kicad_pro").write_text(text)
        return pcb

    @staticmethod
    def _sev(value: object) -> dict:
        return {"board": {"design_settings": {"rule_severities": {"clearance": value}}}}

    @pytest.mark.parametrize("value", ["error", "warning", "ignore"])
    def test_declared_severity_is_read(self, tmp_path: Path, value: str) -> None:
        assert kicad_rule_severity(self._board(tmp_path, self._sev(value)), "clearance") == value

    def test_case_is_normalized(self, tmp_path: Path) -> None:
        assert kicad_rule_severity(self._board(tmp_path, self._sev("Warning")), "clearance") == (
            "warning"
        )

    def test_missing_project_defaults_to_error(self, tmp_path: Path) -> None:
        assert kicad_rule_severity(self._board(tmp_path, None), "clearance") == "error"

    def test_missing_rule_defaults_to_error(self, tmp_path: Path) -> None:
        pcb = self._board(tmp_path, {"board": {"design_settings": {"rule_severities": {}}}})
        assert kicad_rule_severity(pcb, "clearance") == "error"

    @pytest.mark.parametrize("project", ["{not json", [], {"board": []}, None])
    def test_unreadable_project_defaults_to_error(self, tmp_path: Path, project: object) -> None:
        if project is None:
            project = TestRuleSeverity._sev(42)
        assert kicad_rule_severity(self._board(tmp_path, project), "clearance") == "error"

    def test_no_path_returns_default(self) -> None:
        assert kicad_rule_severity(None, "clearance") == "error"
        assert kicad_rule_severity(None, "clearance", default="warning") == "warning"


class TestCliGate:
    @staticmethod
    def _router_with_near_miss() -> Autorouter:
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND", 12.0, 10.7)
        router.existing_routes.append(_v3v3())
        return router

    @staticmethod
    def _out(tmp_path: Path, severity: str | None) -> Path:
        out = tmp_path / "out.kicad_pcb"
        out.write_text("(kicad_pcb)")
        if severity is not None:
            out.with_suffix(".kicad_pro").write_text(json.dumps(TestRuleSeverity._sev(severity)))
        return out

    def test_error_severity_fails(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        errors = _audit_kept_copper_clearance(
            self._router_with_near_miss(), self._out(tmp_path, None), quiet=False
        )
        out = capsys.readouterr().out
        assert len(errors) == 1
        assert "ERROR pre-existing clearance violation in input board" in out

    def test_warning_severity_reports_but_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        errors = _audit_kept_copper_clearance(
            self._router_with_near_miss(), self._out(tmp_path, "warning"), quiet=False
        )
        out = capsys.readouterr().out
        assert errors == []
        assert "WARNING pre-existing clearance violation in input board" in out

    def test_ignore_severity_is_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        errors = _audit_kept_copper_clearance(
            self._router_with_near_miss(), self._out(tmp_path, "ignore"), quiet=False
        )
        assert errors == []
        assert "clearance violation" not in capsys.readouterr().out

    def test_banner_for_input_clearance_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        near = kept_copper_clearance_violations(
            self._router_with_near_miss(), board_rules=BoardClearanceRules()
        )
        _print_short_failure_banner(near, "out.pcb")
        out = capsys.readouterr().out
        assert (
            "ROUTING FAILED: clearance violations in input copper kept by --preserve-existing"
            in out
        )
        assert "cross-net copper shorts" not in out
        assert "1 pre-existing in the input board (input defect), 0 caused by routing" in out
        assert "rule_severities.clearance" in out


# --- End-to-end: kct route --preserve-existing, then kicad-cli DRC ----------

_NETS = {1: "+3V3", 2: "GND", 3: "SIG"}


def _smd(ref: str, x: float, y: float, net: int) -> str:
    return (
        f'(footprint "Test:Pad" (layer "F.Cu") (at {x} {y}) '
        f'(property "Reference" "{ref}" (at 0 -1) (layer "F.SilkS")) '
        f'(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net {net} "{_NETS[net]}")))'
    )


def _tht(ref: str, x: float, y: float, net: int) -> str:
    return (
        f'(footprint "Test:TH" (layer "F.Cu") (at {x} {y}) '
        f'(property "Reference" "{ref}" (at 0 -1.5) (layer "F.SilkS")) '
        f'(pad "1" thru_hole circle (at 0 0) (size 1.2 1.2) (drill 0.6) (layers "*.Cu") '
        f'(net {net} "{_NETS[net]}")))'
    )


def _seg(x1: float, y1: float, x2: float, y2: float, net: int, layer: str = "F.Cu") -> str:
    return f'(segment (start {x1} {y1}) (end {x2} {y2}) (width 0.25) (layer "{layer}") (net {net}))'


def _via_sexp(x: float, y: float, net: int) -> str:
    return f'(via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {net}))'


#: Every kept net is anchored to its own pads, so KiCad's connectivity keeps
#: each item's net (floating copper would be re-netted on load).  The +3V3
#: trace J1-J2 at y=10 is common to all; SIG (J5-J6) is left for routing.
_CASES: dict[str, list[str]] = {
    "near_pad": [_smd("J3", 12, 10.7, 2), _smd("J4", 12, 20, 2), _seg(12, 11.2, 12, 20, 2)],
    "clear_pad": [_smd("J3", 12, 10.85, 2), _smd("J4", 12, 20, 2), _seg(12, 11.35, 12, 20, 2)],
    "trace_trace": [_smd("J3", 12, 5, 2), _smd("J4", 12, 15, 2), _seg(12, 5, 12, 15, 2)],
    "trace_via": [
        _tht("J3", 12, 4, 2),
        _smd("J4", 16, 4, 2),
        _seg(12, 4, 16, 4, 2),
        _seg(12, 4, 12, 10.3, 2, layer="B.Cu"),
        _via_sexp(12, 10.3, 2),
    ],
    "via_via": [
        _tht("J3", 8, 16, 2),
        _smd("J4", 12, 16, 2),
        _seg(8, 16, 12, 16, 2),
        _via_sexp(8, 10, 1),
        _via_sexp(8, 10.5, 2),
        _seg(8, 10.5, 8, 16, 2, layer="B.Cu"),
    ],
    "same_net": [_smd("J3", 12, 20, 1), _seg(12, 10, 12, 20, 1), _via_sexp(12, 10, 1)],
}


def _board(extra: list[str]) -> str:
    nodes = [
        '(kicad_pcb (version 20240108) (generator "test")',
        "(general (thickness 1.6))",
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))',
        '(net 0 "")',
        *(f'(net {n} "{name}")' for n, name in _NETS.items()),
        "(gr_rect (start 0 0) (end 24 40) (stroke (width 0.1) (type default)) "
        '(fill none) (layer "Edge.Cuts"))',
        _smd("J1", 5, 10, 1),
        _smd("J2", 19, 10, 1),
        _smd("J5", 5, 30, 3),
        _smd("J6", 19, 30, 3),
        _seg(5, 10, 19, 10, 1),
        *extra,
    ]
    return "\n".join([*nodes, ")"])


def _route(tmp_path: Path, case: str, project: dict | None = None) -> tuple[int, Path]:
    src = tmp_path / "in.kicad_pcb"
    out = tmp_path / "out.kicad_pcb"
    src.write_text(_board(_CASES[case]))
    if project is not None:
        src.with_suffix(".kicad_pro").write_text(json.dumps(project))
    rc = route_main(
        [str(src), "-o", str(out), "--preserve-existing", "--no-optimize", "--skip-drc"]
    )
    return rc, out


class TestCliPreserveExisting:
    def test_kept_near_miss_fails_the_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _out = _route(tmp_path, "near_pad")
        stdout = capsys.readouterr().out

        assert rc == 3
        assert "ROUTING FAILED: clearance violations in input copper" in stdout
        assert (
            "pre-existing clearance violation in input board (kept by --preserve-existing): "
            "+3V3 trace vs GND pad J3.1" in stdout
        )

    def test_warning_severity_passes_with_a_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _out = _route(tmp_path, "near_pad", TestRuleSeverity._sev("warning"))
        stdout = capsys.readouterr().out

        assert rc == 0
        assert "WARNING pre-existing clearance violation in input board" in stdout

    def test_kept_trace_crossing_kept_trace_fails_the_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _out = _route(tmp_path, "trace_trace")
        stdout = capsys.readouterr().out

        assert rc == 3
        assert "pre-existing short in input board (kept by --preserve-existing): " in stdout
        assert "+3V3 trace vs GND trace" in stdout

    def test_same_net_kept_copper_exits_zero(self, tmp_path: Path) -> None:
        rc, _out = _route(tmp_path, "same_net")
        assert rc == 0


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("near_pad", {"clearance"}),
        ("clear_pad", set()),
        ("trace_trace", {"short"}),
        ("trace_via", {"short"}),
        ("via_via", {"short", "clearance"}),
        ("same_net", set()),
    ],
)
def test_kicad_cli_agrees(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    expected: set[str],
) -> None:
    """kct and kicad-cli find the same classes of defect on the written board."""
    from kicad_tools.cli.runner import find_kicad_cli

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        pytest.skip("kicad-cli not installed")

    rc, out = _route(tmp_path, case)
    stdout = capsys.readouterr().out
    kct_found = set()
    if "pre-existing short in input board" in stdout:
        kct_found.add("short")
    if "pre-existing clearance violation in input board" in stdout:
        kct_found.add("clearance")

    report = tmp_path / "drc.json"
    subprocess.run(
        [
            str(kicad_cli),
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            "-o",
            str(report),
            str(out),
        ],
        check=False,
        capture_output=True,
        timeout=300,
    )
    data = json.loads(report.read_text())
    kicad_found = set()
    for v in data.get("violations", []):
        if v["type"] in ("shorting_items", "tracks_crossing"):
            kicad_found.add("short")
        elif v["type"] == "clearance":
            kicad_found.add("clearance")

    assert kct_found == expected, stdout
    assert kicad_found == expected, data.get("violations")
    assert rc == (3 if expected else 0)
