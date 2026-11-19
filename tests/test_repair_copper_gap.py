"""Same-net ``physical_copper_gap`` repair (issue #9332).

Fixtures are synthetic 2-layer boards under the jlcpcb profile: one rect pad
(R1/1, net N1) centred at (100, 100) spanning x 99..101, y 99.5..100.5, a
same-net fan-out via 0.10 mm off the pad's top edge, and (for the tangent
class) a same-net track at y = 105.3 with a via 0.05 mm off its edge.  Unit
tests build the findings directly (the descriptors ``kct check`` writes into
``items``); the fix-drc end-to-end tests exercise both the stubbed wiring and
the real ``kct check`` rule set with the fab-view gap bands resolved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli import fix_drc_cmd
from kicad_tools.drc.repair_copper_gap import CopperGapRepairer, copper_gap_violations
from kicad_tools.drc.report import DRCReport
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType
from kicad_tools.sexp import parse_file
from tests.conftest import make_assembly_ready

PAD = "pad-r1-1"
# The descriptors ``validate/copper_refs.py`` writes into a finding's items.
VIA_PAD = "Via@100/100.9:F.Cu-B.Cu:d0.3/s0.6"
VIA_TANGENT = "Via@100/105.75:F.Cu-B.Cu:d0.3/s0.6"
TRACE = "Trace@F.Cu:w0.2:95/105.3~105/105.3"

BOARD = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "N1")
  (net 2 "N2")
  (gr_rect (start 85 85) (end 120 116) (stroke (width 0.1) (type default)) (fill none)
    (layer "Edge.Cuts"))
  (footprint "test:R1" (layer "F.Cu") (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "R" (at 0 1.5 0) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -1.2 -0.8) (end 1.2 0.8) (stroke (width 0.05) (type solid)) (fill no)
      (layer "F.CrtYd"))
    (pad "1" smd rect (at 0 0) (size 2 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "N1") (uuid "pad-r1-1"))
    (model "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0402_1005Metric.step"
      (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))
  )
  (footprint "test:R2" (layer "F.Cu") (at 108 92)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "R" (at 0 1.5 0) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -0.75 -0.75) (end 0.75 0.75) (stroke (width 0.05) (type solid)) (fill no)
      (layer "F.CrtYd"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "N1") (uuid "pad-r2-1"))
    (model "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0402_1005Metric.step"
      (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))
  )
%s
)
"""

# A same-net via 0.10 mm off its own pad (pad edge y=100.5, via edge y=100.6).
# A different-net via 0.10 mm off the same pad (never touched by the repairer).
DIFF_NET_VIA = (
    '  (via (at 101.4 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-n2"))'
)


def _seg(uuid: str, a, b, net: int = 1, width: float = 0.2, layer: str = "F.Cu") -> str:
    return (
        f"  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) "
        f'(layer "{layer}") (net {net}) (uuid "{uuid}"))'
    )


# The net's two pads joined by a 45-degree escape, so only the slit fails
# connectivity-wise on the real-rule boards (the near-miss via dangles off it).
LINK = _seg("trk-link", (100, 100), (108, 92))
PAD_VIA = (
    '  (via (at 100 100.9) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
    '(net 1) (uuid "via-p"))\n' + LINK
)

# A same-net track tangent to its own via: track edge y=105.4, via edge y=105.45.
TANGENT = _seg("trk-t", (95, 105.3), (105, 105.3)) + (
    '  (via (at 100 105.75) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-t"))\n'
    + LINK
)
# Sub-micron variant of TANGENT: the file coordinates sit 0.3 um off whole
# micrometres, so the descriptor (TRACE above) quantises 95.0003 -> 95 and
# 105.0003 -> 105 while the segment itself stays sub-micron -- the endpoint
# match must tolerate the quantisation (judge note on PR #9396).
TANGENT_SUBUM = _seg("trk-t", (95.0003, 105.3), (105.0003, 105.3)) + (
    '  (via (at 100 105.75) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-t"))\n'
    + LINK
)
# A track ending at the fan-out via's centre: it must move with the via.
ATTACHED = _seg("trk-v", (100, 103), (100, 100.9))

# A same-net obround pad (the shape name ``physical_gap`` collects) with the
# same fan-out geometry as PAD_VIA: pad edge y=105.5, via edge y=105.6.
OBROUND_PAD = (
    '  (footprint "test:R3" (layer "F.Cu") (at 110 105)\n'
    '    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS")\n'
    "      (effects (font (size 1 1) (thickness 0.15))))\n"
    '    (property "Value" "R" (at 0 1.5 0) (layer "F.Fab")\n'
    "      (effects (font (size 1 1) (thickness 0.15))))\n"
    '    (pad "1" smd obround (at 0 0) (size 2 1) (layers "F.Cu" "F.Paste" "F.Mask") '
    '(net 1 "N1") (uuid "pad-r3-1"))\n'
    "  )\n"
    '  (via (at 110 105.9) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-o"))'
)
OBROUND = "pad-r3-1"
VIA_OBROUND = "Via@110/105.9:F.Cu-B.Cu:d0.3/s0.6"


def _board(tmp_path: Path, copper: str) -> Path:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(BOARD % copper)
    return path


def _finding(
    items: list[str],
    nets: tuple[str, ...] = ("N1",),
    severity: Severity = Severity.ERROR,
    required: float = 0.15,
    actual: float = 0.1,
) -> DRCViolation:
    return DRCViolation(
        type=ViolationType.PHYSICAL_COPPER_GAP,
        type_str="physical_copper_gap",
        severity=severity,
        message=f"Physical copper gap {actual:.4f}mm < {required:.4f}mm",
        locations=[Location(x_mm=100.0, y_mm=100.55, layer="F.Cu")],
        items=list(items),
        nets=list(nets),
        required_value_mm=required,
        actual_value_mm=actual,
    )


def _via(path: Path, uuid: str) -> tuple[float, float]:
    doc = parse_file(path)
    for node in doc.find_all("via"):
        uuid_node = node.find("uuid")
        if uuid_node and str(uuid_node.get_first_atom()).strip('"') == uuid:
            at = node.find("at")
            return float(at.get_atoms()[0]), float(at.get_atoms()[1])
    raise AssertionError(uuid)


def _end(path: Path, uuid: str, tag: str) -> tuple[float, float]:
    doc = parse_file(path)
    for node in doc.find_all("segment"):
        if str(node.find("uuid").get_first_atom()).strip('"') == uuid:
            atoms = node.find(tag).get_atoms()
            return float(atoms[0]), float(atoms[1])
    raise AssertionError(uuid)


# ---------------------------------------------------------------------------
# Unit: via beside its own pad
# ---------------------------------------------------------------------------


def test_via_beside_own_pad_moves_clear_of_the_pad(tmp_path):
    board = _board(tmp_path, PAD_VIA)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding([PAD, VIA_PAD])], max_displacement=1.0)
    assert result.repaired == 1 and result.opened == 1 and not result.unrepaired
    repairer.save(board)
    x, y = _via(board, "via-p")
    assert x == 100.0
    # The move opens the measured gap to the finding's 0.15 mm edge + margin.
    assert y == pytest.approx(100.96, abs=0.005)


def test_via_beside_own_obround_pad_is_repaired(tmp_path):
    # Regression (#9399): the hand-rolled pad model matched "oblong", a shape
    # name KiCad never writes, so obround pad sides were declined
    # ``side_not_found``.  The rule's own ``clearance._pad_polygon`` model is
    # reused now, so the pad resolves and the via is pushed clear.
    board = _board(tmp_path, OBROUND_PAD)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding([OBROUND, VIA_OBROUND])], max_displacement=1.0)
    assert result.repaired == 1 and result.opened == 1 and not result.unrepaired
    repairer.save(board)
    x, y = _via(board, "via-o")
    assert x == 110.0
    assert y == pytest.approx(105.96, abs=0.005)


def test_via_snaps_to_pad_centre_when_a_process_is_declared(tmp_path):
    from kicad_tools.manufacturers.base import DesignRules

    rules = DesignRules(
        min_clearance_mm=0.127,
        min_trace_width_mm=0.127,
        min_via_drill_mm=0.3,
        min_via_diameter_mm=0.6,
        min_annular_ring_mm=0.15,
        via_in_pad_supported=True,
        via_in_pad_process_id="pofv",
    )
    board = _board(tmp_path, PAD_VIA)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps(
        [_finding([PAD, VIA_PAD])], max_displacement=1.0, design_rules=rules
    )
    assert result.repaired == 1 and result.snapped_to_pad == 1
    repairer.save(board)
    assert _via(board, "via-p") == (100.0, 100.0)


def test_attached_track_moves_with_the_via_and_undoes(tmp_path):
    board = _board(tmp_path, PAD_VIA + "\n" + ATTACHED)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding([PAD, VIA_PAD])], max_displacement=1.0)
    assert result.repaired == 1
    assert result.actions[0].joined == [("trk-v", "end")]
    repairer.save(board)
    assert _end(board, "trk-v", "end")[1] == pytest.approx(100.96, abs=0.005)
    undo = CopperGapRepairer(board)
    assert undo.undo_move(result.actions[0])
    undo.save(board)
    assert _via(board, "via-p") == (100.0, 100.9)
    assert _end(board, "trk-v", "end") == (100.0, 100.9)


def test_undo_restores_an_unattached_via(tmp_path):
    board = _board(tmp_path, PAD_VIA)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding([PAD, VIA_PAD])], max_displacement=1.0)
    assert repairer.undo_move(result.actions[0])
    repairer.save(board)
    assert _via(board, "via-p") == (100.0, 100.9)


# ---------------------------------------------------------------------------
# Unit: track tangent to its own via
# ---------------------------------------------------------------------------


def test_tangent_track_snaps_the_via_onto_the_centreline(tmp_path):
    board = _board(tmp_path, TANGENT)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding([TRACE, VIA_TANGENT], required=0.127, actual=0.05)])
    assert result.repaired == 1 and result.snapped_to_track == 1 and not result.unrepaired
    repairer.save(board)
    assert _via(board, "via-t") == (100.0, 105.3)
    # The track itself never moves.
    assert _end(board, "trk-t", "start") == (95.0, 105.3)
    assert _end(board, "trk-t", "end") == (105.0, 105.3)


def test_tangent_track_with_submicron_endpoints_still_snaps(tmp_path):
    # Regression (judge note on PR #9396): exact endpoint equality against the
    # 1 um-quantised descriptor silently declined sub-micron geometry as
    # ``track_not_found`` -- 35% of candidate segments on board 07.
    board = _board(tmp_path, TANGENT_SUBUM)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding([TRACE, VIA_TANGENT], required=0.127, actual=0.05)])
    assert result.repaired == 1 and result.snapped_to_track == 1 and not result.unrepaired
    repairer.save(board)
    assert _via(board, "via-t") == (100.0, 105.3)
    # The track itself never moves (sub-micron coordinates included).
    assert _end(board, "trk-t", "start") == (95.0003, 105.3)
    assert _end(board, "trk-t", "end") == (105.0003, 105.3)


def test_tangent_track_descriptor_sort_flip_still_resolves(tmp_path):
    # Endpoints whose raw and 1 um-quantised sorts disagree (both x quantise
    # to 100 while the raw x order is opposite to the y order): the descriptor
    # sorts (100/100) first, the raw coordinates sort (100.0, 105.3) first,
    # so the match must try both orderings.
    copper = _seg("trk-f", (100.0004, 100), (100.0, 105.3)) + (
        '  (via (at 100.45 102.65) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        '(net 1) (uuid "via-f"))'
    )
    board = _board(tmp_path, copper)
    result = CopperGapRepairer(board).repair_gaps(
        [
            _finding(
                ["Trace@F.Cu:w0.2:100/100~100/105.3", "Via@100.45/102.65:F.Cu-B.Cu:d0.3/s0.6"],
                required=0.127,
                actual=0.05,
            )
        ]
    )
    assert result.repaired == 1 and result.snapped_to_track == 1 and not result.unrepaired


def test_dry_run_predicts_without_modifying(tmp_path):
    board = _board(tmp_path, PAD_VIA + "\n" + TANGENT)
    before = board.read_text()
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps(
        [_finding([PAD, VIA_PAD]), _finding([TRACE, VIA_TANGENT], required=0.127, actual=0.05)],
        max_displacement=1.0,
        dry_run=True,
    )
    assert result.repaired == 2 and len(result.actions) == 2
    assert not repairer.modified
    assert board.read_text() == before


def test_second_finding_on_a_moved_via_is_skipped(tmp_path):
    board = _board(tmp_path, PAD_VIA)
    finding = _finding([PAD, VIA_PAD])
    result = CopperGapRepairer(board).repair_gaps([finding, finding], max_displacement=1.0)
    assert result.repaired == 1
    # The applied move rewrote the via's position, so the duplicate finding's
    # descriptor no longer resolves.
    assert result.skipped("via_not_found") == 1
    # In a dry run the document is untouched, so the same via resolves twice.
    dry = CopperGapRepairer(board).repair_gaps(
        [finding, finding], max_displacement=1.0, dry_run=True
    )
    assert dry.repaired == 1
    assert dry.skipped("via_already_moved") == 1


# ---------------------------------------------------------------------------
# Unit: classes that are never touched
# ---------------------------------------------------------------------------


def test_different_net_finding_is_never_selected(tmp_path):
    board = _board(tmp_path, DIFF_NET_VIA)
    finding = _finding([PAD, "Via@101.4/100:F.Cu-B.Cu:d0.3/s0.6"], nets=("N1", "N2"))
    assert copper_gap_violations([finding]) == []
    result = CopperGapRepairer(board).repair_gaps([finding], max_displacement=1.0)
    assert result.total_violations == 0 and result.repaired == 0


def test_warning_finding_is_never_selected(tmp_path):
    finding = _finding([PAD, VIA_PAD], severity=Severity.WARNING)
    assert copper_gap_violations([finding]) == []


@pytest.mark.parametrize(
    "items, reason",
    [
        # zone-fill side (a raw uuid, not a pad of this board)
        (["zone-abcdef", VIA_PAD], "side_not_found"),
        # via-via pair
        ([VIA_PAD, VIA_TANGENT], "unsupported_pair"),
        # track-track pair (no via to move)
        ([TRACE, "Trace@F.Cu:w0.2:95/100.3~105/100.3"], "unsupported_pair"),
        # the via named by the finding is not on the board
        ([PAD, "Via@150/150:F.Cu-B.Cu:d0.3/s0.6"], "via_not_found"),
        # the track named by the finding is not on the board
        (["Trace@F.Cu:w0.2:90/105.3~91/105.3", VIA_TANGENT], "track_not_found"),
    ],
)
def test_unsupported_or_unresolvable_pairs_are_skipped(tmp_path, items, reason):
    board = _board(tmp_path, PAD_VIA + "\n" + TANGENT)
    before = board.read_text()
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding(items, required=0.127)], max_displacement=1.0)
    assert result.repaired == 0
    assert result.skipped(reason) == 1, result.unrepaired
    assert not repairer.modified
    assert board.read_text() == before


def test_via_on_a_different_net_than_the_pad_is_declined(tmp_path):
    board = _board(tmp_path, DIFF_NET_VIA)
    # The finding claims same-net, but the via's own net disagrees.
    finding = _finding([PAD, "Via@101.4/100:F.Cu-B.Cu:d0.3/s0.6"])
    result = CopperGapRepairer(board).repair_gaps([finding], max_displacement=1.0)
    assert result.repaired == 0
    assert result.skipped("not_same_net") == 1


def test_locked_via_is_declined(tmp_path):
    locked = PAD_VIA.replace("(at 100 100.9)", "(at 100 100.9) (locked yes)")
    board = _board(tmp_path, locked)
    result = CopperGapRepairer(board).repair_gaps([_finding([PAD, VIA_PAD])], max_displacement=1.0)
    assert result.repaired == 0
    assert result.skipped("locked_via") == 1


def test_move_longer_than_max_displacement_is_declined(tmp_path):
    board = _board(tmp_path, TANGENT)
    result = CopperGapRepairer(board).repair_gaps(
        [_finding([TRACE, VIA_TANGENT], required=0.127, actual=0.05)], max_displacement=0.1
    )
    assert result.repaired == 0
    assert result.skipped("exceeds_max_displacement") == 1


def test_move_that_crowds_foreign_copper_is_declined(tmp_path):
    from kicad_tools.manufacturers import get_profile

    rules = get_profile("jlcpcb").get_design_rules(2)
    # The via snaps onto its track at (100, 105.3); its 0.3 mm disc would
    # then sit 0.10 mm from this N2 track's edge (min clearance 0.127).
    copper = (
        _seg("trk-short", (95, 105.3), (100.1, 105.3))
        + "\n"
        + _seg("trk-n2", (100.5, 105.3), (102, 105.3), net=2)
        + "\n"
        + '  (via (at 100 105.75) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        '(net 1) (uuid "via-t"))'
    )
    board = _board(tmp_path, copper)
    short_trace = "Trace@F.Cu:w0.2:95/105.3~100.1/105.3"
    result = CopperGapRepairer(board).repair_gaps(
        [_finding([short_trace, VIA_TANGENT], required=0.127, actual=0.05)],
        max_displacement=1.0,
        design_rules=rules,
    )
    assert result.repaired == 0
    assert result.skipped("would_violate_clearance") == 1
    assert _via(board, "via-t") == (100.0, 105.75)


# ---------------------------------------------------------------------------
# fix-drc integration: --only copper-gap
# ---------------------------------------------------------------------------


def _report(*violations: DRCViolation) -> DRCReport:
    return DRCReport(
        source_file="b.kicad_pcb",
        created_at=None,
        pcb_name="b.kicad_pcb",
        violations=list(violations),
    )


def _run(monkeypatch, board: Path, reports: list[DRCReport], extra=()):
    """Run fix-drc --only copper-gap; ``reports`` feed successive kct-check runs."""
    feed = iter(reports)
    last = [reports[-1]]

    def fake_python_drc(*args, **kwargs):
        last[0] = next(feed, last[0])
        return last[0]

    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", fake_python_drc)
    return fix_drc_cmd.main(
        [
            str(board),
            "--only",
            "copper-gap",
            "--no-connectivity-check",
            "--max-displacement",
            "1.0",
            *extra,
        ]
    )


def test_fix_drc_only_copper_gap_repairs_and_verifies(tmp_path, monkeypatch, capsys):
    board = _board(tmp_path, PAD_VIA)
    before = _report(_finding([PAD, VIA_PAD]))
    clean = _report()
    # calls: detection, final re-check
    code = _run(monkeypatch, board, [before, clean])
    assert code == 0, capsys.readouterr().out
    assert _via(board, "via-p")[1] == pytest.approx(100.96, abs=0.005)
    assert "COPPER GAP (same-net): 1/1" in capsys.readouterr().out


def test_fix_drc_only_copper_gap_reports_unrepaired_in_json(tmp_path, monkeypatch, capsys):
    import json

    board = _board(tmp_path, DIFF_NET_VIA)
    finding = _finding([PAD, "Via@101.4/100:F.Cu-B.Cu:d0.3/s0.6"], nets=("N1", "N2"))
    _run(monkeypatch, board, [_report(finding)], extra=("--dry-run", "--format", "json"))
    data = json.loads(capsys.readouterr().out)
    # A different-net finding is not even targeted.
    assert data["copper_gap"]["violations"] == 0
    assert "copper_gap" in data


def test_fix_drc_dry_run_leaves_the_board_alone(tmp_path, monkeypatch):
    board = _board(tmp_path, PAD_VIA)
    original = board.read_text()
    _run(monkeypatch, board, [_report(_finding([PAD, VIA_PAD]))], extra=("--dry-run",))
    assert board.read_text() == original


def test_fix_drc_reports_a_copper_gap_repair_in_json(tmp_path, monkeypatch, capsys):
    import json

    board = _board(tmp_path, PAD_VIA)
    before = _report(_finding([PAD, VIA_PAD]))
    clean = _report()
    _run(monkeypatch, board, [before, clean], extra=("--format", "json"))
    data = json.loads(capsys.readouterr().out)
    assert data["copper_gap"]["violations"] == 1
    assert data["copper_gap"]["repaired"] == 1
    assert data["copper_gap"]["opened"] == 1
    assert data["copper_gap"]["actions"][0]["via_uuid"] == "via-p"


# ---------------------------------------------------------------------------
# Real rule set: the finding exists before and is gone after
# ---------------------------------------------------------------------------


def _same_net_gap_errors(report) -> list:
    return [
        v
        for v in report.violations
        if v.type_str == "physical_copper_gap" and v.is_error and len(set(v.nets)) == 1
    ]


def _diff_net_gap_errors(report) -> list:
    return [
        v
        for v in report.violations
        if v.type_str == "physical_copper_gap" and v.is_error and len(set(v.nets)) > 1
    ]


def _real_report(board: Path):
    report = fix_drc_cmd._run_python_drc(
        board, manufacturer="jlcpcb", layers=2, project_pcb_path=board, copper_gap=True
    )
    assert report is not None
    return report


def test_real_rule_via_beside_own_pad_is_found_then_repaired(tmp_path, capsys):
    board = _board(tmp_path, PAD_VIA)
    make_assembly_ready(board)

    before = _real_report(board)
    (finding,) = _same_net_gap_errors(before)
    assert PAD in finding.items
    assert any(str(i).startswith("Via@") for i in finding.items)
    assert finding.required_value_mm == pytest.approx(0.15)

    fix_drc_cmd.main([str(board), "--only", "copper-gap", "--no-connectivity-check"])
    out = capsys.readouterr().out
    # The re-check re-measures with the fab-view gap bands: nothing targeted
    # is left (the residual info/warning advisories keep the exit code at 2,
    # the same "violations remain" contract as any other fix-drc category).
    assert "kct-check: 0 targeted error(s)" in out
    assert "COPPER GAP (same-net): 1/1" in out

    after = _real_report(board)
    assert _same_net_gap_errors(after) == []
    x, y = _via(board, "via-p")
    assert x == 100.0
    assert y == pytest.approx(100.96, abs=0.005)


def test_real_rule_tangent_track_slit_is_merged(tmp_path, capsys):
    board = _board(tmp_path, TANGENT)
    make_assembly_ready(board)

    before = _real_report(board)
    (finding,) = _same_net_gap_errors(before)
    assert TRACE in finding.items
    assert finding.required_value_mm == pytest.approx(0.127)

    fix_drc_cmd.main([str(board), "--only", "copper-gap", "--no-connectivity-check"])
    out = capsys.readouterr().out
    assert "kct-check: 0 targeted error(s)" in out

    after = _real_report(board)
    assert _same_net_gap_errors(after) == []
    assert _via(board, "via-t") == (100.0, 105.3)


def test_real_rule_different_net_gap_is_left_unchanged(tmp_path):
    board = _board(tmp_path, PAD_VIA + "\n" + DIFF_NET_VIA)
    make_assembly_ready(board)

    before = _real_report(board)
    assert len(_same_net_gap_errors(before)) == 1
    assert len(_diff_net_gap_errors(before)) == 1

    fix_drc_cmd.main([str(board), "--only", "copper-gap", "--no-connectivity-check"])

    after = _real_report(board)
    assert _same_net_gap_errors(after) == []
    # The different-net via did not move and its finding remains.
    assert _via(board, "via-n2") == (101.4, 100.0)
    assert len(_diff_net_gap_errors(after)) == 1
