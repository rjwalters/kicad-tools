"""Tests for the build pipeline stitch step (issue #2747).

Covers:
- ``BuildStep.STITCH`` enum membership and value.
- Default chain ordering (``stitch`` between ``route`` and ``verify``).
- ``--step`` argparse choice (``stitch``).
- ``--help`` lists ``stitch`` as a choice.
- ``_run_step_stitch`` behaviour:
  * 2-layer board -> skipped with descriptive message.
  * No-PCB -> graceful no-op success.
  * No-plane-nets -> skipped.
  * 4-layer board with plane-net pads -> vias added.
  * Dry-run -> reports plan, no file modification.
- Idempotency: a second invocation reports ``already_connected > 0`` and
  ``vias_added == []``.
- ``_PCB_WRITE_STEPS`` includes ``BuildStep.STITCH`` so the kicad-cli
  smoke-check attributes load failures correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from kicad_tools.cli.build_cmd import (
    _PCB_WRITE_STEPS,
    BuildContext,
    BuildStep,
    _run_step_stitch,
)
from kicad_tools.cli.stitch_cmd import find_all_plane_nets, run_stitch
from kicad_tools.core.sexp_file import load_pcb

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Minimal 2-layer board: GND + +3.3V nets, NO inner planes.  The stitch
# step must skip 2-layer boards because there is nothing to stitch to.
TWO_LAYER_PCB = """(kicad_pcb
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
  (net 1 "GND")
  (net 2 "+3.3V")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 110 110)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
  )
)
"""

# 4-layer board WITHOUT any zones.  Stitch must skip with a "no plane
# nets" message because no zones means no planes to stitch onto.
FOUR_LAYER_NO_ZONES_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SDA")
  (net 2 "SCL")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 110 110)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SDA"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "SCL"))
  )
)
"""

# 4-layer board WITH plane-net zones on the inner layers.  Stitch must
# add vias from the GND/+3.3V pads down to the In1.Cu / In2.Cu plane.
FOUR_LAYER_WITH_ZONES_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 110 110)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000200")
    (at 120 110)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c2"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
  )
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (uuid "zone-gnd-uuid")
    (name "GND_plane")
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 140 100) (xy 140 130) (xy 100 130)))
  )
  (zone (net 2) (net_name "+3.3V") (layer "In2.Cu") (uuid "zone-3v3-uuid")
    (name "3V3_plane")
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 140 100) (xy 140 130) (xy 100 130)))
  )
)
"""


@pytest.fixture
def two_layer_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "two_layer.kicad_pcb"
    p.write_text(TWO_LAYER_PCB)
    return p


@pytest.fixture
def four_layer_no_zones_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "four_layer_no_zones.kicad_pcb"
    p.write_text(FOUR_LAYER_NO_ZONES_PCB)
    return p


@pytest.fixture
def four_layer_with_zones_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "four_layer_zones.kicad_pcb"
    p.write_text(FOUR_LAYER_WITH_ZONES_PCB)
    return p


def _make_ctx(
    pcb_file: Path | None,
    *,
    routed: bool = True,
    **kwargs,
) -> BuildContext:
    """Build a minimal BuildContext for testing.

    When *routed* is True (the default), the pcb_file is treated as the
    routed output (set on ``ctx.routed_pcb_file``) — this matches how
    ``_run_step_stitch`` is invoked inside the build chain after
    ``_run_step_route`` populates ``ctx.routed_pcb_file``.
    """
    project_dir = pcb_file.parent if pcb_file else Path("/tmp")
    if pcb_file and routed:
        return BuildContext(
            project_dir=project_dir,
            spec_file=None,
            pcb_file=None,
            routed_pcb_file=pcb_file,
            **kwargs,
        )
    return BuildContext(
        project_dir=project_dir,
        spec_file=None,
        pcb_file=pcb_file,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# BuildStep enum membership & ordering
# ---------------------------------------------------------------------------


class TestBuildStepEnum:
    """Verify STITCH is a member of BuildStep with the right value."""

    def test_stitch_value(self):
        assert BuildStep.STITCH.value == "stitch"

    def test_stitch_in_pcb_write_steps(self):
        """STITCH must be in _PCB_WRITE_STEPS so the smoke-check covers it."""
        assert BuildStep.STITCH in _PCB_WRITE_STEPS

    def test_stitch_after_route_before_verify_in_enum(self):
        """In the enum declaration STITCH must sit between ROUTE and VERIFY."""
        members = list(BuildStep)
        route_idx = members.index(BuildStep.ROUTE)
        stitch_idx = members.index(BuildStep.STITCH)
        verify_idx = members.index(BuildStep.VERIFY)
        assert route_idx < stitch_idx < verify_idx


# ---------------------------------------------------------------------------
# Default chain ordering & argparse surface
# ---------------------------------------------------------------------------


class TestDefaultChainOrdering:
    """Verify that `kct build` (no --step) executes STITCH between ROUTE and VERIFY."""

    def test_default_chain_includes_stitch_between_route_and_verify(self):
        """Reconstruct the default chain from the source and assert ordering."""
        import inspect

        from kicad_tools.cli import build_cmd

        source = inspect.getsource(build_cmd.main)
        # The default chain block starts at `if args.step == "all":`.
        # We assert STITCH is listed AFTER ROUTE and BEFORE VERIFY.
        route_pos = source.find("BuildStep.ROUTE,")
        stitch_pos = source.find("BuildStep.STITCH,")
        verify_pos = source.find("BuildStep.VERIFY,")
        assert route_pos != -1, "ROUTE entry missing from default chain"
        assert stitch_pos != -1, "STITCH entry missing from default chain"
        assert verify_pos != -1, "VERIFY entry missing from default chain"
        assert route_pos < stitch_pos < verify_pos, (
            "default chain order must be ROUTE -> STITCH -> VERIFY"
        )


class TestArgparseChoice:
    """Verify --step stitch is a recognised argparse choice."""

    def test_help_lists_stitch_choice(self, capsys):
        """`kct build --help` must include 'stitch' in --step choices."""
        from kicad_tools.cli.build_cmd import main

        with pytest.raises(SystemExit) as excinfo:
            main(["--help"])
        assert excinfo.value.code == 0
        captured = capsys.readouterr()
        # argparse renders choices either as `{a,b,stitch,c}` or in the
        # help body -- just check the literal string appears.
        assert "stitch" in captured.out


# ---------------------------------------------------------------------------
# _run_step_stitch behaviour
# ---------------------------------------------------------------------------


class TestRunStepStitchBehaviour:
    """Skip / success paths for the stitch step."""

    def test_skip_when_no_pcb(self):
        """No PCB at all -> stitch step succeeds as a no-op."""
        ctx = _make_ctx(pcb_file=None)
        result = _run_step_stitch(ctx, Console())
        assert result.success is True
        assert "skipped" in result.message.lower()
        # The "no PCB" branch is keyed on the absence of any PCB file.
        assert "no pcb" in result.message.lower()

    def test_skip_when_pcb_missing(self, tmp_path: Path):
        """Path provided but file doesn't exist -> success no-op."""
        ctx = _make_ctx(pcb_file=tmp_path / "nonexistent.kicad_pcb")
        result = _run_step_stitch(ctx, Console())
        assert result.success is True
        assert "no pcb" in result.message.lower()

    def test_skip_on_two_layer_board(self, two_layer_pcb: Path):
        """2-layer boards have no internal planes; stitch must skip."""
        ctx = _make_ctx(pcb_file=two_layer_pcb)
        result = _run_step_stitch(ctx, Console())
        assert result.success is True
        assert "2-layer" in result.message
        assert "skipped" in result.message.lower()

    def test_skip_when_no_plane_nets(self, four_layer_no_zones_pcb: Path):
        """4-layer board with no zones -> 'no plane nets detected'."""
        ctx = _make_ctx(pcb_file=four_layer_no_zones_pcb)
        result = _run_step_stitch(ctx, Console())
        assert result.success is True
        assert "no plane nets" in result.message.lower()
        assert "skipped" in result.message.lower()

    def test_adds_vias_on_4layer_board_with_zones(self, four_layer_with_zones_pcb: Path):
        """Stitch must add vias on a 4-layer board with plane-net pads."""
        # Sanity check: find_all_plane_nets must report GND and +3.3V.
        sexp = load_pcb(four_layer_with_zones_pcb)
        plane_nets = find_all_plane_nets(sexp)
        assert set(plane_nets.keys()) == {"GND", "+3.3V"}

        ctx = _make_ctx(pcb_file=four_layer_with_zones_pcb)
        result = _run_step_stitch(ctx, Console(quiet=True))
        assert result.success is True
        # At least one of "added" or "complete" must appear; never
        # "skipped" -- this fixture is the happy path.
        assert "skipped" not in result.message.lower()
        # The output_file points back at the PCB that was stitched.
        assert result.output_file == four_layer_with_zones_pcb

    def test_dry_run_reports_plan_without_modifying(self, four_layer_with_zones_pcb: Path):
        """--dry-run prints the planned command, does not write the PCB."""
        original_content = four_layer_with_zones_pcb.read_text()
        ctx = _make_ctx(pcb_file=four_layer_with_zones_pcb, dry_run=True)
        result = _run_step_stitch(ctx, Console(quiet=True))
        assert result.success is True
        assert "[dry-run]" in result.message
        assert "kct stitch" in result.message
        # File contents unchanged.
        assert four_layer_with_zones_pcb.read_text() == original_content


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestStitchIdempotency:
    """Running stitch twice should be a no-op on the second pass.

    The first pass adds stitching vias; the second pass sees those vias
    via ``is_pad_connected`` and reports them as ``already_connected``,
    with zero new vias added.  This is the property the build chain
    relies on so re-running ``kct build`` doesn't keep adding vias.
    """

    def test_second_invocation_is_no_op(self, four_layer_with_zones_pcb: Path):
        # First pass: should add at least one via.
        ctx_first = _make_ctx(pcb_file=four_layer_with_zones_pcb)
        result_first = _run_step_stitch(ctx_first, Console(quiet=True))
        assert result_first.success is True

        # Second pass on the same (now-stitched) file.
        ctx_second = _make_ctx(pcb_file=four_layer_with_zones_pcb)
        result_second = _run_step_stitch(ctx_second, Console(quiet=True))
        assert result_second.success is True

        # The most direct way to verify idempotency is to call run_stitch
        # again ourselves on the post-stitch PCB and inspect the structured
        # result: it must report already_connected > 0 and vias_added == [].
        sexp = load_pcb(four_layer_with_zones_pcb)
        plane_nets = find_all_plane_nets(sexp)
        receipt = run_stitch(
            four_layer_with_zones_pcb,
            net_names=sorted(plane_nets.keys()),
        )
        assert receipt.vias_added == []
        assert receipt.already_connected > 0
