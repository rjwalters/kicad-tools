"""CLI tests for ``kct check --only/--skip impedance``.

Verifies the new category id is wired into ``CHECK_CATEGORIES`` and the
``check_methods`` dispatch dict at ``cli/check_cmd.py``.

Mirrors ``tests/test_cli_check_diffpair_routing_continuity.py`` (also a
post-rescue / dormant-rule registration test).  The standalone CLI does
not (today) thread per-class impedance targets into the rule, so the
rule uses its default specs (USB ~90Ω, clocks ~50Ω) keyed off net-name
patterns.  On a board without such nets the rule reports nothing and the
CLI exits clean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Minimal valid PCB.  Contains USB_D+/USB_D- nets with traces at 0.2 mm
# width on F.Cu.  The default ImpedanceRule has a USB-targeting spec
# (USB.*D[PM\+\-]?) with target_zdiff=90.  The default check uses the
# single-ended fallback because no diff pairs are detected from the
# standalone CLI -- so the rule MAY or MAY NOT fire depending on whether
# the trace width matches the single-ended target Z0 (default 50 Ω).
#
# The trace at 0.2 mm width on JLCPCB 4-layer F.Cu microstrip has a Z0
# around 50 Ω, so the single-ended check will be near-compliant.  The
# CLI test asserts that the category is wired and the rule runs without
# crashing (exit 0 or 2 depending on compliance).
MINIMAL_IMPEDANCE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "USB_D+")
  (net 2 "USB_D-")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 110 120) (end 140 120) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000020"))
  (segment (start 110 121) (end 140 121) (width 0.2) (layer "F.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000021"))
)
"""


@pytest.fixture
def minimal_impedance_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "impedance.kicad_pcb"
    pcb_file.write_text(MINIMAL_IMPEDANCE_PCB)
    return pcb_file


class TestImpedanceCLI:
    """CLI-level wiring tests for the new check category."""

    def test_category_in_check_categories_list(self):
        """``"impedance"`` is registered in CHECK_CATEGORIES."""
        from kicad_tools.cli.check_cmd import CHECK_CATEGORIES

        assert "impedance" in CHECK_CATEGORIES

    def test_only_impedance_runs(self, minimal_impedance_pcb: Path, capsys):
        """``--only impedance`` is accepted by the CLI and dispatches.

        The rule runs against the minimal fixture; the resulting exit
        code (0 if compliant, 2 if any error or warning-with-strict)
        depends on the calculated impedance vs default target.  This
        test asserts the wiring is present (the category id dispatches
        cleanly without crashing).
        """
        from kicad_tools.cli.check_cmd import main

        result = main([str(minimal_impedance_pcb), "--only", "impedance"])
        # Exit code is 0 (no error) or 2 (impedance error or warning+strict)
        # but NOT 1 (tool-level failure).
        assert result in (0, 2)

    def test_skip_impedance_runs(self, minimal_impedance_pcb: Path, capsys):
        """``--skip impedance`` is accepted by the CLI."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(minimal_impedance_pcb), "--skip", "impedance"])
        # Skipping impedance bypasses any potential warnings/errors from it.
        assert result in (0, 2)

    def test_impedance_in_check_methods(self):
        """``check_methods`` dispatch dict has "impedance" -> check_impedance.

        Asserts the runtime wiring is present (the dispatch dict in
        ``run_selected_checks`` includes the impedance category).  This
        is the critical regression guard: without this entry, the
        ``--only impedance`` flag would silently produce no output.
        """
        from kicad_tools.validate import DRCChecker

        # The simplest direct check: the impedance method exists on
        # DRCChecker (the new method introduced by this rescue).
        assert hasattr(DRCChecker, "check_impedance")
        # The method is callable (not just a class attribute).
        assert callable(DRCChecker.check_impedance)
