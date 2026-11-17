"""mask_multi_segment: one opening that is several disjoint pieces (Issue #8847).

Standard pad shapes cannot yield a multi-piece opening, so the positive case
drives the rule with a synthetic snapshot; the negative case uses a real board.
"""

from shapely.geometry import MultiPolygon, box

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker
from kicad_tools.validate.mask_geometry import MaskOpening
from kicad_tools.validate.rules.mask_expansion import check_mask_openings
from tests._mask_expansion_fixture import write_board


def _opening(geom):
    return MaskOpening(
        "00000000-0000-0000-0000-000000000001",
        "F.Mask",
        "pad",
        0.0,
        "board",
        False,
        geom,
        box(0, 0, 1, 1),
    )


class _Snap:
    def __init__(self, openings):
        self.openings = openings


def test_disjoint_opening_flagged():
    geom = MultiPolygon([box(0, 0, 1, 1), box(3, 0, 4, 1)])
    res = check_mask_openings(_Snap([_opening(geom)]), None, "multi")
    assert [v.rule_id for v in res.violations] == ["mask_multi_segment"]
    assert res.violations[0].actual_value == 2.0
    assert res.violations[0].severity == "warning"


def test_single_polygon_not_flagged():
    res = check_mask_openings(_Snap([_opening(box(0, 0, 1, 1))]), None, "multi")
    assert not res.violations


def test_real_board_clean(tmp_path):
    path = write_board(tmp_path / "b.kicad_pcb", [("rect", "1 2", ""), ("oval", "1 2", "")])
    checker = DRCChecker(PCB.load(str(path)), manufacturer="jlcpcb", layers=2)
    assert not checker.check_mask_multi_segment().violations


def test_incomplete_snapshot_reports_unknown(tmp_path):
    from kicad_tools.validate.coverage import UNKNOWN, precheck

    path = write_board(tmp_path / "b.kicad_pcb", [("rect", "1 2", "")])
    path.with_suffix(".kicad_dru").write_text("(version 1)")
    checker = DRCChecker(PCB.load(str(path)), manufacturer="jlcpcb", layers=2)
    cov = precheck("mask_multi_segment", checker, pcb_path=path)
    assert cov is not None and cov.state == UNKNOWN
