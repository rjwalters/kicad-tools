"""Issue #6078: the saved zone fill must be as connected as a fresh fill.

``run_fill_zones`` saves KiCad's fill and then applies the #3711
foreign-pad clearance carve.  The carve used to encode every hole by a
hair-thin slit to the nearest exterior point.  On a pour with many,
irregularly placed holes those slits crossed other holes, cutting connected
copper into pieces, and the island filter then dropped every piece without
a same-net anchor.  On board 03 the F.Cu GND pour went from 4230 mm2 in one
region to about 2470 mm2 in 16 pieces: ``kicad-cli pcb drc`` without
``--refill-zones`` reported 17 GND unconnected items instead of 1.  Every
gate refilled first, so none saw it, and Gerber export plots the saved fill.

These tests pin:

* the fracture encoding is lossless and keeps one region per region;
* the carve no longer fragments an irregular hole field;
* the carve never splits anchored copper;
* the pipeline gate and the Gerber exporter both judge the SAVED fill.

All but the last test class are pure Python (shapely only).
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from kicad_tools.drc.geometric import (
    GeometricDRCResult,
    SavedFillCheck,
    check_saved_fill,
    saved_fill_regressions,
)
from kicad_tools.recipes.gate import evaluate_pipeline_gate
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.clearance import _repair_fill_polygon
from kicad_tools.zones.fill_clearance import (
    _fracture_polygon,
    _iter_polygons,
    apply_foreign_pad_clearance,
)

shapely = pytest.importorskip("shapely")
from shapely import make_valid  # noqa: E402
from shapely.geometry import Point, Polygon, box  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _irregular_hole_field(seed: int, n: int = 40):
    """A 30x12 mm pour with ``n`` seeded, non-overlapping round holes."""
    rnd = random.Random(seed)
    holes: list = []
    while len(holes) < n:
        c = Point(rnd.uniform(1.5, 28.5), rnd.uniform(1.5, 10.5)).buffer(0.6)
        if all(c.distance(h) > 0.5 for h in holes):
            holes.append(c)
    return box(0, 0, 30, 12).difference(shapely.unary_union(holes))


class TestFracture:
    @pytest.mark.parametrize("seed", range(6))
    def test_fracture_is_lossless_and_one_region(self, seed):
        poly = _irregular_hole_field(seed)
        assert len(poly.interiors) == 40
        ring = _fracture_polygon(poly)
        rebuilt = _iter_polygons(make_valid(Polygon(ring)))
        assert len(rebuilt) == 1, "fracturing must not split a connected region"
        assert shapely.unary_union(rebuilt).symmetric_difference(poly).area < 1e-9

    def test_fracture_without_holes_is_the_exterior(self):
        ring = _fracture_polygon(box(0, 0, 2, 1))
        assert Polygon(ring).equals(box(0, 0, 2, 1))

    def test_fracture_is_deterministic(self):
        poly = _irregular_hole_field(3)
        assert _fracture_polygon(poly) == _fracture_polygon(poly)

    def test_drc_reader_rebuilds_the_holes(self):
        """``kct check`` reads fills via ``_repair_fill_polygon`` -- it must agree."""
        poly = _irregular_hole_field(1)
        repaired = _repair_fill_polygon(Polygon(_fracture_polygon(poly)))
        assert repaired.symmetric_difference(poly).area < 1e-9


def _board(vias: list[tuple[float, float, int]], outline: list[tuple[float, float]]) -> str:
    via_text = "\n".join(
        f'  (via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {n}))'
        for x, y, n in vias
    )
    pts = " ".join(f"(xy {x} {y})" for x, y in outline)
    return f"""
(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
{via_text}
  (zone
    (net 1)
    (net_name "GND")
    (layer "F.Cu")
    (uuid "gnd-zone")
    (connect_pads (clearance 0.3))
    (min_thickness 0.25)
    (polygon (pts {pts}))
    (filled_polygon (layer "F.Cu") (pts {pts}))
  )
)
"""


def _fill(doc):
    zone = doc.find_all("zone")[0]
    polys = []
    for filled in zone.find_all("filled_polygon"):
        ring = [(xy.get_float(0), xy.get_float(1)) for xy in filled.find("pts").find_all("xy")]
        poly = Polygon(ring)
        polys.append(poly if poly.is_valid else _repair_fill_polygon(poly))
    return shapely.unary_union(polys)


class TestCarveKeepsThePourConnected:
    @pytest.mark.parametrize("seed", range(4))
    def test_irregular_foreign_vias_do_not_fragment_the_pour(self, seed):
        """The board-03 failure class: many irregular foreign antipads."""
        rnd = random.Random(seed)
        vias: list[tuple[float, float, int]] = []
        while len(vias) < 40:
            x, y = rnd.uniform(1.5, 28.5), rnd.uniform(1.5, 10.5)
            if all(Point(x, y).distance(Point(a, b)) > 1.9 for a, b, _ in vias):
                vias.append((round(x, 3), round(y, 3), 2))
        vias.append((0.8, 0.8, 1))  # the pour's one same-net anchor (corner)
        outline = [(0, 0), (30, 0), (30, 12), (0, 12)]
        doc = parse_string(_board(vias, outline))

        assert apply_foreign_pad_clearance(doc) >= 1
        fill = _fill(doc)

        antipads = shapely.unary_union(
            [Point(x, y).buffer(0.3 + 0.3 + 0.125) for x, y, n in vias if n == 2]
        )
        expected = box(0, 0, 30, 12).difference(antipads)
        # Exactly one copper region, and only the antipads were removed (up
        # to buffer-polygon approximation).  The old slit encoding left
        # several pieces and dropped all but the anchored one, losing whole
        # square millimetres of the pour.
        assert len(_iter_polygons(fill)) == len(_iter_polygons(expected)) == 1
        assert fill.symmetric_difference(expected).area < 0.25
        for x, y, n in vias:
            if n == 2:
                assert fill.distance(Point(x, y).buffer(0.3)) >= 0.3 - 1e-6

    # Two 10x10 lobes joined by a 0.2 mm neck (y 5.3..5.5) at x 10..12.
    _DUMBBELL = [
        (0, 0), (10, 0), (10, 5.3), (12, 5.3), (12, 0), (22, 0),
        (22, 10), (12, 10), (12, 5.5), (10, 5.5), (10, 10), (0, 10),
    ]  # fmt: skip

    def test_carve_never_splits_anchored_copper(self):
        """Foreign vias exactly the clearance (0.3 mm) above and below a neck.

        KiCad's fill is legal here, but the carve's conservative antipads
        (radius 0.3 + clearance + min_thickness/2 = 0.725 mm) would sever the
        neck and leave two anchored halves -- the split the fab would
        receive.  The carve must keep KiCad's connected fill instead.
        """
        vias = [(11.0, 6.1, 2), (11.0, 4.7, 2), (5.0, 5.0, 1), (17.0, 5.0, 1)]
        doc = parse_string(_board(vias, self._DUMBBELL))
        before = _fill(doc)
        apply_foreign_pad_clearance(doc)
        after = _fill(doc)
        assert len(_iter_polygons(after)) == 1
        assert after.symmetric_difference(before).area < 1e-9


def _geo(unconnected: int = 0, **types: int) -> GeometricDRCResult:
    from kicad_tools.drc.violation import DRCViolation

    return GeometricDRCResult(
        ran=True,
        all_by_type=dict(types),
        unconnected_items=[DRCViolation.__new__(DRCViolation) for _ in range(unconnected)],
    )


class TestSavedFillComparison:
    def test_board03_numbers_regress(self):
        """The #6078 board-03 measurement: 40 vs 24, +8 islands, +10 slivers."""
        lines = saved_fill_regressions(
            _geo(40, isolated_copper=9, copper_sliver=10), _geo(24, isolated_copper=1)
        )
        assert len(lines) == 3
        assert lines[0].startswith("unconnected_items: 40")

    def test_equal_or_better_saved_fill_is_clean(self):
        assert (
            saved_fill_regressions(_geo(24, isolated_copper=1), _geo(24, isolated_copper=1)) == []
        )
        assert saved_fill_regressions(_geo(1), _geo(2)) == []

    def test_a_run_that_did_not_happen_has_no_verdict(self):
        assert saved_fill_regressions(GeometricDRCResult(ran=False), _geo(3)) == []

    def test_fast_path_skips_the_refill_run(self, monkeypatch, tmp_path):
        calls: list[bool] = []

        def fake(path, *, timeout=120, kicad_cli=None, refill_zones=True):
            calls.append(refill_zones)
            return _geo(0)

        monkeypatch.setattr("kicad_tools.drc.geometric.run_geometric_drc", fake)
        assert check_saved_fill(tmp_path / "b.kicad_pcb").ok
        assert calls == [False]

    def test_saved_findings_are_compared_with_a_refill(self, monkeypatch, tmp_path):
        results = {False: _geo(17), True: _geo(1)}

        def fake(path, *, timeout=120, kicad_cli=None, refill_zones=True):
            return results[refill_zones]

        monkeypatch.setattr("kicad_tools.drc.geometric.run_geometric_drc", fake)
        check = check_saved_fill(tmp_path / "b.kicad_pcb")
        assert check.ran and not check.ok
        assert check.regressions == ["unconnected_items: 17 on the saved fill vs 1 after a refill"]


class TestPipelineGateSavedFillLeg:
    def test_fragmented_saved_fill_fails_drc(self, tmp_path):
        result = evaluate_pipeline_gate(
            tmp_path / "b.kicad_pcb",
            route_ok=True,
            _drc_result=_geo(1),
            _saved_fill_check=SavedFillCheck(ran=True, regressions=["unconnected_items: 17 vs 1"]),
        )
        assert result.saved_fill_ok is False
        assert not result.drc_ok and not result.passed
        assert any("saved zone fill" in r for r in result.reasons)

    def test_clean_saved_fill_passes(self, tmp_path):
        result = evaluate_pipeline_gate(
            tmp_path / "b.kicad_pcb",
            route_ok=True,
            _drc_result=_geo(0),
            _saved_fill_check=SavedFillCheck(ran=True),
        )
        assert result.saved_fill_ok is True and result.passed

    def test_unverified_saved_fill_fails_only_when_drc_is_required(self, tmp_path):
        check = SavedFillCheck(ran=False, note="kicad-cli DRC timed out")
        kwargs = {"route_ok": True, "_drc_result": _geo(0), "_saved_fill_check": check}
        assert not evaluate_pipeline_gate(tmp_path / "b.kicad_pcb", **kwargs).passed
        assert evaluate_pipeline_gate(tmp_path / "b.kicad_pcb", require_drc=False, **kwargs).passed

    def test_the_leg_reuses_the_refilled_run(self, monkeypatch, tmp_path):
        """Production path: one extra DRC (no refill), not two."""
        seen: dict = {}

        def fake_check(path, *, refilled=None, timeout=120, kicad_cli=None):
            seen["refilled"] = refilled
            return SavedFillCheck(ran=True)

        refilled = _geo(0)
        monkeypatch.setattr("kicad_tools.recipes.gate.run_geometric_drc", lambda *a, **k: refilled)
        monkeypatch.setattr("kicad_tools.recipes.gate.check_saved_fill", fake_check)
        result = evaluate_pipeline_gate(tmp_path / "b.kicad_pcb", route_ok=True)
        assert seen["refilled"] is refilled
        assert result.saved_fill_ok is True


class TestGerberExportVerifiesTheSavedFill:
    def _exporter(self, tmp_path, monkeypatch):
        from kicad_tools.export import gerber

        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text("(kicad_pcb (zone (net 1) (filled_polygon (pts (xy 0 0)))))")
        monkeypatch.setattr(gerber, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
        exported: list[Path] = []
        exporter = gerber.GerberExporter(pcb)
        monkeypatch.setattr(
            exporter, "_export_gerbers_impl", lambda config, out, path: exported.append(path)
        )
        return gerber, exporter, exported

    def test_fragmented_saved_fill_is_refused(self, tmp_path, monkeypatch):
        from kicad_tools.exceptions import ExportError

        gerber, exporter, exported = self._exporter(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "kicad_tools.drc.geometric.check_saved_fill",
            lambda *a, **k: SavedFillCheck(ran=True, regressions=["unconnected_items: 17 vs 1"]),
        )
        with pytest.raises(ExportError):
            exporter._export_gerbers(gerber.GerberConfig(), tmp_path / "out")
        assert exported == []

    def test_clean_or_unverifiable_fill_exports(self, tmp_path, monkeypatch):
        gerber, exporter, exported = self._exporter(tmp_path, monkeypatch)
        for check in (SavedFillCheck(ran=True), SavedFillCheck(ran=False, note="absent")):
            monkeypatch.setattr(
                "kicad_tools.drc.geometric.check_saved_fill", lambda *a, _c=check, **k: _c
            )
            exporter._export_gerbers(gerber.GerberConfig(), tmp_path / "out")
        assert len(exported) == 2

    def test_opt_out_skips_the_check(self, tmp_path, monkeypatch):
        gerber, exporter, exported = self._exporter(tmp_path, monkeypatch)

        def boom(*a, **k):
            raise AssertionError("verification must not run when disabled")

        monkeypatch.setattr("kicad_tools.drc.geometric.check_saved_fill", boom)
        exporter._export_gerbers(gerber.GerberConfig(verify_zone_fill=False), tmp_path / "out")
        assert len(exported) == 1


_SHIPPED_ROUTED_BOARDS = sorted(REPO_ROOT.glob("boards/*/output/*_routed.kicad_pcb"))


# One kicad-cli DRC per board (~5-15 s).  Deliberately NOT ci_extended: that
# marker is deselected from the PR "Test" job and runs only on main pushes, so
# a fragmented committed board would merge before this gate saw it.  The PR
# "Test" job runs in the KiCad 10 container, so kicad-cli is available there.
@pytest.mark.pr_required  # Issue #6102: update_ci_extended.py must never list this
@pytest.mark.parametrize(
    "board", _SHIPPED_ROUTED_BOARDS, ids=[p.parent.parent.name for p in _SHIPPED_ROUTED_BOARDS]
)
def test_shipped_routed_board_saved_fill_is_not_fragmented(board):
    """Saved-fill gate over the committed fleet (Issue #6078).

    Gerber export plots the zone fill saved in each committed routed board.
    Every other kicad-cli gate refills first, so this is the check that sees
    the copper the fab would actually receive.
    """
    from kicad_tools.cli.runner import find_kicad_cli

    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not installed")
    check = check_saved_fill(board)
    if not check.ran:
        pytest.skip(f"kicad-cli DRC did not run: {check.note}")
    assert check.regressions == [], (
        f"{board.relative_to(REPO_ROOT)}: the saved zone fill is split where a "
        f"refill is not -- {check.regressions}.  Re-run `kct zones fill` on it."
    )


class TestRouteFillUsesTheShippedRules:
    """``kct route`` must fill under the sidecars it ships (Issue #6078).

    The routed board's ``.kicad_pro``/``.kicad_dru`` used to be emitted only
    by the post-route DRC, after the fill and the oracle loop, so the saved
    fill was computed under different clearances than it ships with.
    """

    def _pcb(self, tmp_path: Path) -> Path:
        pcb = tmp_path / "board_routed.kicad_pcb"
        pcb.write_text(
            '(kicad_pcb (version 20240108) (generator "test")\n'
            '  (net 0 "")\n  (net 1 "GND")\n'
            '  (zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "z")\n'
            "    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))))\n)\n"
        )
        return pcb

    def test_sidecars_are_written_before_the_fill(self, tmp_path, monkeypatch):
        import argparse
        import types

        from kicad_tools.cli import route_cmd, runner

        order: list[str] = []
        pcb = self._pcb(tmp_path)

        def sidecars(output_path, manufacturer, layers, **kwargs):
            order.append(f"sidecars:{manufacturer}:{layers}:{kwargs['source_pcb_path'].name}")

        def fill(path, **kwargs):
            order.append("fill")
            return runner.KiCadCLIResult(success=True, output_path=path, return_code=0)

        monkeypatch.setattr(route_cmd, "_write_drc_constraint_sidecars", sidecars)
        monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("/usr/bin/kicad-cli"))
        monkeypatch.setattr(runner, "run_fill_zones", fill)
        monkeypatch.setattr(
            runner, "validate_net_format", lambda p: runner.NetFormatReport(valid=True)
        )
        args = argparse.Namespace(pcb=str(tmp_path / "src.kicad_pcb"), manufacturer="jlcpcb")
        router = types.SimpleNamespace(
            layer_stack=types.SimpleNamespace(num_layers=4), placement_disposition=None
        )
        route_cmd._fill_zones_after_route(pcb, quiet=True, router=router, args=args)
        assert order == ["sidecars:jlcpcb:4:src.kicad_pcb", "fill"]

    def test_without_route_context_the_fill_still_runs(self, tmp_path, monkeypatch):
        from kicad_tools.cli import route_cmd, runner

        calls: list[str] = []
        monkeypatch.setattr(
            route_cmd, "_write_drc_constraint_sidecars", lambda *a, **k: calls.append("sidecars")
        )
        monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("/usr/bin/kicad-cli"))
        monkeypatch.setattr(
            runner,
            "run_fill_zones",
            lambda p, **k: (
                calls.append("fill")
                or runner.KiCadCLIResult(success=True, output_path=p, return_code=0)
            ),
        )
        monkeypatch.setattr(
            runner, "validate_net_format", lambda p: runner.NetFormatReport(valid=True)
        )
        route_cmd._fill_zones_after_route(self._pcb(tmp_path), quiet=True)
        assert calls == ["fill"]
