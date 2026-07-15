"""Tests for ``zones batch`` auto-prioritization + carve (issue #4167).

``kct zones batch --power-nets 'A:F.Cu,B:F.Cu,...'`` used to add every
zone at the full board outline with a hardcoded priority (GND=1, else=0).
Any two same-layer nets therefore 100%-overlapped, and KiCad's fill
resolver awarded the whole board to the highest-priority zone -- the
lower-priority siblings received zero copper despite being declared.

The fix (mirroring the ``kct route`` auto-pour allocator) assigns
priorities by ascending pad-cluster bbox area (smallest area => highest
priority) within each user-specified layer group and carves the outlines
so overlapping zones become geometrically disjoint.  These tests build
clean synthetic boards (no pre-existing zones) so they can assert
precisely on the batch-internal behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.zones import (
    ZonePartitionError,
    assign_batch_zone_priorities_and_outlines,
)
from kicad_tools.zones.generator import ZoneGenerator

pytest.importorskip(
    "shapely",
    reason=(
        "shapely is required for the zone-priority outline allocator; "
        "install with: pip install kicad-tools[geometry]"
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _polygon_area(polygon: list[tuple[float, float]]) -> float:
    n = len(polygon)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    return abs(area) / 2.0


def _polygons_overlap_area(
    a: list[tuple[float, float]],
    b: list[tuple[float, float]],
) -> float:
    from shapely.geometry import Polygon

    pa = Polygon(a)
    pb = Polygon(b)
    if not pa.is_valid or not pb.is_valid:
        return 0.0
    return pa.intersection(pb).area


_PCB_HEADER = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
"""


def _fp(ref: str, x: float, y: float, net_num: int, net_name: str) -> str:
    return f"""  (footprint "Test:{ref}"
    (layer "F.Cu")
    (at {x} {y})
    (uuid "fp-{ref}-uuid")
    (property "Reference" "{ref}"
      (at 0 -2 0) (layer "F.SilkS") (uuid "{ref}-ref-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "T"
      (at 0 2 0) (layer "F.Fab") (uuid "{ref}-val-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net {net_num} "{net_name}"))
  )"""


def _write_pcb(tmp_path: Path, name: str, nets: list[str], footprints: list[str]) -> Path:
    text = _PCB_HEADER
    text += '  (net 0 "")\n'
    for i, net in enumerate(nets, start=1):
        text += f'  (net {i} "{net}")\n'
    text += """  (gr_rect
    (start 0 0)
    (end 100 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
"""
    text += "\n".join(footprints)
    text += "\n)\n"
    p = tmp_path / name
    p.write_text(text)
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_interleaved_3net_pcb(tmp_path: Path) -> Path:
    """3 power nets on F.Cu with distinct pad-cluster bbox areas.

    Models the reporter's interleaved-power-rail case: the higher-priority
    (smaller) nets reach across the wide net's full height (touching its
    top and bottom edges) at different x-locations, so carving splits the
    wide net into disjoint side strips rather than leaving an
    unrepresentable interior hole.

    - BIG spans the full width, full height (largest bbox => lowest priority)
    - MID occupies a mid-x column across the full height (medium bbox)
    - SMALL occupies a narrow column across the full height (smallest bbox
      => highest priority)
    """
    nets = ["BIG", "MID", "SMALL"]
    # net numbers: BIG=1, MID=2, SMALL=3
    fps = [
        # BIG: wide + tall spread (10..90 x, 10..40 y)
        _fp("U1", 10, 10, 1, "BIG"),
        _fp("U2", 90, 40, 1, "BIG"),
        _fp("U3", 90, 10, 1, "BIG"),
        _fp("U4", 10, 40, 1, "BIG"),
        # MID: mid-x column spanning BIG's full height (30..48 x, 10..40 y)
        _fp("U5", 30, 10, 2, "MID"),
        _fp("U6", 48, 40, 2, "MID"),
        _fp("U7", 48, 10, 2, "MID"),
        # SMALL: narrow column spanning BIG's full height (60..66 x, 10..40 y)
        _fp("U8", 60, 10, 3, "SMALL"),
        _fp("U9", 66, 40, 3, "SMALL"),
    ]
    return _write_pcb(tmp_path, "interleaved_3net.kicad_pcb", nets, fps)


def _make_single_per_layer_pcb(tmp_path: Path) -> Path:
    """Each net is sole on its layer (GND on B.Cu, +5V on F.Cu)."""
    nets = ["GND", "+5V"]
    fps = [
        _fp("U1", 20, 25, 1, "GND"),
        _fp("U2", 40, 25, 1, "GND"),
        _fp("U3", 60, 25, 2, "+5V"),
        _fp("U4", 80, 25, 2, "+5V"),
    ]
    return _write_pcb(tmp_path, "single_per_layer.kicad_pcb", nets, fps)


def _make_equal_area_pcb(tmp_path: Path) -> Path:
    """Two nets with IDENTICAL-area (congruent) pad-cluster bboxes on F.Cu.

    ZED and ALPHA each span a 10x10 region -> equal bbox area, so the
    priority tiebreak must fall to alphabetical net name.
    """
    nets = ["ZED", "ALPHA"]
    fps = [
        # ZED: 10..20 x, 10..20 y
        _fp("U1", 10, 10, 1, "ZED"),
        _fp("U2", 20, 20, 1, "ZED"),
        # ALPHA: 70..80 x, 10..20 y (same 10x10 extent, disjoint region)
        _fp("U3", 70, 10, 2, "ALPHA"),
        _fp("U4", 80, 20, 2, "ALPHA"),
    ]
    return _write_pcb(tmp_path, "equal_area.kicad_pcb", nets, fps)


def _make_coincident_pads_pcb(tmp_path: Path) -> Path:
    """Three power nets whose pads all sit at the SAME point -> no partition."""
    nets = ["A", "B", "C"]
    fps = [
        _fp("U1", 50, 25, 1, "A"),
        _fp("U2", 50, 25, 2, "B"),
        _fp("U3", 50, 25, 3, "C"),
    ]
    return _write_pcb(tmp_path, "coincident.kicad_pcb", nets, fps)


# ---------------------------------------------------------------------------
# Unit tests: the allocator directly
# ---------------------------------------------------------------------------


class TestAssignBatchPrioritiesAndOutlines:
    def test_priority_descends_by_ascending_area(self, tmp_path: Path):
        """Smallest bbox area => highest priority number."""
        pcb_path = _make_interleaved_3net_pcb(tmp_path)
        gen = ZoneGenerator.from_pcb(str(pcb_path))
        alloc = assign_batch_zone_priorities_and_outlines(
            gen.pcb,
            gen.board_outline,
            [("BIG", "F.Cu"), ("MID", "F.Cu"), ("SMALL", "F.Cu")],
        )
        p_big = alloc["BIG"][0]
        p_mid = alloc["MID"][0]
        p_small = alloc["SMALL"][0]
        assert p_small > p_mid > p_big, (
            f"expected SMALL>MID>BIG priorities, got SMALL={p_small} MID={p_mid} BIG={p_big}"
        )

    def test_shared_layer_outlines_are_carved_and_disjoint(self, tmp_path: Path):
        """All three same-layer zones get positive-area, pairwise-disjoint outlines."""
        pcb_path = _make_interleaved_3net_pcb(tmp_path)
        gen = ZoneGenerator.from_pcb(str(pcb_path))
        alloc = assign_batch_zone_priorities_and_outlines(
            gen.pcb,
            gen.board_outline,
            [("BIG", "F.Cu"), ("MID", "F.Cu"), ("SMALL", "F.Cu")],
        )
        outlines = {net: alloc[net][1] for net in ("BIG", "MID", "SMALL")}
        for net, poly in outlines.items():
            assert poly is not None, f"{net} got a None (full-board) outline on a shared layer"
            assert _polygon_area(poly) > 0.0, f"{net} carved to zero area"

        names = list(outlines)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                overlap = _polygons_overlap_area(outlines[a], outlines[b])
                assert overlap < 1e-6, f"{a} and {b} still overlap by {overlap:.4f} mm²"

    def test_single_net_per_layer_is_noop(self, tmp_path: Path):
        """Sole-on-layer nets keep full board outline + legacy priority."""
        pcb_path = _make_single_per_layer_pcb(tmp_path)
        gen = ZoneGenerator.from_pcb(str(pcb_path))
        alloc = assign_batch_zone_priorities_and_outlines(
            gen.pcb,
            gen.board_outline,
            [("GND", "B.Cu"), ("+5V", "F.Cu")],
        )
        # GND-named net keeps legacy priority 1, +5V keeps 0.
        assert alloc["GND"] == (1, None)
        assert alloc["+5V"] == (0, None)

    def test_equal_area_ties_break_alphabetically(self, tmp_path: Path):
        """Equal-area siblings: alphabetically-earlier net gets HIGHER priority.

        The sort key is ``(area, name)`` ascending, and rank 0 (first) gets
        the highest priority.  So on an area tie, the alphabetically-first
        name (ALPHA) sorts first and wins the higher priority number.
        """
        pcb_path = _make_equal_area_pcb(tmp_path)
        gen = ZoneGenerator.from_pcb(str(pcb_path))
        alloc = assign_batch_zone_priorities_and_outlines(
            gen.pcb,
            gen.board_outline,
            [("ZED", "F.Cu"), ("ALPHA", "F.Cu")],
        )
        assert alloc["ALPHA"][0] > alloc["ZED"][0]

    def test_equal_area_priority_is_stable_across_input_order(self, tmp_path: Path):
        """Tiebreak is order-independent (deterministic across runs)."""
        pcb_path = _make_equal_area_pcb(tmp_path)
        gen = ZoneGenerator.from_pcb(str(pcb_path))
        a1 = assign_batch_zone_priorities_and_outlines(
            gen.pcb, gen.board_outline, [("ZED", "F.Cu"), ("ALPHA", "F.Cu")]
        )
        gen2 = ZoneGenerator.from_pcb(str(pcb_path))
        a2 = assign_batch_zone_priorities_and_outlines(
            gen2.pcb, gen2.board_outline, [("ALPHA", "F.Cu"), ("ZED", "F.Cu")]
        )
        assert a1["ALPHA"][0] == a2["ALPHA"][0]
        assert a1["ZED"][0] == a2["ZED"][0]

    def test_coincident_pads_raise_partition_error(self, tmp_path: Path):
        """Fully-coincident pad clusters -> ZonePartitionError, not silent zero-copper."""
        pcb_path = _make_coincident_pads_pcb(tmp_path)
        gen = ZoneGenerator.from_pcb(str(pcb_path))
        with pytest.raises(ZonePartitionError):
            assign_batch_zone_priorities_and_outlines(
                gen.pcb,
                gen.board_outline,
                [("A", "F.Cu"), ("B", "F.Cu"), ("C", "F.Cu")],
            )


# ---------------------------------------------------------------------------
# End-to-end tests: the CLI command
# ---------------------------------------------------------------------------


class TestBatchCommandEndToEnd:
    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from kicad_tools.cli.zones_cmd import main

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            ret = main(argv)
        return ret, out.getvalue(), err.getvalue()

    def test_interleaved_zones_written_disjoint_with_nonzero_copper(self, tmp_path: Path):
        pcb_path = _make_interleaved_3net_pcb(tmp_path)
        ret, out, _err = self._run(
            [
                "batch",
                str(pcb_path),
                "--power-nets",
                "BIG:F.Cu,MID:F.Cu,SMALL:F.Cu",
                "-o",
                str(pcb_path),
            ]
        )
        assert ret == 0, out

        pcb = PCB.load(str(pcb_path))
        f_cu = {z.net_name: z.polygon for z in pcb.zones if z.layer == "F.Cu"}
        assert set(f_cu) == {"BIG", "MID", "SMALL"}
        for net, poly in f_cu.items():
            assert _polygon_area(poly) > 0.0, f"{net} written with zero copper area"

        names = list(f_cu)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                overlap = _polygons_overlap_area(f_cu[a], f_cu[b])
                assert overlap < 1e-6, f"{a}/{b} overlap by {overlap:.4f} mm²"

    def test_no_spurious_warning_on_disjoint_batch(self, tmp_path: Path):
        """Carved batch zones do not emit zero-copper warnings among themselves."""
        pcb_path = _make_interleaved_3net_pcb(tmp_path)
        ret, out, err = self._run(
            [
                "batch",
                str(pcb_path),
                "--power-nets",
                "BIG:F.Cu,MID:F.Cu,SMALL:F.Cu",
                "-o",
                str(pcb_path),
            ]
        )
        assert ret == 0
        assert "zero copper" not in err
        assert "overlap warning" not in err
        # Clean summary (no warning-count suffix) when nothing cedes copper.
        assert "with zero-copper overlap warning" not in out

    def test_single_per_layer_is_noop_no_warnings(self, tmp_path: Path):
        pcb_path = _make_single_per_layer_pcb(tmp_path)
        ret, out, err = self._run(
            [
                "batch",
                str(pcb_path),
                "--power-nets",
                "GND:B.Cu,+5V:F.Cu",
                "-o",
                str(pcb_path),
            ]
        )
        assert ret == 0
        assert "overlap warning" not in err
        # Sole-on-layer nets are not carved.
        assert "(carved)" not in out
        # Legacy priorities preserved (GND=1, +5V=0).
        assert "GND on B.Cu (priority 1)" in out
        assert "+5V on F.Cu (priority 0)" in out

    def test_coincident_pads_hard_error(self, tmp_path: Path):
        pcb_path = _make_coincident_pads_pcb(tmp_path)
        ret, out, err = self._run(
            [
                "batch",
                str(pcb_path),
                "--power-nets",
                "A:F.Cu,B:F.Cu,C:F.Cu",
                "-o",
                str(pcb_path),
            ]
        )
        assert ret == 1, out
        assert "ZERO copper" in err or "zero copper" in err.lower()
        # Must NOT have silently reported a successful create.
        assert "Created" not in out
