"""Panel copies are rigid motions of the source board (Issue #6125).

The old ``_offset_positions`` walk shifted every ``(at)``/``(start)``/
``(end)``/``(mid)`` in a cloned subtree -- including footprint-local pad and
text positions -- and never touched ``(xy ...)``, so footprint keepouts and
board-level zone / ``gr_poly`` outlines stayed at the source location.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapely", reason="Shapely required for panel tests")

from kicad_tools.core.sexp_file import load_pcb  # noqa: E402
from kicad_tools.panel.panel import Panel  # noqa: E402
from kicad_tools.sexp.parser import SExp  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "fp_zone_transform" / "base.kicad_pcb"

# Board-level items the fixture lacks: a zone, a gr_poly, a track segment,
# an arc track, a via and a gr_text -- all in board coordinates.
_EXTRA_ITEMS = """
	(net 1 "N1")
	(zone
		(net 1)
		(net_name "N1")
		(layer "B.Cu")
		(uuid "11111111-1111-1111-1111-111111111111")
		(hatch edge 0.5)
		(connect_pads (clearance 0.5))
		(min_thickness 0.25)
		(fill (thermal_gap 0.5) (thermal_bridge_width 0.5))
		(polygon (pts (xy 102 97) (xy 110 97) (xy 110 103) (xy 102 103)))
	)
	(gr_poly
		(pts (xy 130 120) (xy 135 120) (xy 133 125))
		(stroke (width 0.1) (type solid))
		(fill no)
		(layer "F.SilkS")
		(uuid "22222222-2222-2222-2222-222222222222")
	)
	(gr_text "HELLO"
		(at 130 100 0)
		(layer "F.SilkS")
		(uuid "33333333-3333-3333-3333-333333333333")
		(effects (font (size 1 1) (thickness 0.15)))
	)
	(segment (start 105 120) (end 110 125) (width 0.25) (layer "F.Cu") (net 1)
		(uuid "44444444-4444-4444-4444-444444444444"))
	(arc (start 110 125) (mid 112 126) (end 114 125) (width 0.25) (layer "F.Cu") (net 1)
		(uuid "55555555-5555-5555-5555-555555555555"))
	(via (at 114 125) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)
		(uuid "66666666-6666-6666-6666-666666666666"))
"""


@pytest.fixture
def board(tmp_path: Path) -> Path:
    text = FIXTURE.read_text()
    idx = text.rindex("(embedded_fonts no)\n)")
    out = tmp_path / "board.kicad_pcb"
    out.write_text(text[:idx] + _EXTRA_ITEMS.lstrip("\n") + "\t" + text[idx:])
    return out


def _points(node: SExp) -> list[tuple[str, float, float]]:
    """Every point-bearing node in *node*'s subtree, in document order."""
    out: list[tuple[str, float, float]] = []
    if node.name in ("at", "start", "mid", "end", "center", "xy"):
        x, y = node.get_float(0), node.get_float(1)
        if x is not None and y is not None:
            out.append((node.name, x, y))
    for child in node.children:
        if not child.is_atom:
            out.extend(_points(child))
    return out


def _items(root: SExp, tag: str) -> list[SExp]:
    return [c for c in root.children if c.name == tag]


def _copy_of(items: list[SExp], panel: Panel, instance_index: int) -> SExp:
    """The *instance_index*-th clone (copies are appended per instance)."""
    per_copy = len(items) // panel.board_count
    assert per_copy == 1, "fixture items are unique per board"
    return items[instance_index]


def _deltas(panel: Panel) -> list[tuple[float, float]]:
    min_x, min_y = panel._board_bounds[0], panel._board_bounds[1]
    return [(i.offset_x - min_x, i.offset_y - min_y) for i in panel.instances]


class TestTranslatedCopies:
    def test_footprint_local_coordinates_unchanged(self, board: Path) -> None:
        src = load_pcb(board)
        src_fp = _items(src, "footprint")[0]
        panel = Panel().append_board(board, rows=2, cols=2, spacing=3.0)
        out = panel.build()

        fps = _items(out, "footprint")
        assert len(fps) == 4
        src_local = {
            child.name + str(i): child.find_child("at").values
            for i, child in enumerate(src_fp.children)
            if child.name in ("pad", "property", "fp_text") and child.find_child("at")
        }
        assert src_local, "fixture footprint has pads and properties"
        for k, (dx, dy) in enumerate(_deltas(panel)):
            fp = _copy_of(fps, panel, k)
            local = {
                child.name + str(i): child.find_child("at").values
                for i, child in enumerate(fp.children)
                if child.name in ("pad", "property", "fp_text") and child.find_child("at")
            }
            # Pad (-2, 1, 20) stays (-2, 1, 20) -- not (-2 + dx, 1 + dy).
            assert local == src_local

            sx, sy = src_fp.find_child("at").get_float(0), src_fp.find_child("at").get_float(1)
            at = fp.find_child("at")
            assert (at.get_float(0), at.get_float(1)) == pytest.approx((sx + dx, sy + dy))

    def test_footprint_zones_move_with_copy(self, board: Path) -> None:
        src_fp = _items(load_pcb(board), "footprint")[0]
        src_pts = [p for z in _items(src_fp, "zone") for p in _points(z)]
        assert any(tag == "xy" for tag, _, _ in src_pts)

        panel = Panel().append_board(board, rows=2, cols=2, spacing=3.0)
        fps = _items(panel.build(), "footprint")
        firsts = set()
        for k, (dx, dy) in enumerate(_deltas(panel)):
            pts = [p for z in _items(fps[k], "zone") for p in _points(z)]
            assert [t for t, _, _ in pts] == [t for t, _, _ in src_pts]
            for (_, x, y), (_, sx, sy) in zip(pts, src_pts, strict=True):
                assert (x, y) == pytest.approx((sx + dx, sy + dy), abs=1e-6)
            firsts.add((round(pts[0][1], 6), round(pts[0][2], 6)))
        # Four copies, four distinct keepout locations.
        assert len(firsts) == 4

    @pytest.mark.parametrize("tag", ["zone", "gr_poly", "gr_text", "segment", "arc", "via"])
    def test_board_level_items_translate(self, board: Path, tag: str) -> None:
        src_item = _items(load_pcb(board), tag)[0]
        src_pts = _points(src_item)
        assert src_pts

        panel = Panel().append_board(board, rows=2, cols=2, spacing=3.0)
        items = _items(panel.build(), tag)
        assert len(items) == 4
        for k, (dx, dy) in enumerate(_deltas(panel)):
            pts = _points(_copy_of(items, panel, k))
            assert [t for t, _, _ in pts] == [t for t, _, _ in src_pts]
            for (_, x, y), (_, sx, sy) in zip(pts, src_pts, strict=True):
                assert (x, y) == pytest.approx((sx + dx, sy + dy), abs=1e-6)

    def test_copies_land_inside_their_outline(self, board: Path) -> None:
        """Content sits inside its copy's Edge.Cuts cell.

        ``BoardGeometry`` bounds are board-relative but the cloned nodes are
        sheet-absolute; mixing them displaced every copy's content from its
        outline by the board origin (here (100, 95)).
        """
        panel = Panel().append_board(board, rows=2, cols=2, spacing=3.0)
        out = panel.build()
        for tag in ("footprint", "zone", "gr_poly", "segment", "via"):
            for k, inst in enumerate(panel.instances):
                item = _copy_of(_items(out, tag), panel, k)
                if tag == "footprint":
                    at = item.find_child("at")
                    pts = [("at", at.get_float(0), at.get_float(1))]
                else:
                    pts = _points(item)
                for _, x, y in pts:
                    assert inst.bounds[0] <= x <= inst.bounds[2], (tag, k, x)
                    assert inst.bounds[1] <= y <= inst.bounds[3], (tag, k, y)

    def test_via_at_gets_no_angle(self, board: Path) -> None:
        panel = Panel().append_board(board, rows=1, cols=2, rotation=90)
        for via in _items(panel.build(), "via"):
            assert len(via.find_child("at").children) == 2

    def test_precision_not_snapped_to_hundredths(self, board: Path) -> None:
        """Copies keep 0.0254-mm-grade coordinates (fmt() rounds to 0.01)."""
        src_fp = _items(load_pcb(board), "footprint")[0]
        src_fill = [p for z in _items(src_fp, "zone") for p in _points(z)]
        panel = Panel().append_board(board, rows=1, cols=1)
        fp = _items(panel.build(), "footprint")[0]
        fill = [p for z in _items(fp, "zone") for p in _points(z)]
        (dx, dy) = _deltas(panel)[0]
        for (_, x, y), (_, sx, sy) in zip(fill, src_fill, strict=True):
            assert x == pytest.approx(sx + dx, abs=1e-9)
            assert y == pytest.approx(sy + dy, abs=1e-9)


class TestRotatedCopies:
    def test_non_quarter_rotation_rejected(self, board: Path) -> None:
        with pytest.raises(ValueError, match="multiple of 90"):
            Panel().append_board(board, rotation=30)

    def test_quarter_turn_is_rigid(self, board: Path) -> None:
        src = load_pcb(board)
        src_fp = _items(src, "footprint")[0]
        panel = Panel().append_board(board, rows=1, cols=2, spacing=2.0, rotation=90)
        out = panel.build()

        # Quarter turn swaps the cell dimensions (45 x 40 board).
        inst = panel.instances[0]
        w = inst.bounds[2] - inst.bounds[0]
        h = inst.bounds[3] - inst.bounds[1]
        assert (w, h) == pytest.approx((40.0, 45.0))

        fp = _items(out, "footprint")[0]
        at = fp.find_child("at")
        assert at.get_float(2) == pytest.approx(90.0)

        # Pad positions stay local; its board-absolute angle turns by 90.
        src_pad = src_fp.find_child("pad").find_child("at")
        pad = fp.find_child("pad").find_child("at")
        assert (pad.get_float(0), pad.get_float(1)) == (src_pad.get_float(0), src_pad.get_float(1))
        assert pad.get_float(2) == pytest.approx((src_pad.get_float(2) + 90.0) % 360.0)

        # The keepout keeps its pose relative to the footprint anchor:
        # KiCad Y-down RotatePoint by +90 maps (dx, dy) -> (dy, -dx).
        sax, say = src_fp.find_child("at").get_float(0), src_fp.find_child("at").get_float(1)
        ax, ay = at.get_float(0), at.get_float(1)
        src_pts = [p for z in _items(src_fp, "zone") for p in _points(z)]
        pts = [p for z in _items(fp, "zone") for p in _points(z)]
        for (_, x, y), (_, sx, sy) in zip(pts, src_pts, strict=True):
            assert (x - ax, y - ay) == pytest.approx((sy - say, -(sx - sax)), abs=1e-6)

        # Everything lands inside the copy's cell.
        for _, x, y in _points(fp.find_child("zone")):
            assert inst.bounds[0] - 1e-6 <= x <= inst.bounds[2] + 1e-6
            assert inst.bounds[1] - 1e-6 <= y <= inst.bounds[3] + 1e-6


class TestNetNamesFollowPanelTable:
    """Pad ``(net N "name")`` and zone ``(net "name")`` names get the prefix.

    KiCad resolves a pad's net by name, so an unprefixed ``"N1"`` against a
    panel table of ``"B0/N1"`` left every pad on ``<no net>`` -- shorting
    each copy's tracks to its own pads in ``kicad-cli pcb drc``.
    """

    def test_pad_and_zone_net_names_prefixed(self, board: Path) -> None:
        text = board.read_text().replace('(net 1)\n\t\t(net_name "N1")', '(net "N1")')
        board.write_text(text)
        panel = Panel().append_board(board, rows=1, cols=2)
        out = panel.build()
        zones = _items(out, "zone")
        assert [z.find_child("net").get_string(0) for z in zones] == ["B0/N1", "B1/N1"]

    def test_numbered_net_name_rewritten(self) -> None:
        from kicad_tools.panel.panel import _remap_nets

        pad = SExp.list("pad", "1", SExp.list("net", 1, "VIN"))
        _remap_nets(pad, 2, lambda _i, n: 7 if n == 1 else 0)
        net = pad.find_child("net")
        assert net.get_value(0) == 7
        assert net.get_string(1) == "B2/VIN"

        unconnected = SExp.list("pad", "2", SExp.list("net", 0, ""))
        _remap_nets(unconnected, 2, lambda _i, n: 0)
        assert unconnected.find_child("net").get_string(1) == ""
