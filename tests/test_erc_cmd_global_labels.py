"""Integration tests for cross-sheet global-label filtering in ``kct erc``.

Regression coverage for the bug where ``kct erc`` over-reported
single-pin / isolated nets for global labels that legitimately span
multiple hierarchical sheets (issue #3808).  The suppression logic lived in
``filter_cross_sheet_global_labels`` and was wired into ``kct sch validate``
but never into the ``kct erc`` path.
"""

from pathlib import Path

import pytest

from kicad_tools.cli import erc_cmd
from kicad_tools.core.types import ERCSeverity
from kicad_tools.erc.report import ERCReport
from kicad_tools.erc.violation import ERCViolation, ERCViolationType

# Schematic templates: a root that instantiates one child sheet, with global
# labels placed on both so the shared label spans 2 sheets.

_ROOT_TEMPLATE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "root-uuid-3808")
  (paper "A4")
  (lib_symbols)
  {global_labels}
  (sheet
    (at 130 40) (size 40 30)
    (uuid "sheet-sub-3808")
    (property "Sheetname" "Sub" (at 130 39 0) (effects (font (size 1.27 1.27))))
    (property "Sheetfile" "sub.kicad_sch" (at 130 71 0) (effects (font (size 1.27 1.27))))
  )
)
"""

_SUB_TEMPLATE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "sub-uuid-3808")
  (paper "A4")
  (lib_symbols)
  {global_labels}
)
"""

_GLOBAL_LABEL = """\
  (global_label "{text}"
    (shape input)
    (at 100 {y} 0)
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "{uuid}")
  )
"""


def _gl(text: str, uuid: str, y: int = 100) -> str:
    return _GLOBAL_LABEL.format(text=text, uuid=uuid, y=y)


def _write_two_sheet_design(tmp_path: Path) -> Path:
    """Root + child sharing ``MCU_CLK``; ``LONELY`` only on root."""
    root = _ROOT_TEMPLATE.format(
        global_labels=_gl("MCU_CLK", "gl-root-clk", 100) + _gl("LONELY", "gl-root-lonely", 110)
    )
    sub = _SUB_TEMPLATE.format(global_labels=_gl("MCU_CLK", "gl-sub-clk", 100))
    (tmp_path / "design.kicad_sch").write_text(root)
    (tmp_path / "sub.kicad_sch").write_text(sub)
    return tmp_path / "design.kicad_sch"


def _isolated_violation(label: str, sheet: str) -> ERCViolation:
    return ERCViolation(
        type=ERCViolationType.ISOLATED_PIN_LABEL,
        type_str="isolated_pin_label",
        severity=ERCSeverity.ERROR,
        description="Label connected to only one pin",
        sheet=sheet,
        items=[f"Global Label '{label}'"],
    )


def test_kct_erc_suppresses_cross_sheet_false_positive(tmp_path, monkeypatch, capsys):
    """A global label on >= 2 sheets is not reported, and exit code is 0."""
    schematic = _write_two_sheet_design(tmp_path)

    report = ERCReport(
        source_file=str(schematic),
        violations=[_isolated_violation("MCU_CLK", "/")],
    )

    monkeypatch.setattr(erc_cmd, "run_erc_on_schematic", lambda *a, **k: report)

    exit_code = erc_cmd.main([str(schematic), "--verbose"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "MCU_CLK" not in out
    assert "PASSED" in out


def test_kct_erc_still_reports_genuine_single_pin(tmp_path, monkeypatch, capsys):
    """A genuinely isolated single-sheet label is still reported (exit 1)."""
    schematic = _write_two_sheet_design(tmp_path)

    report = ERCReport(
        source_file=str(schematic),
        violations=[_isolated_violation("LONELY", "/")],
    )

    monkeypatch.setattr(erc_cmd, "run_erc_on_schematic", lambda *a, **k: report)

    # --verbose surfaces the label name from the items array.
    exit_code = erc_cmd.main([str(schematic), "--verbose"])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "LONELY" in out
    assert "isolated_pin_label" in out or "only one pin" in out


def test_kct_erc_mixed_suppresses_only_false_positive(tmp_path, monkeypatch, capsys):
    """Mixed input: cross-sheet label suppressed, genuine one retained."""
    schematic = _write_two_sheet_design(tmp_path)

    report = ERCReport(
        source_file=str(schematic),
        violations=[
            _isolated_violation("MCU_CLK", "/"),  # false positive (2 sheets)
            _isolated_violation("LONELY", "/"),  # genuine (1 sheet)
        ],
    )

    monkeypatch.setattr(erc_cmd, "run_erc_on_schematic", lambda *a, **k: report)

    exit_code = erc_cmd.main([str(schematic), "--verbose"])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "MCU_CLK" not in out
    assert "LONELY" in out


def test_kct_erc_parse_bare_report_does_not_crash(tmp_path, capsys):
    """Parsing a bare report (no schematic) skips the filter gracefully."""
    report_json = tmp_path / "report.json"
    report_json.write_text(
        """
        {
          "source": "design.kicad_sch",
          "kicad_version": "8.0",
          "coordinate_units": "mm",
          "sheets": [
            {
              "path": "/",
              "violations": [
                {
                  "type": "isolated_pin_label",
                  "severity": "error",
                  "description": "Label connected to only one pin",
                  "items": [{"description": "Global Label 'MCU_CLK'"}]
                }
              ]
            }
          ]
        }
        """
    )

    exit_code = erc_cmd.main([str(report_json)])
    out = capsys.readouterr().out

    # Filter is skipped for bare reports, so the violation flows through.
    assert exit_code == 1
    assert "isolated_pin_label" in out or "only one pin" in out


def test_kct_erc_filter_failure_is_non_fatal(tmp_path, monkeypatch, capsys):
    """A failure building the inventory must not crash a clean ERC run."""
    schematic = tmp_path / "missing.kicad_sch"  # never written

    report = ERCReport(
        source_file=str(schematic),
        violations=[_isolated_violation("MCU_CLK", "/")],
    )

    monkeypatch.setattr(erc_cmd, "run_erc_on_schematic", lambda *a, **k: report)

    # The filter will attempt to traverse a nonexistent hierarchy; the
    # try/except in erc_cmd must swallow any error and keep the violation.
    exit_code = erc_cmd.main([str(schematic), "--verbose"])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "MCU_CLK" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
