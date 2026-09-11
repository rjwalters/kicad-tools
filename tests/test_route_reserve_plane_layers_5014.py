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

import pytest

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


class _FlagArgs:
    """Minimal stand-in for the parsed ``argparse.Namespace``."""

    def __init__(self, reserve_plane_layers: bool):
        self.reserve_plane_layers = reserve_plane_layers


class TestReservationAcrossEscalationRungs:
    """``rules`` is shared by reference across every escalation rung (#5014).

    ``route_with_layer_escalation`` builds ``DesignRules`` **once**, outside
    the ladder loop, and hands the same object to every rung; only
    ``layer_stack`` varies per attempt.  So ``_apply_plane_layer_reservation``
    must be able to *clear* a restriction, not merely tighten one -- otherwise
    a plane-free rung reached after a plane-bearing one inherits the previous
    stack's ``['F.Cu', 'B.Cu']`` and silently degenerates into a 2-layer
    route.  These tests walk one ``DesignRules`` instance through the real
    ladder, which no other test in the suite does.
    """

    @staticmethod
    def _ladder():
        from kicad_tools.router.layers import LayerStack

        # The exact ladder built by ``route_with_layer_escalation``.
        return [
            ("two_layer", LayerStack.two_layer()),
            ("four_layer_sig_gnd_pwr_sig", LayerStack.four_layer_sig_gnd_pwr_sig()),
            ("four_layer_all_signal", LayerStack.four_layer_all_signal()),
            ("six_layer_sig_gnd_sig_sig_pwr_sig", LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()),
        ]

    def test_plane_free_rung_clears_previous_rungs_restriction(self):
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        args = _FlagArgs(reserve_plane_layers=True)

        observed: dict[str, list[str] | None] = {}
        for name, stack in self._ladder():
            route_cmd_module._apply_plane_layer_reservation(rules, stack, args)
            observed[name] = rules.allowed_layers

        # Plane-bearing rungs are restricted to their non-PLANE layers...
        assert observed["four_layer_sig_gnd_pwr_sig"] == ["F.Cu", "B.Cu"]
        assert observed["six_layer_sig_gnd_sig_sig_pwr_sig"] == [
            "F.Cu",
            "In2.Cu",
            "In3.Cu",
            "B.Cu",
        ]
        # ...and plane-free rungs are UNRESTRICTED, even when they follow a
        # plane-bearing rung on the same shared ``DesignRules`` instance.
        assert observed["two_layer"] is None
        assert observed["four_layer_all_signal"] is None, (
            "four_layer_all_signal inherited the previous rung's allowed_layers "
            "-- the all-signal 4L rung would degenerate into a 2-layer route"
        )

    def test_flag_off_never_touches_allowed_layers_across_rungs(self):
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        # A pre-existing restriction from some other mechanism must survive
        # untouched when the flag is off: the helper early-``return``s.
        rules.allowed_layers = ["F.Cu", "In1.Cu"]
        args = _FlagArgs(reserve_plane_layers=False)

        for _name, stack in self._ladder():
            route_cmd_module._apply_plane_layer_reservation(rules, stack, args)
            assert rules.allowed_layers == ["F.Cu", "In1.Cu"]


class TestEscalationPlaneAudit:
    """Audit committed copper on the selected router in every terminal flow."""

    @staticmethod
    def _run(tmp_path, monkeypatch, mode, *, dry_run=False, earlier_best=False):
        from kicad_tools.core.types import CopperLayer
        from kicad_tools.router import load_pcb_for_routing as real_load
        from kicad_tools.router.layers import LayerStack
        from kicad_tools.router.primitives import Route, Segment

        pcb = _write_pcb(tmp_path)
        pcb.write_text(
            pcb.read_text()
            .replace(" (at 110 110)", ' (at 110 110) (property "Reference" "R1")')
            .replace(" (at 120 115)", ' (at 120 115) (property "Reference" "R2")')
        )
        output = tmp_path / "escalated.kicad_pcb"
        argv = [
            str(pcb),
            "-o",
            str(output),
            "--backend",
            "python",
            "--strategy",
            "basic",
            "--no-cache",
            "--skip-drc",
            "--quiet",
            "--no-optimize",
            "--max-layers",
            "4",
        ]
        if mode != "rules":
            argv += ["--starting-layers", "4"]
        if mode == "rules":
            argv += ["--no-auto-layers", "--adaptive-rules"]
        elif mode == "combined":
            argv += ["--adaptive-rules"]
        if dry_run:
            argv += ["--dry-run"]
        # The fixed-stack relaxation path detects its stack from the PCB.
        monkeypatch.setattr(
            "kicad_tools.router.io.detect_layer_stack",
            lambda _: LayerStack.four_layer_sig_gnd_pwr_sig(),
        )
        loaded = []
        attempted_stacks = []

        def load(*args, **kwargs):
            attempted_stacks.append(kwargs["layer_stack"])
            if earlier_best and loaded:
                raise ValueError("Later configuration cannot load this fixture")
            router, net_map = real_load(*args, **kwargs)
            loaded.append(router)
            if earlier_best:
                real_stats = router.get_statistics

                def partial_stats(*a, **kw):
                    return {**real_stats(*a, **kw), "nets_routed": 0}

                monkeypatch.setattr(router, "get_statistics", partial_stats)
            return router, net_map

        monkeypatch.setattr("kicad_tools.router.load_pcb_for_routing", load)
        real_finalize = route_cmd_module._finalize_routes
        selected = []

        def finalize(router, *args, **kwargs):
            result = real_finalize(router, *args, **kwargs)
            selected.append(router)
            # Model committed copper at the finalization boundary, without
            # relying on search preferences to choose an inner layer.
            router.routes.append(
                Route(
                    net=1,
                    net_name="AUDIT_SIG",
                    segments=[
                        Segment(
                            x1=110,
                            y1=110,
                            x2=111,
                            y2=110,
                            width=0.2,
                            layer=CopperLayer.IN1_CU,
                            net=1,
                        ),
                    ],
                )
            )
            return result

        monkeypatch.setattr(route_cmd_module, "_finalize_routes", finalize)
        rc = route_cmd_module.main(argv)
        assert selected == [loaded[0]]
        if earlier_best:
            assert len(attempted_stacks) > 1
            if mode != "rules":
                assert attempted_stacks[-1] != selected[0].layer_stack
        return rc

    @pytest.mark.parametrize("mode", ["layers", "rules", "combined"])
    def test_reports_committed_plane_signal(self, tmp_path, monkeypatch, capsys, mode):
        self._run(tmp_path, monkeypatch, mode)
        assert (
            "net 'AUDIT_SIG' has 1 segment routed on reference-plane layer 'In1.Cu'"
            in capsys.readouterr().err
        )

    @pytest.mark.parametrize("mode", ["layers", "rules", "combined"])
    def test_dry_run_does_not_report_committed_plane_signal(
        self, tmp_path, monkeypatch, capsys, mode
    ):
        assert self._run(tmp_path, monkeypatch, mode, dry_run=True) == 0
        assert "net 'AUDIT_SIG'" not in capsys.readouterr().err

    @pytest.mark.parametrize("mode", ["layers", "rules", "combined"])
    def test_audits_earlier_best_result(self, tmp_path, monkeypatch, capsys, mode):
        self._run(tmp_path, monkeypatch, mode, earlier_best=True)
        assert (
            "net 'AUDIT_SIG' has 1 segment routed on reference-plane layer 'In1.Cu'"
            in capsys.readouterr().err
        )
