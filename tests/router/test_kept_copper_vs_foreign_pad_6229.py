"""Kept input copper vs other nets' pads in the post-route short check (#6229).

Background
----------
``validate_routes`` is ``kct route``'s post-route short check.  Issue #5862
taught it to check ROUTED copper against trace copper kept from the input
board by ``--preserve-existing``, but nothing checked that kept trace copper
against the PADS of other nets.  A kept ``+3V3`` trace laid across a ``GND``
pad was therefore never reported: ``kct route`` exited 0 and printed SUCCESS
for a board that shorts two nets, which ``kicad-cli pcb drc`` then reports as
``shorting_items``.

The fix checks every kept trace and via against the pads of every other net,
using the #6225 same-net rule (id match, or an id-0 skipped-net pad with a
matching name).  A short whose copper was kept from the input is tagged
``origin="input"`` and reported as a pre-existing input defect -- it still
fails the run (exit 3/4), because the output board is unmanufacturable
whoever drew the copper.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import _print_short_failure_banner
from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import (
    ClearanceViolation,
    describe_short,
    format_clearance_violations,
    kept_input_routes,
    shorting_violations,
    validate_routes,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

PAD_X, PAD_Y = 12.0, 10.0


def _router() -> Autorouter:
    return Autorouter(
        width=40,
        height=40,
        rules=DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1),
    )


def _pad(net: int, name: str, *, ref: str = "J3", pin: str = "1") -> Pad:
    return Pad(
        x=PAD_X,
        y=PAD_Y,
        width=1.0,
        height=1.0,
        net=net,
        net_name=name,
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
    )


def _kept_trace(net: int, name: str, *, y: float = PAD_Y, layer: Layer = Layer.F_CU) -> Route:
    """A kept trace running straight across the pad at ``(PAD_X, y)``."""
    return Route(
        net=net,
        net_name=name,
        segments=[
            Segment(x1=5.0, y1=y, x2=19.0, y2=y, layer=layer, width=0.25, net=net),
        ],
        vias=[],
    )


def _kept_via(net: int, name: str) -> Route:
    """A kept via sitting on the pad centre."""
    return Route(
        net=net,
        net_name=name,
        segments=[],
        vias=[
            Via(
                x=PAD_X,
                y=PAD_Y,
                drill=0.3,
                diameter=0.6,
                layers=(Layer.F_CU, Layer.B_CU),
                net=net,
            )
        ],
    )


class TestKeptTraceVsForeignPad:
    def test_kept_trace_crossing_foreign_pad_is_flagged(self) -> None:
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND")
        router.net_names.update({1: "+3V3", 2: "GND"})
        router.existing_routes.append(_kept_trace(1, "+3V3"))

        shorts = shorting_violations(validate_routes(router))

        assert len(shorts) == 1, "a kept +3V3 trace across a GND pad is a short"
        short = shorts[0]
        assert short.obstacle_type == "pad"
        assert short.origin == "input"
        assert short.is_input_defect
        assert short.segment_index == 0
        assert short.net_name == "+3V3"
        assert short.obstacle_net_name == "GND"
        assert short.obstacle_pad == "J3.1"
        assert short.layer == Layer.F_CU
        assert abs(-short.distance - 0.125) < 1e-9  # pad half-height + half trace

    def test_kept_trace_crossing_skipped_pour_net_pad_is_flagged(self) -> None:
        """An id-0 pad (skipped pour net) with a DIFFERENT name is foreign."""
        router = _router()
        router.pads[("J3", "1")] = _pad(0, "GND")
        router.existing_routes.append(_kept_trace(1, "+3V3"))

        shorts = shorting_violations(validate_routes(router))

        assert [(v.origin, v.obstacle_net_name) for v in shorts] == [("input", "GND")]

    def test_kept_trace_on_other_layer_is_not_flagged(self) -> None:
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND")
        router.existing_routes.append(_kept_trace(1, "+3V3", layer=Layer.B_CU))

        assert shorting_violations(validate_routes(router)) == []

    def test_kept_trace_near_miss_is_not_reported(self) -> None:
        """Only overlap is reported for kept copper; a near-miss is not ours."""
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND")
        # Pad edge at y=10.5, trace edge at 10.65 - 0.125 = 10.525: 0.025mm gap.
        router.existing_routes.append(_kept_trace(1, "+3V3", y=PAD_Y + 0.65))

        violations = validate_routes(router)

        assert [v for v in violations if v.origin == "input"] == []


class TestKeptViaVsForeignPad:
    def test_kept_via_on_foreign_pad_is_flagged_as_input(self) -> None:
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND")
        router.net_names.update({1: "+3V3", 2: "GND"})
        router.existing_routes.append(_kept_via(1, "+3V3"))

        shorts = shorting_violations(validate_routes(router))

        assert len(shorts) == 1
        short = shorts[0]
        assert short.segment_index == -1
        assert short.origin == "input"
        assert short.obstacle_pad == "J3.1"
        assert "+3V3 via vs GND pad J3.1" in describe_short(short)

    def test_routed_via_on_foreign_pad_stays_routing(self) -> None:
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND")
        router.net_names.update({1: "+3V3", 2: "GND"})
        router.routes.append(_kept_via(1, "+3V3"))

        shorts = shorting_violations(validate_routes(router))

        assert [v.origin for v in shorts] == ["routing"]
        assert describe_short(shorts[0]).startswith("routing short [pad] +3V3 vs GND")


class TestKeptSameNetCopperIsNotFlagged:
    def test_same_net_by_id(self) -> None:
        router = _router()
        router.pads[("J3", "1")] = _pad(1, "+3V3")
        router.existing_routes.append(_kept_trace(1, "+3V3"))
        router.existing_routes.append(_kept_via(1, "+3V3"))

        assert shorting_violations(validate_routes(router)) == []

    def test_same_net_by_name_on_id0_skipped_pad(self) -> None:
        """#6225 rule: id-0 pad whose name matches the kept copper's net."""
        router = _router()
        router.pads[("J3", "1")] = _pad(0, "+3V3")
        router.existing_routes.append(_kept_trace(1, "+3V3"))
        router.existing_routes.append(_kept_via(1, "+3V3"))

        violations = validate_routes(router)

        assert [v for v in violations if v.obstacle_type == "pad"] == []

    def test_same_net_name_resolved_from_router_net_names(self) -> None:
        router = _router()
        router.pads[("J3", "1")] = _pad(0, "+3V3")
        router.net_names[1] = "+3V3"
        router.existing_routes.append(_kept_trace(1, ""))

        assert shorting_violations(validate_routes(router)) == []

    def test_netless_kept_copper_is_skipped(self) -> None:
        """Kept copper on net 0 has no known owner, so it cannot short a net."""
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND")
        router.existing_routes.append(_kept_trace(0, ""))

        assert shorting_violations(validate_routes(router)) == []


class TestKeptSetMatchesTheOutput:
    def test_rerouted_net_stale_copper_is_not_kept(self) -> None:
        """A re-routed net's old copper is replaced in the output, not kept."""
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND")
        router.existing_routes.append(_kept_trace(1, "+3V3"))
        router.routes.append(
            Route(
                net=1,
                net_name="+3V3",
                segments=[
                    Segment(x1=5.0, y1=20.0, x2=19.0, y2=20.0, layer=Layer.F_CU, width=0.25, net=1)
                ],
                vias=[],
            )
        )

        assert kept_input_routes(router) == []
        assert shorting_violations(validate_routes(router)) == []

    def test_emitted_preserved_routes_win(self) -> None:
        """After finalize, exactly the re-emitted preserved copper is audited."""
        router = _router()
        router.pads[("J3", "1")] = _pad(2, "GND")
        kept = _kept_trace(1, "+3V3")
        router._emitted_preserved_routes = [kept]

        assert kept_input_routes(router) == [kept]
        assert [v.origin for v in shorting_violations(validate_routes(router))] == ["input"]

        router._emitted_preserved_routes = []
        router.existing_routes.append(kept)
        assert shorting_violations(validate_routes(router)) == []


class TestReporting:
    @staticmethod
    def _short(origin: str) -> ClearanceViolation:
        return ClearanceViolation(
            segment_index=0,
            x1=5.0,
            y1=10.0,
            x2=19.0,
            y2=10.0,
            net=1,
            obstacle_type="pad",
            obstacle_net=2,
            distance=-0.125,
            required=0.2,
            net_name="+3V3",
            obstacle_net_name="GND",
            location=(12.0, 10.0),
            layer=Layer.F_CU,
            origin=origin,
            obstacle_pad="J3.1",
        )

    def test_default_origin_is_routing(self) -> None:
        v = ClearanceViolation(
            segment_index=0,
            x1=0.0,
            y1=0.0,
            x2=1.0,
            y2=0.0,
            net=1,
            obstacle_type="pad",
            obstacle_net=2,
            distance=-0.1,
            required=0.2,
        )
        assert v.origin == "routing"
        assert not v.is_input_defect

    def test_input_short_wording(self) -> None:
        text = describe_short(self._short("input"))
        assert text == (
            "pre-existing short in input board (kept by --preserve-existing): "
            "+3V3 trace vs GND pad J3.1 at (12.000, 10.000) on F.Cu: overlap 0.125mm"
        )

    def test_summary_counts_input_vs_routing(self) -> None:
        text = format_clearance_violations([self._short("input"), self._short("routing")])
        assert "SHORTS (copper of different nets overlapping): 2" in text
        assert "1 pre-existing in the input board (input defect)" in text
        assert "1 caused by routing (routing failure)" in text
        assert "pre-existing short in input board" in text

    def test_failure_banner_names_both_classes(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_short_failure_banner([self._short("input"), self._short("routing")], "out.pcb")
        out = capsys.readouterr().out
        assert "ROUTING FAILED" in out
        assert "1 pre-existing in the input board (input defect)" in out
        assert "1 caused by routing (routing failure)" in out
        assert "kept by --preserve-existing" in out


# --- End-to-end: kct route --preserve-existing, then kicad-cli DRC ----------


def _board(*, short: bool) -> str:
    """Kept +3V3 trace J1-J2 at y=10; GND pad J3 on it when ``short``.

    SIG (J5-J6) is left unrouted so the run has real routing work.
    """
    gnd_y = 10 if short else 14
    nodes = [
        '(kicad_pcb (version 20240108) (generator "test")',
        "(general (thickness 1.6))",
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))',
        '(net 0 "")',
        '(net 1 "+3V3")',
        '(net 2 "GND")',
        '(net 3 "SIG")',
        "(gr_rect (start 0 0) (end 24 40) (stroke (width 0.1) (type default)) "
        '(fill none) (layer "Edge.Cuts"))',
    ]
    pads = [
        ("J1", 5, 10, 1, "+3V3"),
        ("J2", 19, 10, 1, "+3V3"),
        ("J3", 12, gnd_y, 2, "GND"),
        ("J4", 12, 20, 2, "GND"),
        ("J5", 5, 30, 3, "SIG"),
        ("J6", 19, 30, 3, "SIG"),
    ]
    for ref, x, y, num, name in pads:
        nodes.append(
            f'(footprint "Test:Pad" (layer "F.Cu") (at {x} {y}) '
            f'(property "Reference" "{ref}" (at 0 -1) (layer "F.SilkS")) '
            f'(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net {num} "{name}")))'
        )
    nodes.append('(segment (start 5 10) (end 19 10) (width 0.25) (layer "F.Cu") (net 1))')
    nodes.append(
        f'(segment (start 12 {gnd_y + 0.4}) (end 12 20) (width 0.25) (layer "F.Cu") (net 2))'
    )
    return "\n".join([*nodes, ")"])


def _route(tmp_path: Path, *, short: bool) -> tuple[int, Path]:
    src = tmp_path / "in.kicad_pcb"
    out = tmp_path / "out.kicad_pcb"
    src.write_text(_board(short=short))
    rc = route_main(
        [str(src), "-o", str(out), "--preserve-existing", "--no-optimize", "--skip-drc"]
    )
    return rc, out


class TestCliPreserveExisting:
    def test_input_short_fails_the_run_and_is_labelled(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _out = _route(tmp_path, short=True)
        stdout = capsys.readouterr().out

        assert rc == 3, "fully routed + shorted copper is the exit-3 contract"
        assert "SUCCESS: Design" not in stdout
        assert "ROUTING FAILED" in stdout
        assert (
            "pre-existing short in input board (kept by --preserve-existing): "
            "+3V3 trace vs GND pad J3.1" in stdout
        )
        assert "1 pre-existing in the input board (input defect), 0 caused by routing" in stdout

    def test_clean_kept_copper_still_exits_zero(self, tmp_path: Path) -> None:
        rc, _out = _route(tmp_path, short=False)
        assert rc == 0

    def test_kicad_cli_agrees(self, tmp_path: Path) -> None:
        """Both engines report the short on the written board."""
        from kicad_tools.cli.runner import find_kicad_cli

        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            pytest.skip("kicad-cli not installed")

        rc, out = _route(tmp_path, short=True)
        assert rc == 3

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
        shorts = [v for v in data.get("violations", []) if v["type"] == "shorting_items"]
        assert len(shorts) == 1, shorts
        items = " | ".join(item["description"] for item in shorts[0]["items"])
        assert "J3" in items and "+3V3" in items and "GND" in items
