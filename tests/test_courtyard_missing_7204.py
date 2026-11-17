"""A footprint without a courtyard is a finding, not a silent pass (Issue #7204).

Before #7204 the courtyard-overlap rule only tried footprints that carried
``F.CrtYd`` / ``B.CrtYd`` geometry, and ``kct check`` coverage still said
``courtyard_overlap: checked``.  Boards 00, 01 and 04 were signed off that way
with zero courtyard pairs evaluated.  Now:

* each non-exempt footprint with pads and no courtyard gets one
  ``courtyard_missing`` warning;
* coverage reports the blocking ``unknown:no_courtyard_geometry (N of M
  footprints)``, N counting unresolvable outlines too;
* ``courtyard_missing`` is an assembly-affecting rule, and the blocking
  unknown stops ``kct readiness``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import check_cmd
from kicad_tools.cli.readiness_cmd import (
    ASSEMBLY_AFFECTING_RULES,
    FAILED,
    PASSED,
    ReadinessOptions,
    _gate_warning_review,
)
from kicad_tools.geometry.courtyard import courtyard_exempt
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import coverage as cov
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.courtyard import (
    COURTYARD_MISSING_RULE_ID,
    COURTYARD_RULE_ID,
    COURTYARD_UNRESOLVED_RULE_ID,
    CourtyardOverlapRule,
)

pytest.importorskip("shapely")

REPO = Path(__file__).resolve().parents[1]

HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (49 "F.Fab" user)
  )
  (net 0 "")
  (net 1 "A")
  (net 2 "B")
  (gr_rect (start 100 100) (end 140 130) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
"""

RECT = (
    "    (fp_rect (start -1.75 -0.95) (end 1.75 0.95) "
    '(stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))\n'
)
# A courtyard drawn only with arcs: geometry exists but no polygon resolves.
ARC_ONLY = (
    "    (fp_arc (start -1 0) (mid 0 -1) (end 1 0) "
    '(stroke (width 0.05) (type solid)) (layer "F.CrtYd"))\n'
)
PADS = (
    '    (pad "1" smd roundrect (at -1 0) (size 1 1.3) (layers "F.Cu" "F.Paste" "F.Mask") '
    '(roundrect_rratio 0.25) (net 1 "A"))\n'
    '    (pad "2" smd roundrect (at 1 0) (size 1 1.3) (layers "F.Cu" "F.Paste" "F.Mask") '
    '(roundrect_rratio 0.25) (net 2 "B"))\n'
)


def footprint(
    ref: str,
    x: float,
    y: float = 115.0,
    *,
    lib: str = "Resistor_SMD:R_0805_2012Metric",
    courtyard: str = "",
    pads: str = PADS,
    attr: str = "(attr smd)",
) -> str:
    return (
        f'  (footprint "{lib}"\n'
        '    (layer "F.Cu")\n'
        f"    (at {x} {y})\n"
        f"    {attr}\n"
        f'    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.Fab")\n'
        "      (effects (font (size 1 1) (thickness 0.15))))\n"
        f"{courtyard}{pads}"
        "  )\n"
    )


def write_pcb(tmp_path: Path, *footprints: str) -> Path:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(HEADER + "".join(footprints) + ")\n")
    return path


def rule_findings(path: Path) -> list:
    return CourtyardOverlapRule().check(PCB.load(path)).violations


def coverage_of(path: Path):
    return cov.precheck("courtyard_overlap", DRCChecker(PCB.load(path)), pcb_path=path)


def ids(findings) -> list[str]:
    return sorted(v.rule_id for v in findings)


# ---------------------------------------------------------------------------
# The finding
# ---------------------------------------------------------------------------


def test_two_pad_only_footprints_get_two_warnings_and_unknown_coverage(tmp_path):
    pcb = write_pcb(tmp_path, footprint("R1", 110), footprint("R2", 120))

    findings = rule_findings(pcb)
    missing = [v for v in findings if v.rule_id == COURTYARD_MISSING_RULE_ID]
    assert sorted(v.items for v in missing) == [("R1",), ("R2",)]
    assert {v.severity for v in missing} == {"warning"}
    assert "courtyard overlap not checked" in missing[0].message
    assert "Resistor_SMD:R_0805_2012Metric" in missing[0].message

    state = coverage_of(pcb)
    assert state.status == "unknown:no_courtyard_geometry (2 of 2 footprints)"
    assert state.blocking and state.is_blocking_unknown


def test_mixed_board_counts_n_of_m(tmp_path):
    pcb = write_pcb(
        tmp_path,
        footprint("R1", 105, courtyard=RECT),
        footprint("R2", 115, courtyard=RECT),
        footprint("R3", 125),
    )
    assert ids(rule_findings(pcb)) == [COURTYARD_MISSING_RULE_ID]
    assert coverage_of(pcb).status == "unknown:no_courtyard_geometry (1 of 3 footprints)"


def test_all_courtyards_present_is_checked(tmp_path):
    pcb = write_pcb(
        tmp_path,
        footprint("R1", 110, courtyard=RECT),
        footprint("R2", 120, courtyard=RECT),
    )
    assert rule_findings(pcb) == []
    assert coverage_of(pcb) is None  # the caller records ``checked``


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("allow_missing_courtyard", {"attr": "(attr smd allow_missing_courtyard)"}),
        ("board_only", {"attr": "(attr smd board_only)"}),
        ("mounting hole", {"lib": "MountingHole:MountingHole_3.2mm_M3"}),
        ("fiducial", {"lib": "Fiducial:Fiducial_1mm_Mask2mm"}),
        ("net tie", {"lib": "NetTie:NetTie-2_SMD_Pad0.5mm"}),
        ("no pads (logo)", {"lib": "Symbol:OSHW-Logo_5.7x6mm_SilkScreen", "pads": ""}),
    ],
)
def test_exempt_footprints_get_no_finding_and_are_not_counted(tmp_path, label, kwargs):
    pcb = write_pcb(
        tmp_path,
        footprint("R1", 110, courtyard=RECT),
        footprint("X1", 125, **kwargs),
    )
    assert courtyard_exempt(next(f for f in PCB.load(pcb).footprints if f.reference == "X1"))
    assert rule_findings(pcb) == [], label
    assert coverage_of(pcb) is None, label


def test_dnp_footprint_is_not_exempt(tmp_path):
    pcb = write_pcb(
        tmp_path,
        footprint("R1", 110, courtyard=RECT),
        footprint("R2", 125, attr="(attr smd dnp)"),
    )
    fp = next(f for f in PCB.load(pcb).footprints if f.reference == "R2")
    assert fp.dnp and "dnp" in fp.attr_flags
    assert not courtyard_exempt(fp)
    assert ids(rule_findings(pcb)) == [COURTYARD_MISSING_RULE_ID]
    assert coverage_of(pcb).status == "unknown:no_courtyard_geometry (1 of 2 footprints)"


def test_unresolvable_outline_counts_but_keeps_its_own_info(tmp_path):
    pcb = write_pcb(
        tmp_path,
        footprint("R1", 110, courtyard=RECT),
        footprint("R2", 125, courtyard=ARC_ONLY),
    )
    findings = rule_findings(pcb)
    assert ids(findings) == [COURTYARD_UNRESOLVED_RULE_ID]
    assert findings[0].severity == "info"
    assert coverage_of(pcb).status == "unknown:no_courtyard_geometry (1 of 2 footprints)"


def test_attr_flags_accessor_reports_unmodeled_tokens(tmp_path):
    pcb = write_pcb(
        tmp_path,
        footprint("R1", 110, attr="(attr smd board_only exclude_from_bom allow_missing_courtyard)"),
    )
    fp = PCB.load(pcb).footprints[0]
    assert fp.attr_flags == frozenset({"board_only", "exclude_from_bom", "allow_missing_courtyard"})


def test_courtyard_overlap_still_errors_when_courtyards_exist(tmp_path):
    """The board-02 control: overlapping courtyards still fail at error."""
    pcb = write_pcb(
        tmp_path,
        footprint("R1", 110, courtyard=RECT),
        footprint("R2", 112, courtyard=RECT),
    )
    findings = rule_findings(pcb)
    assert ids(findings) == [COURTYARD_RULE_ID]
    assert findings[0].severity == "error"
    assert coverage_of(pcb) is None


# ---------------------------------------------------------------------------
# kct check end to end: coverage map and summary
# ---------------------------------------------------------------------------


def check_json(capsys, path: Path) -> tuple[int, dict]:
    code = check_cmd.main([str(path), "--only", "courtyard_overlap", "--format", "json"])
    return code, json.loads(capsys.readouterr().out)


def test_kct_check_reports_blocking_unknown_and_warnings(tmp_path, capsys):
    pcb = write_pcb(tmp_path, footprint("R1", 110), footprint("R2", 120))

    _, report = check_json(capsys, pcb)

    entry = report["coverage"]["courtyard_overlap"]
    assert entry["state"] == "unknown"
    assert entry["reason"] == "no_courtyard_geometry (2 of 2 footprints)"
    assert entry["blocking"] is True
    assert "courtyard_overlap" in report["summary"]["coverage_blocking_unknown"]
    assert cov.blocking_unknowns(report["coverage"]) == ["courtyard_overlap"]
    missing = [v for v in report["violations"] if v["rule_id"] == COURTYARD_MISSING_RULE_ID]
    assert len(missing) == 2
    assert report["summary"]["errors"] == 0
    assert report["summary"]["warnings"] == 2


def test_kct_check_coverage_is_checked_with_courtyards(tmp_path, capsys):
    pcb = write_pcb(
        tmp_path,
        footprint("R1", 110, courtyard=RECT),
        footprint("R2", 120, courtyard=RECT),
    )

    _, report = check_json(capsys, pcb)

    assert report["coverage"]["courtyard_overlap"]["state"] == "checked"
    assert not report["summary"]["coverage_blocking_unknown"]


def test_board00_mutation_overlap_is_now_caught(tmp_path, capsys):
    """The issue's board-00 mutation: R1 moved to overlap D1 by 0.19 mm.

    Before boards 00/01 got courtyards this move produced no finding from any
    engine.  D1's courtyard starts at x = 156 - 3.21 = 152.79 and R1's reaches
    1.75 mm east of its centre, so R1 at x = 151.23 overlaps by 0.19 mm.
    """
    board = REPO / "boards" / "00-simple-led" / "output" / "simple_led_routed.kicad_pcb"
    text = board.read_text()
    assert text.count("(at 148.5 85.5)") == 1  # R1
    path = tmp_path / "simple_led_routed.kicad_pcb"
    path.write_text(text.replace("(at 148.5 85.5)", "(at 151.23 85.5)"))

    _, report = check_json(capsys, path)

    overlaps = [v for v in report["violations"] if v["rule_id"] == COURTYARD_RULE_ID]
    assert overlaps and overlaps[0]["severity"] == "error"
    assert set(overlaps[0]["items"]) == {"R1", "D1"}
    assert not [v for v in report["violations"] if v["rule_id"] == COURTYARD_MISSING_RULE_ID]
    assert report["coverage"]["courtyard_overlap"]["state"] == "checked"


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def _options(ack: frozenset[str] = frozenset()) -> ReadinessOptions:
    return ReadinessOptions(
        board_dir=Path("/nonexistent"),
        pcb=Path("/nonexistent/board.kicad_pcb"),
        schematic=None,
        project=None,
        manufacturer="jlcpcb",
        mode="assembly",
        output_dir=Path("/nonexistent/manufacturing"),
        evidence_dir=Path("/nonexistent/readiness"),
        ack_warnings=ack,
    )


def test_courtyard_missing_is_assembly_affecting():
    assert COURTYARD_MISSING_RULE_ID in ASSEMBLY_AFFECTING_RULES
    report = {"violations": [{"rule_id": COURTYARD_MISSING_RULE_ID, "severity": "warning"}]}

    outcome, counts = _gate_warning_review(_options(), report)
    assert outcome.status == FAILED
    assert counts == {COURTYARD_MISSING_RULE_ID: 1}

    acked, _ = _gate_warning_review(_options(frozenset({COURTYARD_MISSING_RULE_ID})), report)
    assert acked.status == PASSED


def test_courtyard_missing_is_an_advisory_category_in_check():
    assert DRCChecker.category_for_rule(COURTYARD_MISSING_RULE_ID) == DRCChecker.CATEGORY_ADVISORY
