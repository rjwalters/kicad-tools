"""A missing ``report`` extra must fail ``kct export`` loudly (issue #8203).

Previously ``ManufacturingPackage._generate_report`` logged one warning line and
returned, so the bundle shipped without ``report.md`` and the command exited 0.
The import failure is simulated by blocking the module in ``sys.modules``; no
package is uninstalled.
"""

from __future__ import annotations

import sys
from pathlib import Path

from kicad_tools.export.manufacturing import (
    ManufacturingConfig,
    ManufacturingPackage,
    ManufacturingResult,
)


def _exporter(tmp_path: Path, include_report: bool) -> ManufacturingPackage:
    return ManufacturingPackage(
        tmp_path / "board.kicad_pcb",
        config=ManufacturingConfig(include_report=include_report),
    )


def test_missing_report_dependency_is_an_error(tmp_path, monkeypatch):
    # A None entry makes ``import`` raise ImportError with name=<module>.
    monkeypatch.setitem(sys.modules, "kicad_tools.report.collector", None)
    result = ManufacturingResult(output_dir=tmp_path)

    _exporter(tmp_path, True)._generate_report(tmp_path, result)

    assert result.success is False
    assert len(result.errors) == 1
    assert "kicad_tools.report.collector" in result.errors[0]
    assert "kicad-tools[report]" in result.errors[0]
    assert "e.g." not in result.errors[0]


def test_names_the_actual_failing_module(tmp_path, monkeypatch):
    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.endswith("report.collector"):
            raise ImportError("No module named 'weasyprint'", name="weasyprint")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)
    result = ManufacturingResult(output_dir=tmp_path)

    _exporter(tmp_path, True)._generate_report(tmp_path, result)

    assert result.success is False
    assert "'weasyprint'" in result.errors[0]
    assert "kicad-tools[report]" in result.errors[0]
