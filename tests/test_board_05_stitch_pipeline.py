"""Regression guard: board-05 design.py pipeline has a stitch step (#3936).

Issue #3936: ``boards/05-bldc-motor-controller/design.py`` ran
route -> rescue -> fill -> DRC -> export but never invoked ``kct stitch``.
As a result, every SMD pad whose pour-carried net (GND / +24V / +3V3 /
+5V) lands on a different copper layer than the pad was left floating --
a fresh regen stranded 58 pads (+24V: 7, +3V3: 18, GND: 33).  ``design.py``
now runs :func:`stitch_pcb` as Step 6c, after routing/rescue and before
zone fill.

This test pins that behaviour on two axes:

1. **Static:** ``design.py`` defines a ``stitch_pcb`` callable and calls
   it from ``main`` between the rescue step and the zone-fill step.  This
   is the cheap, KiCad-independent guard that catches accidental removal
   of the pipeline stage.

2. **Functional:** the underlying stitch primitive
   (:func:`kicad_tools.cli.stitch_cmd.run_stitch`) places at least one
   via on each of GND / +24V / +3V3 when run (dry-run) against the
   committed routed artifact.  This is the same auto-detected-net path
   ``stitch_pcb`` drives via the CLI, and it proves the stranded pour
   pads are reachable -- i.e. the pipeline step will actually connect
   them on a fresh regen.

The committed routed PCB is used **read-only** (dry-run + scratch copy);
the artifact-first shipping truth is never modified by this test.  See
also ``tests/test_board_05_thermal_stitch.py`` (thermal-via primitive on
the same artifact) and ``tests/test_fleet_45_census.py`` (committed
copper census, unaffected by this pipeline-only change).
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

from kicad_tools.cli.stitch_cmd import find_all_plane_nets, run_stitch
from kicad_tools.core.sexp_file import load_pcb

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "05-bldc-motor-controller"
DESIGN_PY = BOARD_DIR / "design.py"
ROUTED_PCB = BOARD_DIR / "output" / "bldc_controller_routed.kicad_pcb"

# The three pour nets the issue calls out as strander-prone.  +5V is a
# pour net too but its pads happen to sit on the pour layer, so we do not
# require a via on it (the auto-detect path still considers it).
REQUIRED_STITCH_NETS = ("GND", "+24V", "+3V3")


class TestBoard05StitchPipelineStatic:
    """Static guards that design.py wires the stitch step into main()."""

    def test_design_defines_stitch_pcb(self) -> None:
        """design.py must define a top-level ``stitch_pcb`` function."""
        tree = ast.parse(DESIGN_PY.read_text())
        funcs = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        assert "stitch_pcb" in funcs, (
            "boards/05-bldc-motor-controller/design.py must define a "
            "stitch_pcb() function (Issue #3936). Without it the pipeline "
            "leaves pour-net pads stranded."
        )

    def test_main_calls_stitch_between_rescue_and_fill(self) -> None:
        """main() must call stitch_pcb after rescue and before fill.

        Ordering is load-bearing: stitch must read the real placed copper
        (so it runs after route/rescue) and its new vias must be bonded
        into the plane by the subsequent zone re-fill (so it runs before
        fill_zones_in_routed_pcb).
        """
        tree = ast.parse(DESIGN_PY.read_text())
        main_fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )

        # Record the source line of the first call to each pipeline fn.
        first_call_line: dict[str, int] = {}
        for node in ast.walk(main_fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                name = node.func.id
                if name not in first_call_line:
                    first_call_line[name] = node.lineno

        assert "stitch_pcb" in first_call_line, "main() must call stitch_pcb() (Issue #3936)."
        assert "rescue_partial_nets" in first_call_line, (
            "main() should still call rescue_partial_nets()."
        )
        assert "fill_zones_in_routed_pcb" in first_call_line, (
            "main() should still call fill_zones_in_routed_pcb()."
        )

        assert (
            first_call_line["rescue_partial_nets"]
            < first_call_line["stitch_pcb"]
            < first_call_line["fill_zones_in_routed_pcb"]
        ), (
            "stitch_pcb() must run AFTER rescue_partial_nets() (real copper "
            "placed) and BEFORE fill_zones_in_routed_pcb() (so the re-fill "
            "bonds the new stitch vias into the plane). Current order:\n"
            f"  rescue_partial_nets @ line {first_call_line['rescue_partial_nets']}\n"
            f"  stitch_pcb          @ line {first_call_line['stitch_pcb']}\n"
            f"  fill_zones          @ line {first_call_line['fill_zones_in_routed_pcb']}"
        )


@pytest.fixture(scope="module")
def routed_pcb_path() -> Path:
    """Resolve the committed routed PCB or skip if absent."""
    if not ROUTED_PCB.exists():
        pytest.skip(
            f"Board 05 routed PCB not found at {ROUTED_PCB!s}; "
            "regenerate via "
            "`uv run python boards/05-bldc-motor-controller/design.py`"
        )
    return ROUTED_PCB


@pytest.fixture
def routed_pcb_copy(routed_pcb_path: Path, tmp_path: Path) -> Path:
    """Yield a per-test scratch copy of the committed routed PCB.

    ``run_stitch`` only mutates the file when ``dry_run=False``; we copy
    anyway so the committed artifact is never touched even if a future
    change adds a side-effect.
    """
    dest = tmp_path / "bldc_controller_routed.kicad_pcb"
    shutil.copy2(routed_pcb_path, dest)
    return dest


class TestBoard05StitchPipelineFunctional:
    """The stitch primitive can reach the stranded pour pads."""

    def test_plane_nets_autodetected(self, routed_pcb_copy: Path) -> None:
        """find_all_plane_nets (the path stitch_pcb drives) sees the pours.

        stitch_pcb runs ``kct stitch`` with no explicit ``--net`` so the
        stitcher auto-detects plane nets from the board's zones. This
        asserts that auto-detect surfaces the required pour nets.
        """
        plane_nets = find_all_plane_nets(load_pcb(routed_pcb_copy))
        missing = [n for n in REQUIRED_STITCH_NETS if n not in plane_nets]
        assert not missing, (
            "find_all_plane_nets did not auto-detect required pour net(s): "
            f"{missing}. Detected: {sorted(plane_nets)}"
        )

    def test_stitch_places_vias_on_pour_nets(
        self,
        routed_pcb_copy: Path,
    ) -> None:
        """run_stitch places >=1 via on each of GND / +24V / +3V3.

        Drives the same auto-detected-net stitch that ``stitch_pcb``
        runs via the CLI, in dry-run, against a copy of the committed
        routed artifact. Every required pour net must receive at least
        one via -- proving the stranded pads (issue: 58 across these
        nets) are reachable and the pipeline step will connect them on a
        fresh regen.
        """
        plane_nets = find_all_plane_nets(load_pcb(routed_pcb_copy))
        result = run_stitch(
            routed_pcb_copy,
            net_names=sorted(plane_nets.keys()),
            dry_run=True,
            micro_via=True,
        )

        vias_per_net: dict[str, int] = {}
        for via in result.vias_added:
            net = via.pad.net_name
            vias_per_net[net] = vias_per_net.get(net, 0) + 1

        shortfalls = [
            f"{net}: {vias_per_net.get(net, 0)} via(s)"
            for net in REQUIRED_STITCH_NETS
            if vias_per_net.get(net, 0) < 1
        ]
        assert not shortfalls, (
            "Board-05 stitch placed no via on required pour net(s):\n  "
            + "\n  ".join(shortfalls)
            + "\n\nThe design.py stitch step (Issue #3936) relies on these "
            "pads being reachable; a shortfall means stranded pour pads "
            "would survive a fresh regen. Inspect StitchResult.pads_skipped "
            "for per-pad rejection reasons."
        )
