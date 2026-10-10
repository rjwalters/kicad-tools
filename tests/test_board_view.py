"""``kct board-view`` and the ``board_view`` MCP tool (issue #6316).

None of this needs ``kicad-cli``.  Tests that draw need matplotlib, which the
``dev`` extra installs; they skip without it.  The ``list`` view, the
coordinate-frame model and the missing-link computation run without it.
"""

from __future__ import annotations

import io
import json
import math
from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.boardview import (
    CAVEAT,
    FRAME_BOARD,
    FRAME_SHEET,
    MAX_VISION_API_PX,
    board_view,
    collect_geometry,
    missing_links,
)
from kicad_tools.boardview import render as render_module
from kicad_tools.boardview.model import arc_points
from kicad_tools.cli import main as kct_main
from kicad_tools.schema.pcb import PCB

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARD_05 = REPO_ROOT / "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb"
ARC_FIXTURE = REPO_ROOT / "tests/fixtures/copper_arc_routed.kicad_pcb"

# The board's Edge.Cuts minimum corner, i.e. pcb.board_origin.  Every
# coordinate in the file below is sheet-absolute; PCB subtracts this from
# footprints, tracks and vias at load and leaves the outline alone.
ORIGIN = (100.0, 50.0)

# The middle segment of the SIG route; dropping it leaves R1.1 and R2.1 apart.
SIG_MIDDLE_SEGMENT = (
    '  (segment (start 109 56) (end 124 56) (width 0.25) (layer "F.Cu") (net 1) (uuid "s2"))\n'
)


def board_text(*, outline: tuple[float, float] = (140.0, 80.0), cut_sig: bool = False) -> str:
    """A four-resistor board with one net of each kind this tool separates.

    * ``SIG``  R1.1 - R2.1, fully routed (three segments).
    * ``OPEN`` R1.2 - R2.2 routed; R3.1 is not reached: one missing link.
    * ``GND``  R3.2 and R4.1 in a pour whose fill covers only R4.1: the net
      is incomplete, but ``kct check`` treats that as advisory.
    """
    x1, y1 = outline
    text = f"""(kicad_pcb (version 20240108) (generator "test_fixture")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "OPEN")
  (net 3 "GND")
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu") (at 110 60)
    (property "Reference" "R1" (at 0 -2 0) (layer "F.SilkS") (uuid "r1-ref"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "SIG") (uuid "r1-1"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 2 "OPEN") (uuid "r1-2"))
  )
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu") (at 125 60)
    (property "Reference" "R2" (at 0 -2 0) (layer "F.SilkS") (uuid "r2-ref"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "SIG") (uuid "r2-1"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 2 "OPEN") (uuid "r2-2"))
  )
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu") (at 125 70)
    (property "Reference" "R3" (at 0 -2 0) (layer "F.SilkS") (uuid "r3-ref"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 2 "OPEN") (uuid "r3-1"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 3 "GND") (uuid "r3-2"))
  )
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu") (at 110 70)
    (property "Reference" "R4" (at 0 -2 0) (layer "F.SilkS") (uuid "r4-ref"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 3 "GND") (uuid "r4-1"))
  )
  (gr_rect (start {ORIGIN[0]:g} {ORIGIN[1]:g}) (end {x1:g} {y1:g})
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") (uuid "edge"))
  (segment (start 109 60) (end 109 56) (width 0.25) (layer "F.Cu") (net 1) (uuid "s1"))
{SIG_MIDDLE_SEGMENT}  (segment (start 124 56) (end 124 60) (width 0.25) (layer "F.Cu") (net 1) (uuid "s3"))
  (segment (start 111 60) (end 111 63) (width 0.25) (layer "F.Cu") (net 2) (uuid "o1"))
  (segment (start 111 63) (end 126 63) (width 0.25) (layer "F.Cu") (net 2) (uuid "o2"))
  (segment (start 126 63) (end 126 60) (width 0.25) (layer "F.Cu") (net 2) (uuid "o3"))
  (zone (net 3) (net_name "GND") (layer "F.Cu") (uuid "zone") (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.25)
    (filled_areas_thickness no)
    (polygon (pts (xy 104 66) (xy 130 66) (xy 130 74) (xy 104 74)))
    (filled_polygon
      (layer "F.Cu")
      (pts (xy 104 66) (xy 114 66) (xy 114 74) (xy 104 74))
    )
  )
)
"""
    if cut_sig:
        assert SIG_MIDDLE_SEGMENT in text
        text = text.replace(SIG_MIDDLE_SEGMENT, "")
    return text


@pytest.fixture
def board(tmp_path: Path) -> Path:
    path = tmp_path / "views.kicad_pcb"
    path.write_text(board_text())
    return path


@pytest.fixture
def cut_board(tmp_path: Path) -> Path:
    """The same board with one SIG trace removed."""
    path = tmp_path / "cut.kicad_pcb"
    path.write_text(board_text(cut_sig=True))
    return path


def _png_size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def _axes(geo, report, box, **kwargs):
    """Draw one panel and hand back its axes for inspection."""
    pytest.importorskip("matplotlib")
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=(8, 6), dpi=100)
    FigureCanvasAgg(fig)
    ax = fig.add_axes((0.1, 0.1, 0.8, 0.8))
    render_module.draw_panel(ax, geo, report, box, **kwargs)
    return ax


def _is_point(candidate, point, tol: float = 1e-9) -> bool:
    return math.isclose(candidate[0], point[0], abs_tol=tol) and math.isclose(
        candidate[1], point[1], abs_tol=tol
    )


# ---------------------------------------------------------------------------
# Coordinate frame
# ---------------------------------------------------------------------------


class TestCoordinateFrame:
    def test_fixture_has_a_non_zero_origin(self, board: Path):
        assert PCB.load(board).board_origin == ORIGIN

    def test_board_relative_is_the_default_and_matches_net_status(self, board: Path):
        pcb = PCB.load(board)
        geo = collect_geometry(pcb)
        assert geo.frame == FRAME_BOARD
        assert geo.origin == ORIGIN

        r1_1 = next(p for p in geo.pads if p.name == "R1.1")
        assert (r1_1.x, r1_1.y) == pytest.approx((9.0, 10.0))
        # kct net-status prints PadInfo.position: same frame, same numbers.
        sig = NetStatusAnalyzer(pcb).analyze().get_net("SIG")
        assert sig is not None
        listed = next(p for p in sig.connected_pads if p.full_name == "R1.1")
        assert listed.position == pytest.approx((r1_1.x, r1_1.y))

    def test_pad_and_track_end_that_coincide_in_the_file_coincide_in_the_model(self, board: Path):
        """File: pad R1.1 at (109, 60), a SIG segment starting at (109, 60)."""
        for absolute, expected in ((False, (9.0, 10.0)), (True, (109.0, 60.0))):
            geo = collect_geometry(PCB.load(board), absolute=absolute)
            pad = next(p for p in geo.pads if p.name == "R1.1")
            assert (pad.x, pad.y) == pytest.approx(expected)
            ends = [end for t in geo.tracks if t.net == "SIG" for end in (t.start, t.end)]
            assert any(_is_point(end, (pad.x, pad.y)) for end in ends)

    def test_outline_is_brought_into_the_same_frame(self, board: Path):
        """The trap: Edge.Cuts is sheet-absolute in ``pcb.graphics``; copper is not."""
        relative = collect_geometry(PCB.load(board))
        assert relative.bounds == pytest.approx((0.0, 0.0, 40.0, 30.0))
        (outline,) = relative.outline
        assert min(p[0] for p in outline) == pytest.approx(0.0)
        assert max(p[1] for p in outline) == pytest.approx(30.0)
        # Every pad lies inside the outline it is drawn against.
        for pad in relative.pads:
            assert 0.0 < pad.x < 40.0 and 0.0 < pad.y < 30.0

        absolute = collect_geometry(PCB.load(board), absolute=True)
        assert absolute.frame == FRAME_SHEET
        assert absolute.bounds == pytest.approx((100.0, 50.0, 140.0, 80.0))
        (outline,) = absolute.outline
        assert min(p[0] for p in outline) == pytest.approx(100.0)

    def test_pad_and_track_end_coincide_in_image_data_coordinates(self, board: Path):
        from matplotlib.collections import LineCollection, PolyCollection

        pcb = PCB.load(board)
        for absolute, centre in ((False, (9.0, 10.0)), (True, (109.0, 60.0))):
            geo = collect_geometry(pcb, absolute=absolute)
            assert geo.bounds is not None
            ax = _axes(geo, missing_links(pcb, absolute=absolute), geo.bounds)

            pads = next(c for c in ax.collections if isinstance(c, PolyCollection))
            centres = [path.vertices[:4].mean(axis=0) for path in pads.get_paths()]
            assert any(_is_point(c, centre) for c in centres), "pad R1.1 not at its frame position"

            ends = [
                tuple(point)
                for c in ax.collections
                if isinstance(c, LineCollection)
                for segment in c.get_segments()
                for point in (segment[0], segment[-1])
            ]
            assert any(_is_point(end, centre) for end in ends), "no track ends on pad R1.1"
            # ... and both are inside the visible window (the blank-image bug).
            x0, x1 = sorted(ax.get_xlim())
            y0, y1 = sorted(ax.get_ylim())
            assert x0 <= centre[0] <= x1 and y0 <= centre[1] <= y1

    def test_y_axis_grows_downward(self, board: Path):
        pcb = PCB.load(board)
        geo = collect_geometry(pcb)
        assert geo.bounds is not None
        ax = _axes(geo, missing_links(pcb), geo.bounds)
        top, bottom = ax.get_ylim()[1], ax.get_ylim()[0]
        assert top < bottom

    def test_result_and_title_state_the_frame_and_origin(self, board: Path, tmp_path: Path):
        pytest.importorskip("matplotlib")
        relative = board_view(str(board), "overview")
        assert relative["success"], relative["error_message"]
        assert relative["frame"] == "board-relative"
        assert relative["board_origin_mm"] == [100.0, 50.0]
        assert relative["units"] == "mm"
        assert relative["window_mm"] == [-1.0, -1.0, 41.0, 31.0]
        flat = " ".join(relative["title"].split())
        assert "board-relative" in flat and "(100, 50)" in flat
        assert "kct check" in flat

        absolute = board_view(str(board), "overview", absolute=True)
        assert absolute["frame"] == "sheet-absolute"
        assert absolute["board_origin_mm"] == [100.0, 50.0]
        assert absolute["window_mm"] == [99.0, 49.0, 141.0, 81.0]
        assert "sheet-absolute" in " ".join(absolute["title"].split())


# ---------------------------------------------------------------------------
# Missing links
# ---------------------------------------------------------------------------


class TestMissingLinks:
    def test_one_link_per_open_connection_between_closest_pads(self, board: Path):
        report = missing_links(PCB.load(board))
        assert set(report.links) == {"OPEN"}
        (link,) = report.links["OPEN"]
        # R2.2 (26, 10) is nearer R3.1 (24, 20) than R1.2 (11, 10) is.
        assert (link.a.name, link.b.name) == ("R2.2", "R3.1")
        assert (link.a.x, link.a.y) == pytest.approx((26.0, 10.0))
        assert (link.b.x, link.b.y) == pytest.approx((24.0, 20.0))
        assert link.length_mm == pytest.approx(math.hypot(2.0, 10.0))
        assert link.to_dict() == {
            "from": "R2.2",
            "to": "R3.1",
            "from_mm": [26.0, 10.0],
            "to_mm": [24.0, 20.0],
            "length_mm": 10.198,
        }

    def test_link_count_matches_open_connections_for_every_net(self, cut_board: Path):
        pcb = PCB.load(cut_board)
        report = missing_links(pcb)
        assert set(report.links) == {"OPEN", "SIG"}
        for status in NetStatusAnalyzer(pcb).analyze().nets:
            if status.net_name in report.links:
                assert len(report.links[status.net_name]) == status.open_connections
        assert report.link_count == 2

    def test_links_follow_the_selected_frame(self, board: Path):
        (link,) = missing_links(PCB.load(board), absolute=True).links["OPEN"]
        assert (link.a.x, link.a.y) == pytest.approx((126.0, 60.0))
        assert (link.b.x, link.b.y) == pytest.approx((124.0, 70.0))

    def test_removed_trace_is_named_by_list(self, board: Path, cut_board: Path):
        """A fixture with one trace removed: ``list`` names the net and pad pair."""
        before = board_view(str(board), "list")
        assert before["success"]
        assert "SIG" not in before["missing_links"]

        after = board_view(str(cut_board), "list")
        assert after["success"]
        assert after["unfinished_net_count"] == 2
        assert after["missing_link_count"] == 2
        (pair,) = after["missing_links"]["SIG"]
        assert {pair["from"], pair["to"]} == {"R1.1", "R2.1"}
        assert pair["length_mm"] == pytest.approx(15.0)
        assert "image_base64" not in after

    def test_net_view_draws_the_ratsnest_line_between_those_pads(self, cut_board: Path):
        pcb = PCB.load(cut_board)
        geo = collect_geometry(pcb)
        report = missing_links(pcb)
        box = render_module.net_window(geo, report, "SIG")
        assert box is not None
        ax = _axes(geo, report, box, net="SIG")

        dashed = [line for line in ax.lines if line.get_linestyle() == "--"]
        assert len(dashed) == 1, "exactly the highlighted net's link is drawn dashed"
        xs, ys = dashed[0].get_xdata(), dashed[0].get_ydata()
        ends = {(round(float(x), 6), round(float(y), 6)) for x, y in zip(xs, ys, strict=True)}
        assert ends == {(9.0, 10.0), (24.0, 10.0)}  # R1.1 and R2.1 pad centres
        # OPEN's link, where it crosses this window, is the dotted "other net" style.
        assert all(line.get_linestyle() != "--" or line is dashed[0] for line in ax.lines)

    def test_net_view_crops_round_the_missing_link(self, cut_board: Path):
        pytest.importorskip("matplotlib")
        result = board_view(str(cut_board), "net", net="SIG", margin_mm=4.0)
        assert result["success"], result["error_message"]
        x0, y0, x1, y1 = result["window_mm"]
        assert (x0, x1) == pytest.approx((5.0, 28.0))  # link ends 9 and 24, 4 mm margin
        assert y1 - y0 == pytest.approx(12.0)  # widened from 8 mm: never a sliver
        assert "its 1 missing link magenta dashed" in " ".join(result["title"].split())

    def test_complete_net_is_cropped_round_its_pads(self, board: Path):
        pytest.importorskip("matplotlib")
        result = board_view(str(board), "net", net="SIG")
        assert result["success"], result["error_message"]
        assert "it has no missing links" in " ".join(result["title"].split())
        x0, _, x1, _ = result["window_mm"]
        assert (x0, x1) == pytest.approx((5.0, 28.0))


# ---------------------------------------------------------------------------
# Zone-owning nets: the gate kct check applies
# ---------------------------------------------------------------------------


class TestPourGate:
    def test_precondition_gnd_is_incomplete_only_by_an_advisory_residual(self, board: Path):
        gnd = NetStatusAnalyzer(PCB.load(board)).analyze().get_net("GND")
        assert gnd is not None
        assert gnd.status == "incomplete"
        assert gnd.has_filled_zone and gnd.is_advisory_incomplete

    def test_advisory_pour_net_gets_no_link_and_its_own_key(self, board: Path):
        report = missing_links(PCB.load(board))
        assert "GND" not in report.links
        assert report.pour_advisory == [
            {
                "net": "GND",
                "open_connections": 1,
                "islands": [["R3.2"], ["R4.1"]],
                "zone_layers": ["F.Cu"],
            }
        ]

    def test_links_are_exactly_the_nets_kct_check_reports(self, board: Path, cut_board: Path):
        """The image and the connectivity rule cannot disagree."""
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.validate.rules.connectivity import ConnectivityRule

        rules = get_profile("jlcpcb").get_design_rules(2, 1.0)
        for path in (board, cut_board):
            pcb = PCB.load(path)
            results = ConnectivityRule().check(pcb, rules)
            flagged = {
                net
                for net in ("SIG", "OPEN", "GND")
                if any(f"'{net}'" in violation.message for violation in results.violations)
            }
            assert flagged == set(missing_links(pcb).links)
            assert results.error_count == len(flagged)

    def test_zone_net_without_fill_is_not_excluded(self, board: Path, tmp_path: Path):
        """Owning a zone is not enough: no fill copper, so the open is real."""
        text = board_text()
        start = text.index("    (filled_polygon")
        end = text.index("  )\n)", start)
        unfilled = tmp_path / "unfilled.kicad_pcb"
        unfilled.write_text(text[:start] + text[end:])

        report = missing_links(PCB.load(unfilled))
        assert report.pour_advisory == []
        (link,) = report.links["GND"]
        assert {link.a.name, link.b.name} == {"R3.2", "R4.1"}

    def test_result_reports_pours_as_not_drawn(self, board: Path):
        pytest.importorskip("matplotlib")
        result = board_view(str(board), "overview")
        assert result["pours_drawn"] is False
        assert [entry["net"] for entry in result["pour_advisory"]] == ["GND"]
        flat = " ".join(result["title"].split())
        assert "Zone pours are not drawn" in flat
        assert "advisory in kct check, no link drawn): GND" in flat


# ---------------------------------------------------------------------------
# Image size, title, labels
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("board")
class TestImage:
    @pytest.fixture(autouse=True)
    def _needs_matplotlib(self):
        pytest.importorskip("matplotlib")

    def test_cap_matches_the_screenshot_tools(self):
        from kicad_tools.mcp.tools.screenshot import MAX_VISION_API_PX as screenshot_cap

        assert MAX_VISION_API_PX == screenshot_cap == 1568

    @pytest.mark.parametrize(
        ("outline", "long_side"),
        [((400.0, 80.0), "width"), ((140.0, 350.0), "height"), ((140.0, 80.0), "width")],
        ids=["wide-board", "tall-board", "ordinary-board"],
    )
    def test_longest_side_is_capped(self, tmp_path: Path, outline, long_side):
        path = tmp_path / "shape.kicad_pcb"
        path.write_text(board_text(outline=outline))
        result = board_view(str(path), "overview", output_path=str(tmp_path / "o.png"))
        assert result["success"], result["error_message"]

        width, height = _png_size((tmp_path / "o.png").read_bytes())
        assert (result["width_px"], result["height_px"]) == (width, height)
        assert max(width, height) <= 1568
        # The cap is used, not merely respected: the long side reaches it.
        assert result[f"{long_side}_px"] >= 1560

    def test_layers_view_is_capped_and_has_a_panel_per_copper_layer(self, cut_board: Path):
        result = board_view(str(cut_board), "layers", net="SIG")
        assert result["success"], result["error_message"]
        assert max(result["width_px"], result["height_px"]) <= 1568
        single = board_view(str(cut_board), "net", net="SIG")
        assert result["window_mm"] == single["window_mm"], "same crop as the net view"
        assert result["px_per_mm"] < single["px_per_mm"], "two panels share the width"

    @pytest.mark.parametrize("cap", [400, 800])
    def test_max_size_is_honoured(self, board: Path, cap: int):
        for view, kwargs in (("overview", {}), ("layers", {"net": "SIG"})):
            result = board_view(str(board), view, max_size_px=cap, **kwargs)
            assert result["success"], result["error_message"]
            assert max(result["width_px"], result["height_px"]) <= cap

    def test_image_is_not_blank(self, board: Path):
        """The scratch renderer drew an empty board until the frame was fixed."""
        import numpy as np
        from matplotlib.image import imread

        result = board_view(str(board), "overview")
        import base64

        pixels = imread(io.BytesIO(base64.b64decode(result["image_base64"])))[..., :3]
        # F.Cu copper is red-dominant; the background and the text are not.
        red = (pixels[..., 0] > 0.6) & (pixels[..., 1] < 0.45) & (pixels[..., 2] < 0.45)
        assert int(np.count_nonzero(red)) > 2000

    def test_title_spells_out_the_colour_key_and_the_caveat(self, board: Path):
        result = board_view(str(board), "overview")
        flat = " ".join(result["title"].split())
        assert "F.Cu red" in flat and "B.Cu blue" in flat
        assert "through-hole pads gold" in flat
        assert "magenta dashed" in flat
        assert "1 unfinished nets, 1 missing links" in flat
        assert CAVEAT in flat
        assert result["caveat"] == CAVEAT
        assert result["color_key"]["F.Cu"] == "red"
        assert result["color_key"]["missing_links"] == "magenta dashed"
        # The wrapped title fits the image: no line is wider than the figure.
        widest = max(len(line) for line in result["title"].splitlines())
        assert widest * 7.6 <= result["width_px"]

    def test_pad_net_labels_only_when_the_crop_is_small_enough(self, board: Path):
        pcb = PCB.load(board)
        geo = collect_geometry(pcb)
        report = missing_links(pcb)
        assert geo.bounds is not None

        def texts(px_per_mm: float) -> set[str]:
            ax = _axes(geo, report, geo.bounds, net="SIG", px_per_mm=px_per_mm)
            return {text.get_text() for text in ax.texts}

        coarse = texts(10.0)
        assert {"R1", "R2", "R3", "R4"} <= coarse, "reference designators always"
        assert not any(":" in text or text in {"OPEN", "GND"} for text in coarse)

        fine = texts(60.0)
        assert {"R1", "R2", "R3", "R4"} <= fine
        assert {"1:SIG", "OPEN", "GND"} <= fine, "highlighted net as pad:net, others by net"

        assert board_view(str(board), "overview")["pad_labels_drawn"] is False
        crop = board_view(str(board), "crop", window=(5, 6, 15, 14))
        assert crop["pad_labels_drawn"] is True

    def test_crop_takes_an_explicit_window_in_either_corner_order(self, board: Path):
        result = board_view(str(board), "crop", window=[30, 25, 5, 5], net="OPEN")
        assert result["success"], result["error_message"]
        assert result["window_mm"] == [5.0, 5.0, 30.0, 25.0]
        assert result["net"] == "OPEN"

    def test_output_path_is_written(self, board: Path, tmp_path: Path):
        out = tmp_path / "nested" / "view.png"
        result = board_view(str(board), "overview", output_path=str(out))
        assert result["output_path"] == str(out)
        assert _png_size(out.read_bytes()) == (result["width_px"], result["height_px"])
        assert board_view(str(board), "overview")["output_path"] is None


class TestArcs:
    def test_arc_polyline_passes_through_all_three_points(self):
        points = arc_points((10.0, 10.0), (15.0, 5.0), (20.0, 10.0))
        assert points[0] == (10.0, 10.0) and points[-1] == (20.0, 10.0)
        assert len(points) > 10
        # Centre (15, 10), radius 5: every point is on the circle ...
        assert all(math.hypot(x - 15.0, y - 10.0) == pytest.approx(5.0) for x, y in points)
        # ... and on the mid point's side of the chord, not the far side.
        assert all(y <= 10.0 + 1e-9 for _, y in points)
        assert any(_is_point(p, (15.0, 5.0), tol=1e-6) for p in points)

    def test_collinear_arc_degrades_to_its_points(self):
        assert arc_points((0.0, 0.0), (1.0, 0.0), (2.0, 0.0)) == [
            (0.0, 0.0),
            (1.0, 0.0),
            (2.0, 0.0),
        ]

    def test_copper_arc_tracks_are_drawn(self, tmp_path: Path):
        pytest.importorskip("matplotlib")
        pcb = PCB.load(ARC_FIXTURE)
        assert pcb.arcs and not pcb.segments
        geo = collect_geometry(pcb)
        (track,) = geo.tracks
        assert len(track.points) > 2
        assert track.start == pcb.arcs[0].start and track.end == pcb.arcs[0].end

        # No Edge.Cuts on this fixture: the window falls back to the copper.
        result = board_view(str(ARC_FIXTURE), "overview", output_path=str(tmp_path / "arc.png"))
        assert result["success"], result["error_message"]
        assert result["missing_links"] == {}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestErrors:
    def test_missing_file_and_wrong_suffix(self, tmp_path: Path):
        missing = board_view(str(tmp_path / "nope.kicad_pcb"), "list")
        assert missing["success"] is False
        assert "not found" in missing["error_message"]

        other = tmp_path / "x.kicad_sch"
        other.write_text("")
        wrong = board_view(str(other), "list")
        assert wrong["success"] is False
        assert ".kicad_pcb" in wrong["error_message"]

    def test_failure_has_the_screenshot_result_shape(self, tmp_path: Path):
        result = board_view(str(tmp_path / "nope.kicad_pcb"), "overview")
        assert result["image_base64"] is None
        assert (result["width_px"], result["height_px"]) == (0, 0)
        assert result["output_path"] is None

    def test_unknown_view_net_and_missing_arguments(self, board: Path):
        assert "Unknown view" in board_view(str(board), "sideways")["error_message"]
        assert "needs a net name" in board_view(str(board), "net")["error_message"]
        if render_module.matplotlib_available():
            unknown = board_view(str(board), "net", net="SIGG")
            assert unknown["success"] is False
            assert "Did you mean: SIG" in unknown["error_message"]
            assert "needs a window" in board_view(str(board), "crop")["error_message"]
            assert (
                "max_size_px" in board_view(str(board), "overview", max_size_px=10)["error_message"]
            )

    def test_unreadable_board_is_an_error_not_a_traceback(self, tmp_path: Path):
        broken = tmp_path / "broken.kicad_pcb"
        broken.write_text("(kicad_pcb (version")
        result = board_view(str(broken), "list")
        assert result["success"] is False
        assert "Could not read" in result["error_message"]


class TestMissingMatplotlib:
    @pytest.fixture(autouse=True)
    def _no_matplotlib(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(render_module, "matplotlib_available", lambda: False)

    def test_image_views_fail_with_an_install_hint(self, board: Path):
        for view, kwargs in (
            ("overview", {}),
            ("net", {"net": "SIG"}),
            ("layers", {"net": "SIG"}),
            ("crop", {"window": (0, 0, 10, 10)}),
        ):
            result = board_view(str(board), view, **kwargs)
            assert result["success"] is False
            assert "kicad-tools[visualization]" in result["error_message"]
            assert result["image_base64"] is None

    def test_list_still_works(self, board: Path):
        result = board_view(str(board), "list")
        assert result["success"] is True
        assert set(result["missing_links"]) == {"OPEN"}

    def test_cli_exits_non_zero_without_a_traceback(self, board: Path, capsys, tmp_path: Path):
        code = kct_main(["board-view", "overview", str(board), "-o", str(tmp_path / "o.png")])
        captured = capsys.readouterr()
        assert code == 1
        assert "kicad-tools[visualization]" in captured.err
        assert "Traceback" not in captured.err + captured.out
        assert not (tmp_path / "o.png").exists()

    def test_cli_json_error_document(self, board: Path, capsys):
        code = kct_main(["board-view", "overview", str(board), "--format", "json"])
        document = json.loads(capsys.readouterr().out)
        assert code == 1
        assert document["success"] is False
        assert document["command"] == "board-view"
        assert "kicad-tools[visualization]" in document["error"]

    def test_mcp_tool_reports_the_hint(self, board: Path):
        from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

        result = TOOL_REGISTRY["board_view"].handler({"pcb_path": str(board)})
        assert result["success"] is False
        assert "kicad-tools[visualization]" in result["error_message"]
        assert "_mcp_content" not in result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_list_prints_json_by_default(self, cut_board: Path, capsys):
        assert kct_main(["board-view", "list", str(cut_board)]) == 0
        document = json.loads(capsys.readouterr().out)
        assert document["command"] == "board-view"
        assert document["view"] == "list"
        assert document["frame"] == "board-relative"
        assert document["board_origin_mm"] == [100.0, 50.0]
        assert set(document["missing_links"]) == {"OPEN", "SIG"}
        assert [entry["net"] for entry in document["pour_advisory"]] == ["GND"]
        assert "output" not in document and "image_base64" not in document

    def test_list_text_and_absolute(self, cut_board: Path, capsys):
        assert (
            kct_main(["board-view", "list", str(cut_board), "--format", "text", "--absolute"]) == 0
        )
        out = capsys.readouterr().out
        assert "Frame: sheet-absolute" in out
        assert "Unfinished nets: 2 (2 missing links)" in out
        assert "SIG: R1.1 -> R2.1  15.00 mm" in out
        assert "GND: pour leaves 2 separate islands" in out

    def test_every_image_view_writes_a_png(self, cut_board: Path, tmp_path: Path, capsys):
        pytest.importorskip("matplotlib")
        cases = {
            "overview": ["overview", str(cut_board)],
            "net": ["net", str(cut_board), "SIG"],
            "layers": ["layers", str(cut_board), "SIG", "--margin", "6"],
            "crop": ["crop", str(cut_board), "5", "5", "30", "25", "--net", "OPEN"],
        }
        for name, argv in cases.items():
            out = tmp_path / f"{name}.png"
            assert kct_main(["board-view", *argv, "-o", str(out)]) == 0, name
            width, height = _png_size(out.read_bytes())
            assert max(width, height) <= 1568
            text = capsys.readouterr().out
            assert f"Board view saved to {out}" in text
            assert "Frame: board-relative" in text
            assert "Note: An image reading is a hypothesis" in text

    def test_json_document_and_default_output_name(self, cut_board: Path, capsys):
        pytest.importorskip("matplotlib")
        assert kct_main(["board-view", "net", str(cut_board), "SIG", "--format", "json"]) == 0
        document = json.loads(capsys.readouterr().out)
        expected = cut_board.with_name("cut-net-SIG.png")
        assert document["output"] == str(expected)
        assert expected.exists()
        assert document["success"] is True
        assert document["net"] == "SIG"
        assert document["input"] == str(cut_board)
        assert max(document["width_px"], document["height_px"]) <= 1568
        assert "image_base64" not in document
        assert document["missing_links"]["SIG"][0]["length_mm"] == 15.0

    def test_unknown_net_exits_non_zero(self, board: Path, capsys, tmp_path: Path):
        pytest.importorskip("matplotlib")
        out = tmp_path / "o.png"
        assert kct_main(["board-view", "net", str(board), "NOPE", "-o", str(out)]) == 1
        assert "Error: Net 'NOPE'" in capsys.readouterr().err
        assert not out.exists()

    def test_no_view_prints_usage(self, capsys):
        assert kct_main(["board-view"]) == 1
        assert "overview, net, layers, crop, list" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------


class TestMcpTool:
    def test_registered_beside_screenshot_board(self):
        from kicad_tools.mcp.tools.registry import TOOL_REGISTRY, list_tools

        spec = TOOL_REGISTRY["board_view"]
        assert spec.category == "screenshot"
        assert {"screenshot_board", "board_view"} <= {t.name for t in list_tools("screenshot")}
        properties = spec.parameters["properties"]
        assert spec.parameters["required"] == ["pcb_path"]
        assert properties["view"]["enum"] == ["overview", "net", "layers", "crop", "list"]
        assert {"net", "window", "absolute", "max_size_px", "output_path"} <= set(properties)
        # The description is the only guidance an MCP-only agent sees.
        assert "hypothesis" in spec.description

    def test_image_view_returns_text_plus_one_image_block(self, cut_board: Path, tmp_path: Path):
        pytest.importorskip("matplotlib")
        from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

        out = tmp_path / "mcp.png"
        result = TOOL_REGISTRY["board_view"].handler(
            {"pcb_path": str(cut_board), "view": "net", "net": "SIG", "output_path": str(out)}
        )
        assert result["success"], result["error_message"]
        # Same result shape as screenshot_board ...
        assert {"image_base64", "width_px", "height_px", "output_path", "error_message"} <= set(
            result
        )
        assert result["output_path"] == str(out) and out.exists()
        # ... plus the missing-link JSON.
        assert set(result["missing_links"]) == {"OPEN", "SIG"}

        content = result["_mcp_content"]
        assert [block["type"] for block in content] == ["text", "image"]
        assert content[1]["mimeType"] == "image/png"
        assert content[1]["data"] == result["image_base64"]
        meta = json.loads(content[0]["text"])
        assert "image_base64" not in meta
        assert meta["frame"] == "board-relative"
        assert meta["missing_links"]["SIG"][0]["from"] in {"R1.1", "R2.1"}

    def test_crop_window_and_list_view(self, cut_board: Path):
        from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

        handler = TOOL_REGISTRY["board_view"].handler
        listing = handler({"pcb_path": str(cut_board), "view": "list", "absolute": True})
        assert listing["success"] and listing["frame"] == "sheet-absolute"
        assert "_mcp_content" not in listing
        assert listing["missing_links"]["SIG"][0]["length_mm"] == 15.0

        if render_module.matplotlib_available():
            crop = handler(
                {
                    "pcb_path": str(cut_board),
                    "view": "crop",
                    "window": [5, 5, 30, 25],
                    "max_size_px": 600,
                }
            )
            assert crop["success"], crop["error_message"]
            assert max(crop["width_px"], crop["height_px"]) <= 600
            assert len([b for b in crop["_mcp_content"] if b["type"] == "image"]) == 1


# ---------------------------------------------------------------------------
# Board 05 (the board the issue's experiment ran on)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def board_05_listing() -> dict:
    return board_view(str(BOARD_05), "list")


class TestBoard05:
    def test_list_reports_no_unfinished_nets(self, board_05_listing: dict):
        """Board 05's LVS is clean, zone-owning GND and +5V included."""
        listing = board_05_listing
        assert listing["success"], listing["error_message"]
        assert listing["missing_links"] == {}
        assert listing["pour_advisory"] == []
        assert listing["unfinished_net_count"] == 0
        assert listing["board_origin_mm"] == [113.5, 60.0]

    @pytest.mark.parametrize(
        ("view", "kwargs"),
        [
            ("overview", {}),
            ("net", {"net": "PHASE_B"}),
            ("layers", {"net": "PHASE_B"}),
            ("crop", {"window": (20.0, 30.0, 50.0, 50.0), "net": "PWM_A"}),
        ],
        ids=["overview", "net", "layers", "crop"],
    )
    def test_image_views_run(self, view: str, kwargs: dict, tmp_path: Path):
        pytest.importorskip("matplotlib")
        out = tmp_path / f"{view}.png"
        result = board_view(str(BOARD_05), view, output_path=str(out), **kwargs)
        assert result["success"], result["error_message"]
        assert _png_size(out.read_bytes()) == (result["width_px"], result["height_px"])
        assert max(result["width_px"], result["height_px"]) <= 1568
        assert result["frame"] == "board-relative"
        assert list(result["color_key"])[:4] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
