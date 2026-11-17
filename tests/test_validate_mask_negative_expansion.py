"""mask_negative_expansion: opening smaller than its pad (Issue #8847)."""

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker
from kicad_tools.validate.coverage import CHECKED
from tests._mask_expansion_fixture import write_board


def _checker(path):
    return DRCChecker(PCB.load(str(path)), manufacturer="jlcpcb", layers=2)


def test_negative_board_margin_flagged(tmp_path):
    path = write_board(
        tmp_path / "b.kicad_pcb",
        [("rect", "1 2", "")],
        setup="(pad_to_mask_clearance -0.05)",
    )
    res = _checker(path).check_mask_negative_expansion()
    assert [v.rule_id for v in res.violations] == ["mask_negative_expansion"]
    assert res.violations[0].severity == "warning"
    assert res.violations[0].items == ("U1",)


def test_zero_and_positive_not_flagged(tmp_path):
    path = write_board(
        tmp_path / "b.kicad_pcb",
        [("rect", "1 2", ""), ("rect", "1 2", "(solder_mask_margin 0.05)")],
    )
    assert not _checker(path).check_mask_negative_expansion().violations


def test_pad_override_wins_over_board_default(tmp_path):
    # Board default negative, pad override positive -> clean; and vice versa.
    clean = write_board(
        tmp_path / "a.kicad_pcb",
        [("rect", "1 2", "(solder_mask_margin 0.05)")],
        setup="(pad_to_mask_clearance -0.05)",
    )
    assert not _checker(clean).check_mask_negative_expansion().violations
    dirty = write_board(
        tmp_path / "c.kicad_pcb",
        [("rect", "1 2", "(solder_mask_margin -0.05)")],
        setup="(pad_to_mask_clearance 0.1)",
    )
    assert len(_checker(dirty).check_mask_negative_expansion().violations) == 1


def test_registered_and_in_jlcpcb_coverage(tmp_path, capsys):
    import json

    from kicad_tools.cli.check_cmd import main

    path = write_board(tmp_path / "b.kicad_pcb", [("rect", "1 2", "(solder_mask_margin -0.05)")])
    main([str(path), "--mfr", "jlcpcb", "--format", "json", "--drc-only"])
    data = json.loads(capsys.readouterr().out)
    cov = data["coverage"]
    for cat in ("mask_negative_expansion", "mask_multi_segment"):
        assert cov[cat]["state"] == CHECKED, cov[cat]
    assert any(v["rule_id"] == "mask_negative_expansion" for v in data["violations"])
