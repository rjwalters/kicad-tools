"""Overlapping different-net pads in the INPUT board are input defects (#6693).

Background
----------
On chorus-test-revA v21 every pad of the rotated SSOP-20 U8 has a stale angle,
so each 1.475 mm land runs *along* the 0.65 mm-pitch pin row and overlaps its
neighbours.  The two nets of each neighbour pair are shorted before routing
begins, and any via the router lands in a U8 pad touches the neighbouring pad
too.  ``kct route`` nevertheless reported those 16 shorts as "0 pre-existing in
the input board, 16 caused by routing", because input-origin attribution only
covered copper kept by ``--preserve-existing``.

The fix:

1. :func:`find_input_pad_overlaps` scans the router's own pad model for
   different-net pad pairs whose copper overlaps, and ``kct route`` prints a
   pre-route ``INPUT DEFECT`` block naming each footprint.
2. :func:`validate_routes` tags a short against a pad that already overlaps a
   pad of the copper's own net (which that copper touches) with
   ``origin="input_pad_overlap"``, so :func:`short_origin_summary` counts it as
   pre-existing in the input board.

Exit codes are unchanged: a short still fails the run.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import (
    _print_short_failure_banner,
    _report_input_pad_overlaps,
)
from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import (
    ORIGIN_INPUT_PAD_OVERLAP,
    describe_short,
    find_input_pad_overlaps,
    input_pad_overlaps,
    short_origin_summary,
    shorting_violations,
    summarize_input_pad_overlaps,
    validate_routes,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

PITCH = 0.65
# A stale-angle SSOP land: its 1.475 mm long axis runs along the pin row, so
# neighbours at 0.65 mm pitch overlap by 0.825 mm.
STALE_W, STALE_H = 1.475, 0.4


def _router() -> Autorouter:
    return Autorouter(
        width=40,
        height=40,
        rules=DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.05),
    )


def _pad(
    pin: str,
    x: float,
    net: int,
    name: str,
    *,
    ref: str = "U8",
    y: float = 10.0,
    width: float = STALE_W,
    height: float = STALE_H,
    layer: Layer = Layer.F_CU,
    shape: str = "rect",
    rotation: float = 0.0,
) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=name,
        layer=layer,
        ref=ref,
        pin=pin,
        shape=shape,
        rotation=rotation,
    )


def _via_route(net: int, name: str, x: float, y: float = 10.0) -> Route:
    return Route(
        net=net,
        net_name=name,
        segments=[],
        vias=[
            Via(
                x=x,
                y=y,
                drill=0.3,
                diameter=0.6,
                layers=(Layer.F_CU, Layer.B_CU),
                net=net,
            )
        ],
    )


def _stale_u8_router() -> Autorouter:
    """U8.1 (SPI_MOSI) and U8.2 (DBG_LED) overlap, as on the chorus fixture."""
    router = _router()
    router.pads[("U8", "1")] = _pad("1", 10.0, 1, "SPI_MOSI")
    router.pads[("U8", "2")] = _pad("2", 10.0 + PITCH, 2, "DBG_LED")
    router.net_names.update({1: "SPI_MOSI", 2: "DBG_LED"})
    return router


def _fine_pitch_router() -> Autorouter:
    """The same pins with the correct angle: short axis along the row, no overlap."""
    router = _router()
    router.pads[("U8", "1")] = _pad("1", 10.0, 1, "SPI_MOSI", width=STALE_H, height=STALE_W)
    router.pads[("U8", "2")] = _pad("2", 10.0 + PITCH, 2, "DBG_LED", width=STALE_H, height=STALE_W)
    router.net_names.update({1: "SPI_MOSI", 2: "DBG_LED"})
    return router


# --- The input scan ---------------------------------------------------------


class TestFindInputPadOverlaps:
    def test_stale_angle_neighbours_overlap(self) -> None:
        overlaps = find_input_pad_overlaps(_stale_u8_router().pads)
        assert len(overlaps) == 1
        o = overlaps[0]
        assert (o.pad_a, o.pad_b) == ("U8.1", "U8.2")
        assert (o.net_a, o.net_b) == ("SPI_MOSI", "DBG_LED")

    def test_correctly_oriented_fine_pitch_does_not_overlap(self) -> None:
        assert find_input_pad_overlaps(_fine_pitch_router().pads) == []

    def test_same_net_overlap_is_not_a_defect(self) -> None:
        pads = {
            ("U8", "1"): _pad("1", 10.0, 1, "GND"),
            ("U8", "2"): _pad("2", 10.0 + PITCH, 1, "GND"),
        }
        assert find_input_pad_overlaps(pads) == []

    def test_skipped_pour_pads_same_name_are_same_net(self) -> None:
        # Issue #6225: skipped pour-net pads are rewritten to id 0, name kept.
        pads = {
            ("U8", "1"): _pad("1", 10.0, 0, "GND"),
            ("U8", "2"): _pad("2", 10.0 + PITCH, 0, "GND"),
        }
        assert find_input_pad_overlaps(pads) == []

    def test_skipped_pour_pad_vs_signal_is_a_defect(self) -> None:
        pads = {
            ("U8", "1"): _pad("1", 10.0, 0, "GND"),
            ("U8", "2"): _pad("2", 10.0 + PITCH, 2, "DBG_LED"),
        }
        assert len(find_input_pad_overlaps(pads)) == 1

    def test_unconnected_pads_are_ignored(self) -> None:
        pads = {
            ("U8", "1"): _pad("1", 10.0, 0, ""),
            ("U8", "2"): _pad("2", 10.0 + PITCH, 2, "DBG_LED"),
        }
        assert find_input_pad_overlaps(pads) == []

    def test_smd_pads_on_different_layers_do_not_overlap(self) -> None:
        pads = {
            ("U8", "1"): _pad("1", 10.0, 1, "A"),
            ("U8", "2"): _pad("2", 10.0 + PITCH, 2, "B", layer=Layer.B_CU),
        }
        assert find_input_pad_overlaps(pads) == []

    def test_touching_pads_do_not_count(self) -> None:
        pads = {
            ("U8", "1"): _pad("1", 10.0, 1, "A", width=1.0),
            ("U8", "2"): _pad("2", 11.0, 2, "B", width=1.0),
        }
        assert find_input_pad_overlaps(pads) == []

    def test_circle_pads_use_their_disc(self) -> None:
        # Diagonal neighbours whose bounding squares overlap but discs do not.
        pads = {
            ("J1", "1"): _pad("1", 10.0, 1, "A", ref="J1", width=1.0, height=1.0, shape="circle"),
            ("J1", "2"): _pad(
                "2", 10.8, 2, "B", ref="J1", y=10.8, width=1.0, height=1.0, shape="circle"
            ),
        }
        assert find_input_pad_overlaps(pads) == []

    def test_residual_rotation_is_honoured(self) -> None:
        # A 90-degree residual turns the stale land back across the row.
        pads = {
            ("U8", "1"): _pad("1", 10.0, 1, "A", rotation=90.0),
            ("U8", "2"): _pad("2", 10.0 + PITCH, 2, "B", rotation=90.0),
        }
        assert find_input_pad_overlaps(pads) == []

    def test_summary_groups_by_footprint(self) -> None:
        router = _stale_u8_router()
        router.pads[("U8", "3")] = _pad("3", 10.0 + 2 * PITCH, 3, "SPI_MISO")
        router.pads[("J2", "1")] = _pad("1", 30.0, 4, "X", ref="J2")
        router.pads[("J4", "1")] = _pad("1", 30.5, 5, "Y", ref="J4")
        lines = summarize_input_pad_overlaps(find_input_pad_overlaps(router.pads))
        # 1.475 mm lands at 0.65 mm pitch reach the next-but-one pin too
        # (1.3 mm apart), so three pins give three overlapping pairs.
        assert lines == [
            "U8: 3 overlapping different-net pad pair(s)",
            "J2/J4: 1 overlapping different-net pad pair(s)",
        ]

    def test_router_scan_is_memoized(self) -> None:
        router = _stale_u8_router()
        first = input_pad_overlaps(router)
        assert input_pad_overlaps(router) is first


# --- Post-route short attribution -------------------------------------------


class TestShortAttribution:
    def test_via_in_overlapping_pad_is_an_input_defect(self) -> None:
        router = _stale_u8_router()
        # In-pad escape via at U8.1's centre: inside U8.2's 1.475 mm extent.
        router.routes.append(_via_route(1, "SPI_MOSI", 10.0))

        shorts = shorting_violations(validate_routes(router))

        assert len(shorts) == 1
        short = shorts[0]
        assert short.obstacle_pad == "U8.2"
        assert short.origin == ORIGIN_INPUT_PAD_OVERLAP
        assert short.is_input_defect
        assert short.overlapping_pad == "U8.1"
        assert short_origin_summary(shorts) == (
            "1 pre-existing in the input board (input defect), "
            "0 caused by routing (routing failure)"
        )
        assert describe_short(short).startswith(
            "pre-existing short in input board (pads U8.1 and U8.2 already overlap): "
            "SPI_MOSI via vs DBG_LED pad U8.2"
        )

    def test_trace_into_overlapping_pad_is_an_input_defect(self) -> None:
        router = _stale_u8_router()
        router.routes.append(
            Route(
                net=1,
                net_name="SPI_MOSI",
                segments=[
                    Segment(x1=10.0, y1=10.0, x2=10.0, y2=5.0, layer=Layer.F_CU, width=0.2, net=1)
                ],
                vias=[],
            )
        )
        shorts = shorting_violations(validate_routes(router))
        assert [v.obstacle_pad for v in shorts] == ["U8.2"]
        assert shorts[0].is_input_defect

    def test_correct_fine_pitch_via_short_is_routing(self) -> None:
        """Regression: no input overlap, so a via on a foreign pad is routing's fault."""
        router = _fine_pitch_router()
        router.routes.append(_via_route(1, "SPI_MOSI", 10.0 + PITCH))

        shorts = shorting_violations(validate_routes(router))

        assert len(shorts) == 1
        assert shorts[0].origin == "routing"
        assert not shorts[0].is_input_defect
        assert "0 pre-existing" in short_origin_summary(shorts)
        assert "1 caused by routing" in short_origin_summary(shorts)

    def test_short_not_touching_own_overlapping_pad_is_routing(self) -> None:
        """An overlap elsewhere does not excuse copper that never touches it."""
        router = _stale_u8_router()
        # SPI_MOSI via on the far end of U8.2, clear of U8.1.
        router.routes.append(_via_route(1, "SPI_MOSI", 10.0 + PITCH + 0.6))

        shorts = shorting_violations(validate_routes(router))

        assert len(shorts) == 1
        assert shorts[0].obstacle_pad == "U8.2"
        assert shorts[0].origin == "routing"

    def test_third_net_short_on_overlapping_pad_is_routing(self) -> None:
        """Only the nets whose pads overlap are shorted by the input."""
        router = _stale_u8_router()
        router.net_names[3] = "OTHER"
        router.routes.append(_via_route(3, "OTHER", 10.0 + PITCH))

        shorts = shorting_violations(validate_routes(router))

        assert shorts
        assert all(v.origin == "routing" for v in shorts)

    def test_banner_points_at_input_pads(self, capsys: pytest.CaptureFixture[str]) -> None:
        router = _stale_u8_router()
        router.routes.append(_via_route(1, "SPI_MOSI", 10.0))
        _print_short_failure_banner(shorting_violations(validate_routes(router)), "out.kicad_pcb")
        out = capsys.readouterr().out
        assert "1 pre-existing in the input board (input defect), 0 caused by routing" in out
        assert "already overlaps a different-net pad in the input board" in out
        assert "--only clearance" in out
        # The --preserve-existing hint is for kept copper only.
        assert "kept by --preserve-existing; fix or remove" not in out


# --- Pre-route INPUT DEFECT block -------------------------------------------


class TestPreRouteReport:
    def test_names_overlapping_footprint(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = argparse.Namespace()
        _report_input_pad_overlaps(_stale_u8_router(), args, "board.kicad_pcb")
        out = capsys.readouterr().out
        assert "INPUT DEFECT" in out
        assert "U8: 1 overlapping different-net pad pair(s)" in out
        assert "U8.1 (SPI_MOSI) overlaps U8.2 (DBG_LED)" in out
        assert "kct check board.kicad_pcb --only clearance" in out

    def test_printed_once_per_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = argparse.Namespace()
        _report_input_pad_overlaps(_stale_u8_router(), args, "b.kicad_pcb")
        _report_input_pad_overlaps(_stale_u8_router(), args, "b.kicad_pcb")
        assert capsys.readouterr().out.count("INPUT DEFECT") == 1

    def test_silent_for_clean_fine_pitch(self, capsys: pytest.CaptureFixture[str]) -> None:
        _report_input_pad_overlaps(_fine_pitch_router(), argparse.Namespace(), "b.kicad_pcb")
        assert capsys.readouterr().out == ""

    def test_silent_when_quiet(self, capsys: pytest.CaptureFixture[str]) -> None:
        _report_input_pad_overlaps(
            _stale_u8_router(), argparse.Namespace(), "b.kicad_pcb", quiet=True
        )
        assert capsys.readouterr().out == ""


# --- End-to-end: kct route on a board with overlapping pads -----------------


def _board(*, overlap: bool) -> str:
    """U1's two pads overlap when ``overlap`` (stale-angle land).

    U1's nets A/B are single-pad nets, so the only routing work is the short
    SIG net J1-J2: the run stays fast and the assertion is about the
    pre-route report, not about routing through an impossible footprint.
    """
    size = f"{STALE_W} {STALE_H}" if overlap else f"{STALE_H} {STALE_W}"
    nodes = [
        '(kicad_pcb (version 20240108) (generator "test")',
        "(general (thickness 1.6))",
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))',
        '(net 0 "")',
        '(net 1 "A")',
        '(net 2 "B")',
        '(net 3 "SIG")',
        "(gr_rect (start 0 0) (end 30 30) (stroke (width 0.1) (type default)) "
        '(fill none) (layer "Edge.Cuts"))',
        '(footprint "Test:Pair" (layer "F.Cu") (at 15 15) '
        '(property "Reference" "U1" (at 0 -2) (layer "F.SilkS")) '
        f'(pad "1" smd rect (at {-PITCH / 2} 0) (size {size}) (layers "F.Cu") (net 1 "A")) '
        f'(pad "2" smd rect (at {PITCH / 2} 0) (size {size}) (layers "F.Cu") (net 2 "B")))',
    ]
    for ref, x, y in (("J1", 5, 25), ("J2", 25, 25)):
        nodes.append(
            f'(footprint "Test:Pad" (layer "F.Cu") (at {x} {y}) '
            f'(property "Reference" "{ref}" (at 0 -1) (layer "F.SilkS")) '
            '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "SIG")))'
        )
    return "\n".join([*nodes, ")"])


class TestCli:
    @pytest.fixture(autouse=True)
    def _no_kicad_cli_discovery(self, monkeypatch):
        # Issue #6645: keep kicad-cli discovery out of the routing verdict.
        monkeypatch.setattr("kicad_tools.cli.runner.find_kicad_cli", lambda *a, **k: None)

    def _route(self, tmp_path: Path, *, overlap: bool) -> int:
        src = tmp_path / "in.kicad_pcb"
        src.write_text(_board(overlap=overlap))
        rc = route_main(
            [str(src), "-o", str(tmp_path / "out.kicad_pcb"), "--no-optimize", "--skip-drc"]
        )
        return rc

    def test_overlapping_input_pads_are_named_before_routing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._route(tmp_path, overlap=True)
        out = capsys.readouterr().out
        assert "INPUT DEFECT: overlapping different-net pads" in out
        assert "U1: 1 overlapping different-net pad pair(s)" in out
        assert out.index("INPUT DEFECT") < out.index("Nets to route")
        assert "kct check " in out and "--only clearance" in out

    def test_clean_board_has_no_input_defect_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._route(tmp_path, overlap=False)
        assert "INPUT DEFECT" not in capsys.readouterr().out
