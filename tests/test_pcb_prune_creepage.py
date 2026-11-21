"""Issue #9640: ``pcb prune`` takes a voltage map and never undoes creepage spacing."""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path

import pytest

from kicad_tools.pcb_prune import prune_board
from kicad_tools.schema.pcb import PCB

_uid = count(1)

HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "HV")
  (net 2 "LV")
"""


def _pad_fp(ref: str, x: float, y: float, w: float, h: float, net: int, name: str) -> str:
    return (
        f'  (footprint "X:P" (layer "F.Cu") (uuid "f-{ref}") (at {x} {y})\n'
        f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
        f'    (pad "1" smd rect (at 0 0) (size {w} {h}) (layers "F.Cu") (net {net} "{name}"))\n'
        "  )\n"
    )


def _seg(a, b, net=2, w=0.6, extra="") -> str:
    return (
        f"  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) "
        f'(layer "F.Cu") (net {net}){extra} (uuid "s-{next(_uid)}"))\n'
    )


def _stub_board(extra: str = "") -> str:
    """An LV track that ends at the NEAR edge of a long thin pad (x=109.5).

    The pad is 1.2 x 0.3 mm; the 0.6 mm track overhangs it.  Snapping the end
    onto the pad centre (x=110) pushes that wider copper 0.5 mm further along
    the pad, so its round cap ends up closer to the HV pad above (1.525 mm
    against 1.67 mm edge to edge) -- inside a 1.6 mm (150 V, PD2/IIIa)
    requirement.
    """
    return (
        HEADER
        + _pad_fp("A", 100, 100, 0.4, 0.4, 2, "LV")
        + _pad_fp("B", 110, 100, 1.2, 0.3, 2, "LV")
        + _pad_fp("H", 110.5, 98, 0.4, 0.4, 1, "HV")
        + _seg((100, 100), (109.5, 100), extra=extra)
        + ")\n"
    )


@pytest.fixture
def board(tmp_path: Path) -> Path:
    p = tmp_path / "stub.kicad_pcb"
    p.write_text(_stub_board())
    return p


@pytest.fixture
def vmap(tmp_path: Path) -> Path:
    p = tmp_path / "vm.json"
    p.write_text(json.dumps({"HV": 150.0, "LV": 0.0}))
    return p


def _audit(vmap: Path):
    from kicad_tools.creepage.output_audit import CreepageAudit

    return CreepageAudit(voltage_map=vmap)


def _end_x(pcb: PCB) -> float:
    (seg,) = pcb.segments
    return max(seg.start[0], seg.end[0])


def test_baseline_snaps_the_pad_edge_end_onto_the_centre(board):
    """Without a voltage map prune takes the snap (the premise of the issue)."""
    pcb, res = prune_board(board, verify_drc=False)
    assert res.stats["ends_snapped"] == 1
    assert _end_x(pcb) == pytest.approx(110.0)


def test_input_passes_and_snapped_form_fails_creepage(board, vmap, tmp_path):
    audit = _audit(vmap)
    assert audit(board.read_text()) == {}
    pcb, _res = prune_board(board, verify_drc=False)
    snapped = tmp_path / "snapped.kicad_pcb"
    pcb.save(snapped)
    failing = audit(snapped.read_text())
    assert failing and all(k.endswith("|F.Cu") for k in failing)


def test_prune_with_voltage_map_keeps_the_pad_edge_end(board, vmap):
    pcb, res = prune_board(board, verify_drc=False, creepage_audit=_audit(vmap))
    assert _end_x(pcb) == pytest.approx(109.5)
    assert res.stats["ends_snapped"] == 0
    assert [p.net_name for p in res.creepage_rejected] == ["LV"]
    assert "creepage" in (res.creepage_rejected[0].reject_reason or "")
    assert res.creepage["creepage_audit"] == "ran"
    assert res.creepage["creepage_unwaived_failures_after"] == 0
    checked = res.creepage["creepage_pairs_checked"]
    assert {frozenset((r["net_a"], r["net_b"])) for r in checked} == {frozenset(("HV", "LV"))}


def test_prune_without_audit_is_unchanged(board):
    _pcb, res = prune_board(board, verify_drc=False)
    assert res.creepage is None and res.creepage_rejected == []


def test_locked_track_is_not_snapped(tmp_path):
    p = tmp_path / "locked.kicad_pcb"
    p.write_text(_stub_board(extra=" (locked yes)"))
    pcb, res = prune_board(p, verify_drc=False)
    assert res.stats["ends_snapped"] == 0
    assert _end_x(pcb) == pytest.approx(109.5)


def test_unavailable_audit_drops_every_plan(board):
    class Broken:
        def __call__(self, _text):
            return None

        def census(self, _text):
            return None

    pcb, res = prune_board(board, verify_drc=False, creepage_audit=Broken())  # type: ignore[arg-type]
    assert res.accepted == [] and len(res.creepage_rejected) == 1
    assert res.creepage["creepage_audit"] == "unavailable"
    assert _end_x(pcb) == pytest.approx(109.5)


# --- CLI -------------------------------------------------------------------


def _cli(board: Path, *extra: str, capsys) -> tuple[int, dict]:
    from kicad_tools.cli import main

    rc = main(["pcb", "prune", str(board), "--no-drc", "--unverified", "--format", "json", *extra])
    return rc, json.loads(capsys.readouterr().out)


def test_cli_voltage_map_reports_rejected_by_creepage(board, vmap, tmp_path, capsys):
    out = tmp_path / "out.kicad_pcb"
    rc, data = _cli(board, "--voltage-map", str(vmap), "-o", str(out), capsys=capsys)
    assert rc == 0
    assert [n["net"] for n in data["rejected_by_creepage"]] == ["LV"]
    assert data["creepage"]["creepage_audit"] == "ran"
    assert data["snapped"] == 0
    # nothing changed, so nothing was written over the input
    assert not out.exists()


def test_cli_accepted_edit_keeps_the_creepage_report(tmp_path, capsys):
    """A board-changing run still reports the pairs it checked (#9640 review).

    Pass 1 accepts the snap (the HV pad sits far away, so it is
    creepage-safe); pass 2 re-plans the edited board, finds nothing and
    returns the degenerate "unchanged" report.  The JSON must still carry
    the whole-run audit of the board actually written.
    """
    board = tmp_path / "safe.kicad_pcb"
    board.write_text(
        HEADER
        + _pad_fp("A", 100, 100, 0.4, 0.4, 2, "LV")
        + _pad_fp("B", 110, 100, 1.2, 0.3, 2, "LV")
        + _pad_fp("H", 110.5, 80, 0.4, 0.4, 1, "HV")
        + _seg((100, 100), (109.5, 100))
        + ")\n"
    )
    vmap = tmp_path / "vm.json"
    vmap.write_text(json.dumps({"HV": 150.0, "LV": 0.0}))
    out = tmp_path / "out.kicad_pcb"
    rc, data = _cli(board, "--voltage-map", str(vmap), "-o", str(out), capsys=capsys)
    assert rc == 0
    assert data["snapped"] == 1  # the edit was accepted and the board changed
    assert out.exists()
    creep = data["creepage"]
    assert creep["creepage_audit"] == "ran"
    assert {frozenset((r["net_a"], r["net_b"])) for r in creep["creepage_pairs_checked"]} == {
        frozenset(("HV", "LV"))
    }
    assert creep["creepage_unwaived_failures_before"] == 0
    assert creep["creepage_unwaived_failures_after"] == 0


def test_cli_without_voltage_map_still_snaps(board, tmp_path, capsys):
    rc, data = _cli(board, "-o", str(tmp_path / "out.kicad_pcb"), capsys=capsys)
    assert rc == 0
    assert data["snapped"] == 1
    assert "rejected_by_creepage" not in data


def test_cli_missing_voltage_map_is_an_error(board, tmp_path, capsys):
    from kicad_tools.cli import main

    rc = main(["pcb", "prune", str(board), "--voltage-map", str(tmp_path / "nope.json")])
    assert rc == 1
    assert "voltage-map file not found" in capsys.readouterr().err
