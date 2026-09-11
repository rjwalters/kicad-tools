"""End-to-end guard for ``kct route --reserve-plane-layers`` (Issue #5014).

Unit coverage for the underlying predicates/helpers, and a real-pathfinder
proof that the derived ``allowed_layers`` value is actually honoured, live
in ``tests/test_layer_advisories.py``. This module exercises the CLI
surface reached through ``route_cmd.main``:

* ``--reserve-plane-layers`` threads through to the constructed
  ``DesignRules.allowed_layers`` for a plane-bearing stack (``--layers
  4``'s In1.Cu/In2.Cu are excluded, F.Cu/B.Cu remain routable).
* Without the flag, the Tier-3 advisory recommending it is printed to
  stderr for a plane-bearing stack.
* With the flag, the advisory is silent (the hard restriction is already
  in effect).
* A stack with no plane layers (``--layers 2``) is a no-op on both counts.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from kicad_tools.cli import route_cmd as route_cmd_module

# ---------------------------------------------------------------------------
# Fixture: minimal 2-pad rectangular PCB, pads far enough apart that the
# router has room to pick any layer. Mirrors the fixture shape used by
# tests/test_route_cmd_power_nets.py.
# ---------------------------------------------------------------------------

_PCB_TEMPLATE = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (footprint "TestLib:Pad" (layer "F.Cu") (at 110 110)
    (pad "1" smd roundrect (at 0 0) (size 1.0 1.3)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 1 "SIG"))
  )
  (footprint "TestLib:Pad" (layer "F.Cu") (at 120 115)
    (pad "1" smd roundrect (at 0 0) (size 1.0 1.3)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 1 "SIG"))
  )
  (gr_line (start 100 100) (end 130 100) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 130 100) (end 130 125) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 130 125) (end 100 125) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 100 125) (end 100 100) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
)
"""


def _write_pcb(tmp_path: Path) -> Path:
    pcb_path = tmp_path / "fixture.kicad_pcb"
    pcb_path.write_text(_PCB_TEMPLATE)
    return pcb_path


def _base_argv(pcb_path: Path, out_path: Path) -> list[str]:
    return [
        str(pcb_path),
        "-o",
        str(out_path),
        "--strategy",
        "negotiated",
        "--layers",
        "4",
        "--backend",
        "python",
        "--no-cache",
        "--skip-drc",
        "--quiet",
    ]


def _run_and_capture_design_rules(argv: list[str]) -> list:
    """Run ``route_cmd.main(argv)`` and return every ``DesignRules`` built.

    Wraps the real class (via ``side_effect``) so construction, and every
    downstream consumer of the returned instance -- including
    ``_apply_plane_layer_reservation``'s in-place ``allowed_layers``
    mutation -- behaves exactly as in production; only the constructor call
    itself is observed.
    """
    from kicad_tools.router.rules import DesignRules as RealDesignRules

    captured: list = []

    def _capture(*args, **kwargs):
        obj = RealDesignRules(*args, **kwargs)
        captured.append(obj)
        return obj

    with patch("kicad_tools.router.DesignRules", side_effect=_capture):
        route_cmd_module.main(argv)
    return captured


class TestReservePlaneLayersHardRestriction:
    def test_allowed_layers_excludes_planes_when_flag_set(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path)
        out_path = tmp_path / "out.kicad_pcb"

        captured = _run_and_capture_design_rules(
            [*_base_argv(pcb_path, out_path), "--reserve-plane-layers"]
        )

        assert captured, "DesignRules must have been constructed at least once"
        for rules in captured:
            assert rules.allowed_layers == ["F.Cu", "B.Cu"]

    def test_allowed_layers_untouched_without_flag(self, tmp_path: Path):
        pcb_path = _write_pcb(tmp_path)
        out_path = tmp_path / "out.kicad_pcb"

        captured = _run_and_capture_design_rules(_base_argv(pcb_path, out_path))

        assert captured
        for rules in captured:
            assert rules.allowed_layers is None

    def test_allowed_layers_untouched_for_2layer_stack_even_with_flag(self, tmp_path: Path):
        # No-op: a 2-layer stack has no PLANE layers to reserve.
        pcb_path = _write_pcb(tmp_path)
        out_path = tmp_path / "out.kicad_pcb"
        argv = [a if a != "4" else "2" for a in _base_argv(pcb_path, out_path)]

        captured = _run_and_capture_design_rules([*argv, "--reserve-plane-layers"])

        assert captured
        for rules in captured:
            assert rules.allowed_layers is None

    def test_baseline_without_flag_still_routes(self, tmp_path: Path):
        # Sanity: the plain (non-reserved) --layers 4 run still succeeds --
        # --reserve-plane-layers must not be required for a normal route.
        pcb_path = _write_pcb(tmp_path)
        out_path = tmp_path / "out.kicad_pcb"

        exit_code = route_cmd_module.main(_base_argv(pcb_path, out_path))
        assert exit_code in (0, 2)
        assert out_path.exists()


class TestPlaneLayerReservationAdvisory:
    def test_advisory_printed_without_flag(self, tmp_path: Path, capsys):
        pcb_path = _write_pcb(tmp_path)
        out_path = tmp_path / "out.kicad_pcb"

        route_cmd_module.main(_base_argv(pcb_path, out_path))
        err = capsys.readouterr().err
        assert "--reserve-plane-layers" in err
        assert "In1.Cu" in err

    def test_advisory_silent_with_flag(self, tmp_path: Path, capsys):
        pcb_path = _write_pcb(tmp_path)
        out_path = tmp_path / "out.kicad_pcb"

        route_cmd_module.main([*_base_argv(pcb_path, out_path), "--reserve-plane-layers"])
        err = capsys.readouterr().err
        assert "Pass --reserve-plane-layers to hard-restrict" not in err

    def test_advisory_silent_for_2layer_stack(self, tmp_path: Path, capsys):
        pcb_path = _write_pcb(tmp_path)
        out_path = tmp_path / "out.kicad_pcb"

        argv = [a if a != "4" else "2" for a in _base_argv(pcb_path, out_path)]
        route_cmd_module.main(argv)
        err = capsys.readouterr().err
        assert "--reserve-plane-layers" not in err
