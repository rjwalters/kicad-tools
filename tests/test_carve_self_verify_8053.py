"""Issue #8053: the #3711 foreign-pad carve verifies itself against KiCad's fill.

``run_fill_zones`` saves KiCad's fill and then subtracts foreign-net antipads
from it (#3711).  The carve uses the zone's ``connect_pads`` clearance plus
half the minimum thickness, which is more than KiCad needs whenever the
board's rules let the pour sit closer (chorus v25: a ``.kicad_dru`` clearance
of 0.1016 mm against a 0.3 mm zone clearance).  There the carve fixes nothing
and can still cut copper KiCad keeps.  On chorus v25 it left 5
``copper_sliver`` findings on a fill KiCad refills clean, so ``kct export``
refused the board (#6078).

The synthetic board below has the same shape of problem: a GND pour whose
right lobe hangs off a neck that a foreign via sits in.  KiCad (DRU
clearance 0.1016 mm) keeps the neck; the carve (0.3 + 0.125 mm) severs it
and drops the unanchored lobe.  kicad-cli is faked: its DRC reports a
regression exactly when the saved fill differs from KiCad's.

Pinned behaviour:

* carve splits the pour, KiCad's fill meets clearance -> carve rolled back;
* KiCad's fill violates foreign clearance on another zone -> carve kept only
  there;
* the carve is needed on the zone it splits -> the conflict is reported,
  naming the zone and the foreign item, and ``kct zones fill`` exits 1;
* the carved fill is not worse than KiCad's -> carve kept as is.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

shapely = pytest.importorskip("shapely")
from shapely import make_valid  # noqa: E402
from shapely.geometry import Point, Polygon, box  # noqa: E402

from kicad_tools.cli import runner, zones_cmd  # noqa: E402
from kicad_tools.drc.geometric import SavedFillCheck  # noqa: E402
from kicad_tools.sexp import parse_file  # noqa: E402
from kicad_tools.zones.fill_clearance import _fracture_polygon  # noqa: E402

SPLIT_ZONE = "00000000-0000-4000-8000-0000000000a1"
OTHER_ZONE = "00000000-0000-4000-8000-0000000000b1"
NECK_VIA = "00000000-0000-4000-8000-0000000000c1"
INTRUDER_VIA = "00000000-0000-4000-8000-0000000000c2"
NECK_VIA_DESC = "Via [SIG] on F.Cu - B.Cu"
INTRUDER_DESC = "Via [SIG2] on F.Cu - B.Cu"

#: KiCad's fill of the split zone (F.Cu): a left lobe holding the GND pad and
#: a right lobe joined to it by a 1.4 mm neck.  The foreign via at (10, 5)
#: (r 0.3) gets KiCad's 0.1016 mm DRU clearance, leaving two 0.3 mm necks.
_SPLIT_FILL = make_valid(
    box(1, 1, 9, 9).union(box(9, 4.3, 11, 5.7)).union(box(11, 1, 19, 9))
).difference(Point(10, 5).buffer(0.4016))

#: KiCad's fill of the second zone (B.Cu), which really is too close to a
#: foreign via at (30, 5): no hole around it at all.
_OTHER_FILL = box(21, 1, 39, 9)


def _filled(poly, layer: str) -> str:
    ring = _fracture_polygon(poly)
    pts = " ".join(f"(xy {x:.6f} {y:.6f})" for x, y in ring)
    return f'\t\t(filled_polygon (layer "{layer}") (pts {pts}))'


def _board(split_fill=_SPLIT_FILL, other_fill=_OTHER_FILL) -> str:
    return f"""(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")
\t(general (thickness 1.6))
\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "SIG")
\t(net 3 "SIG2")
\t(footprint "T:P" (layer "F.Cu") (uuid "00000000-0000-4000-8000-000000000011") (at 3 5)
\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND") (uuid "00000000-0000-4000-8000-000000000031"))
\t)
\t(footprint "T:P" (layer "B.Cu") (uuid "00000000-0000-4000-8000-000000000012") (at 23 5)
\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "B.Cu") (net 1 "GND") (uuid "00000000-0000-4000-8000-000000000032"))
\t)
\t(via (at 10 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "{NECK_VIA}"))
\t(via (at 30 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 3) (uuid "{INTRUDER_VIA}"))
\t(zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "{SPLIT_ZONE}") (hatch edge 0.5)
\t\t(connect_pads (clearance 0.3))
\t\t(min_thickness 0.25)
\t\t(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
\t\t(polygon (pts (xy 1 1) (xy 19 1) (xy 19 9) (xy 1 9)))
{_filled(split_fill, "F.Cu")}
\t)
\t(zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "{OTHER_ZONE}") (hatch edge 0.5)
\t\t(connect_pads (clearance 0.3))
\t\t(min_thickness 0.25)
\t\t(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
\t\t(polygon (pts (xy 21 1) (xy 39 1) (xy 39 9) (xy 21 9)))
{_filled(other_fill, "B.Cu")}
\t)
)
"""


def _zone_area(path: Path, uuid: str) -> float:
    for zone in parse_file(path).find_all("zone"):
        if zone.find("uuid").get_string(0) != uuid:
            continue
        polys = []
        for f in zone.find_all("filled_polygon"):
            pts = [(xy.get_float(0), xy.get_float(1)) for xy in f.find("pts").find_all("xy")]
            polys.append(make_valid(Polygon(pts)))
        return shapely.unary_union(polys).area if polys else 0.0
    raise AssertionError(uuid)


def _violation(kind: str, *items: tuple[str, str]) -> dict:
    return {
        "type": kind,
        "severity": "error" if kind != "copper_sliver" else "warning",
        "description": kind,
        "items": [{"uuid": u, "description": d} for u, d in items],
    }


class FakeKiCad:
    """A kicad-cli whose fill is :func:`_board` and whose DRC judges geometry.

    * A refill writes KiCad's fill back and reports ``clearance_on`` (the
      zones whose KiCad fill is too close to a foreign via).
    * A read-only DRC reports one ``copper_sliver`` per zone whose saved fill
      lost copper relative to KiCad's -- the carve's fragmentation.  A zone
      named in ``clearance_on`` whose saved fill still equals KiCad's keeps
      its clearance finding.
    """

    def __init__(self, clearance_on: dict[str, tuple[str, str]] | None = None):
        self.clearance_on = clearance_on or {}
        self.kicad_area: dict[str, float] = {}
        self.calls: list[str] = []

    def _clearance(self, zone: str) -> dict:
        via_uuid, via_desc = self.clearance_on[zone]
        return _violation("clearance", (zone, "Zone [GND]"), (via_uuid, via_desc))

    def report(self, pcb_path: Path, kicad_cli: Path, *, refill: bool) -> dict:
        self.calls.append("refill" if refill else "check")
        if refill:
            pcb_path.write_text(_board())
            return {
                "violations": [self._clearance(z) for z in self.clearance_on],
                "unconnected_items": [],
            }
        violations = []
        for zone in (SPLIT_ZONE, OTHER_ZONE):
            area = _zone_area(pcb_path, zone)
            if area < self.kicad_area[zone] - 1e-6:
                if zone == SPLIT_ZONE:
                    violations.append(_violation("copper_sliver"))
            elif zone in self.clearance_on:
                violations.append(self._clearance(zone))
        return {"violations": violations, "unconnected_items": []}


@pytest.fixture
def board(tmp_path) -> Path:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_board())
    return pcb


def _carve_and_fake(board: Path, monkeypatch, fake: FakeKiCad) -> None:
    fake.kicad_area = {z: _zone_area(board, z) for z in (SPLIT_ZONE, OTHER_ZONE)}
    monkeypatch.setattr(runner, "_kicad_drc_report", fake.report)
    assert runner._apply_foreign_pad_clearance(board) > 0


def test_carve_fragments_a_pour_kicad_keeps_whole(board):
    """The fixture is a real #8053 case: the carve drops the right lobe."""
    before = _zone_area(board, SPLIT_ZONE)
    assert runner._apply_foreign_pad_clearance(board) > 0
    after = _zone_area(board, SPLIT_ZONE)
    # The 8 x 8 mm right lobe (64 mm^2) is gone, not just an antipad.
    assert before - after > 60.0


def test_carve_rolled_back_when_kicad_fill_meets_clearance(board, monkeypatch):
    fake = FakeKiCad()
    _carve_and_fake(board, monkeypatch, fake)

    notes, conflicts = runner._verify_foreign_pad_carve(board, Path("/fake/kicad-cli"))

    assert conflicts == []
    assert len(notes) == 1 and "rolled back" in notes[0]
    assert "copper_sliver: 1 with the carve vs 0 on KiCad's fill" in notes[0]
    # The saved fill is KiCad's own again -- what a refill (and export) sees.
    assert _zone_area(board, SPLIT_ZONE) == pytest.approx(fake.kicad_area[SPLIT_ZONE])
    assert _zone_area(board, OTHER_ZONE) == pytest.approx(fake.kicad_area[OTHER_ZONE])
    assert fake.calls == ["check", "refill"]


def test_carve_kept_only_where_kicad_fill_violates_clearance(board, monkeypatch):
    fake = FakeKiCad(clearance_on={OTHER_ZONE: (INTRUDER_VIA, INTRUDER_DESC)})
    _carve_and_fake(board, monkeypatch, fake)

    notes, conflicts = runner._verify_foreign_pad_carve(board, Path("/fake/kicad-cli"))

    assert conflicts == []
    assert len(notes) == 1 and "kept only on zone GND on B.Cu" in notes[0]
    # Split zone: KiCad's fill.  Violating zone: carved (antipad around the via).
    assert _zone_area(board, SPLIT_ZONE) == pytest.approx(fake.kicad_area[SPLIT_ZONE])
    assert _zone_area(board, OTHER_ZONE) < fake.kicad_area[OTHER_ZONE] - 1.0
    assert fake.calls == ["check", "refill", "check"]


def test_needed_carve_that_splits_the_pour_is_a_named_conflict(board, monkeypatch):
    fake = FakeKiCad(clearance_on={SPLIT_ZONE: (NECK_VIA, NECK_VIA_DESC)})
    _carve_and_fake(board, monkeypatch, fake)

    notes, conflicts = runner._verify_foreign_pad_carve(board, Path("/fake/kicad-cli"))

    assert notes == []
    assert len(conflicts) == 1
    assert "zone GND on F.Cu" in conflicts[0]
    assert NECK_VIA_DESC in conflicts[0]
    # The clearance-correct (carved) fill is what is left on disk.
    assert _zone_area(board, SPLIT_ZONE) < fake.kicad_area[SPLIT_ZONE] - 60.0


def test_carve_kept_when_it_is_no_worse_than_kicad(board, monkeypatch):
    fake = FakeKiCad()
    _carve_and_fake(board, monkeypatch, fake)
    carved = board.read_text()
    # KiCad's own fill has the same finding: the carve is not to blame.
    original = fake.report

    def report(pcb_path, kicad_cli, *, refill):
        out = original(pcb_path, kicad_cli, refill=refill)
        if refill:
            out["violations"].append(_violation("copper_sliver"))
        return out

    monkeypatch.setattr(runner, "_kicad_drc_report", report)
    assert runner._verify_foreign_pad_carve(board, Path("/fake/kicad-cli")) == ([], [])
    assert board.read_text() == carved


def test_clean_carve_costs_one_read_only_drc(board, monkeypatch):
    fake = FakeKiCad()
    _carve_and_fake(board, monkeypatch, fake)
    carved = board.read_text()
    clean = {"violations": [], "unconnected_items": []}
    assert runner._verify_foreign_pad_carve(board, Path("/k"), clean) == ([], [])
    assert fake.calls == []  # the remediation's report was reused
    assert board.read_text() == carved


def test_run_fill_zones_rolls_back_a_fragmenting_carve(board, monkeypatch):
    """End to end through ``run_fill_zones`` (fill, remediation, carve, verify)."""
    fake = FakeKiCad()
    fake.kicad_area = {z: _zone_area(board, z) for z in (SPLIT_ZONE, OTHER_ZONE)}
    monkeypatch.setattr(runner, "_kicad_drc_report", fake.report)
    monkeypatch.setattr(runner, "_kicad_cli_has_fill_zones", lambda cli: False)
    monkeypatch.setattr(runner, "_kicad_drc_supports_refill", lambda cli: True)

    def fill(pcb_path, output_path, kicad_cli, **kw):
        pcb_path.write_text(_board())
        return runner.KiCadCLIResult(success=True, output_path=pcb_path)

    monkeypatch.setattr(runner, "_run_fill_zones_via_drc", fill)

    result = runner.run_fill_zones(board, kicad_cli=Path("/fake/kicad-cli"))

    assert result.success
    assert result.fill_conflicts == []
    assert any("rolled back" in n for n in result.fill_notes)
    assert _zone_area(board, SPLIT_ZONE) == pytest.approx(fake.kicad_area[SPLIT_ZONE])


class TestZonesFillReportsTheConflict:
    def _run(self, tmp_path, monkeypatch, result, check, capsys):
        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
        monkeypatch.setattr(runner, "run_fill_zones", lambda *a, **k: result)
        monkeypatch.setattr("kicad_tools.drc.geometric.check_saved_fill", lambda *a, **k: check)
        args = argparse.Namespace(
            pcb=str(pcb), output=None, net=None, dry_run=False, quiet=False, format="text"
        )
        code = zones_cmd._run_fill(args)
        return code, capsys.readouterr()

    def test_conflict_exits_1_naming_zones_and_pads(self, tmp_path, monkeypatch, capsys):
        line = f"zone GND on F.Cu: KiCad's fill violates clearance to {NECK_VIA_DESC}"
        result = runner.KiCadCLIResult(success=True, fill_conflicts=[line])
        check = SavedFillCheck(ran=True, regressions=["copper_sliver: 1 vs 0"])
        code, out = self._run(tmp_path, monkeypatch, result, check, capsys)
        assert code == 1
        assert "copper_sliver: 1 vs 0" in out.err
        assert line in out.err
        assert "--refill-zones --save-board" in out.err

    def test_rollback_note_is_printed_and_fill_succeeds(self, tmp_path, monkeypatch, capsys):
        note = "Foreign-pad clearance carve (#3711) rolled back: ..."
        result = runner.KiCadCLIResult(success=True, fill_notes=[note])
        code, out = self._run(tmp_path, monkeypatch, result, SavedFillCheck(ran=True), capsys)
        assert code == 0
        assert note in out.out
