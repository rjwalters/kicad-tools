"""CLI tests for ``kct check --only/--skip ampacity`` (Issue #4217).

Verifies the new ``ampacity`` category is wired into ``CHECK_CATEGORIES``
and the ``check_methods`` dispatch dict at ``cli/check_cmd.py``, and that
``DRCChecker.check_ampacity`` exists.

Mirrors ``tests/test_cli_check_impedance.py``.  Ampacity targets are
declarative (per-net via the net-class-map sidecar), so a standalone
``kct check --only ampacity`` on a board with no sidecar is a clean
no-op: the category dispatches, the rule sees an empty spec map, and the
CLI exits without an ampacity error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Minimal valid PCB with a single wide power net segment.  No net-class
# sidecar is supplied on the standalone CLI path, so the ampacity rule
# derives no specs and reports nothing.
MINIMAL_AMPACITY_PCB = """(kicad_pcb
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
  (net 1 "VBUS")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 110 120) (end 140 120) (width 0.5) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000040"))
)
"""


@pytest.fixture
def minimal_ampacity_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "ampacity.kicad_pcb"
    pcb_file.write_text(MINIMAL_AMPACITY_PCB)
    return pcb_file


class TestAmpacityCLI:
    """CLI-level wiring tests for the new check category."""

    def test_category_in_check_categories_list(self):
        """``"ampacity"`` is registered in CHECK_CATEGORIES."""
        from kicad_tools.cli.check_cmd import CHECK_CATEGORIES

        assert "ampacity" in CHECK_CATEGORIES

    def test_only_ampacity_runs(self, minimal_ampacity_pcb: Path, capsys):
        """``--only ampacity`` is accepted and dispatches cleanly.

        Without a ``--net-class-map`` sidecar the rule is a no-op (0
        ampacity errors).  The exit code is 0 or 2 (2 = overall
        INCOMPLETE because no schematic is present for ERC/LVS), but NOT 1
        (tool-level failure).
        """
        from kicad_tools.cli.check_cmd import main

        result = main([str(minimal_ampacity_pcb), "--only", "ampacity"])
        assert result in (0, 2)

    def test_skip_ampacity_runs(self, minimal_ampacity_pcb: Path, capsys):
        """``--skip ampacity`` is accepted by the CLI."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(minimal_ampacity_pcb), "--skip", "ampacity"])
        assert result in (0, 2)

    def test_ampacity_in_check_methods(self):
        """``DRCChecker`` exposes ``check_ampacity`` and it is registered."""
        from kicad_tools.validate import DRCChecker

        assert hasattr(DRCChecker, "check_ampacity")
        assert callable(DRCChecker.check_ampacity)
        assert "check_ampacity" in DRCChecker.CHECK_ALL_METHODS

    def test_check_ampacity_no_sidecar_is_clean_noop(self, minimal_ampacity_pcb: Path):
        """Standalone ``check_ampacity`` (no net_class_map) reports nothing."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(str(minimal_ampacity_pcb))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_ampacity()
        assert results.errors == []
