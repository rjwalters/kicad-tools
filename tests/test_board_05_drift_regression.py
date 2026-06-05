"""Regression guard: board-05 fresh build emits zero schematic<->PCB value drift.

Issue #3210: prior to PR-3210, a fresh ``uv run python design.py`` against
``boards/05-bldc-motor-controller/`` shipped four schematic<->PCB value
mismatches on the same fresh build that the BOM is generated from:

* ``C3`` -- schematic emitted ``100uF`` (BuckConverter default), PCB
  silkscreen emitted ``220uF`` (hardcoded in ``generate_cap_0805``).
* ``D3`` -- schematic emitted ``PWR`` (LEDIndicator label), PCB
  silkscreen emitted ``LED`` (hardcoded in ``generate_led_0805``).
* ``D4`` -- schematic emitted ``STATUS`` (LEDIndicator label), PCB
  silkscreen emitted ``LED`` (same hardcode).
* ``J4`` -- schematic emitted ``SWD-6`` (DebugHeader._get_value_label),
  PCB silkscreen emitted ``SWD Debug`` (generate_pin_header call-site
  string literal).

(Note: the issue body also listed C4, but that row had already been
brought into sync by an earlier change -- the actual drift was four rows,
not five.  See the curator's comment on #3210.)

PR-3210 patches the **PCB-side** call sites and ``generate_led_0805``
signature to match the schematic-side ground truth -- the schematic
blocks are the design-intent document, and ``"SWD-6"`` / ``"PWR"`` /
``"STATUS"`` are also more diagnostic silkscreen labels for fab/bringup
than the previous generic strings.

This test rebuilds the schematic + unrouted PCB in a tmp directory
(skipping the slow routing/manufacturing steps -- value drift is
visible immediately after PCB generation) and asserts the canonical
``kicad_tools.sync.drift.analyze_drift(...).value_mismatches == []``.
If a future config drift re-introduces any of the four mismatches --
e.g. a Builder reverts the ``cascade.buck.input_cap.value = "220uF"``
patch, or renames a LEDIndicator label without updating the PCB
generator call -- this test fails loudly at CI time with a list of the
re-introduced ref/value/value triples.

The test runs ``design.py``'s two schematic+PCB factories directly
(``create_bldc_controller`` and ``create_bldc_pcb``), not via the
``__main__`` ``main()`` entry point, because the latter also runs ERC,
routing, zone-filling, DRC, and the manufacturing bundle -- all of which
are orthogonal to value drift and add minutes of runtime per CI run.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "05-bldc-motor-controller"
DESIGN_PY = BOARD_DIR / "design.py"


def _load_design_module():
    """Import the board-05 ``design.py`` as a fresh module.

    ``design.py`` is a script, not a packaged module, so we use
    ``importlib.util`` to load it from its absolute path.  We cache
    nothing here -- pytest's collection already runs this once per test
    session and the file load is cheap relative to ``create_bldc_*``.
    """
    if not DESIGN_PY.exists():
        pytest.skip(f"Board 05 design.py not found at {DESIGN_PY!s}")

    spec = importlib.util.spec_from_file_location("board_05_design", DESIGN_PY)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        pytest.skip(f"Could not load spec for {DESIGN_PY!s}")
    module = importlib.util.module_from_spec(spec)
    # Inject under a synthetic name so any re-import inside design.py
    # (none today, but defensive) sees the same module object.
    sys.modules["board_05_design"] = module
    spec.loader.exec_module(module)
    return module


def test_board_05_fresh_build_zero_value_drift(tmp_path: Path) -> None:
    """Fresh design.py run on board 05 must produce zero value mismatches.

    See module docstring for the four pre-PR-3210 mismatches.  The
    assertion uses the canonical ``kicad_tools.sync.drift.analyze_drift``
    helper -- the same code path that powers ``kct check`` and the
    audit pipeline -- so the test stays in lockstep with how production
    drift detection works.

    A failure of this test means one of four things regressed:

    1. ``cascade.buck.input_cap.value = "220uF"`` post-construction
       patch was reverted (C3 drifts to 100uF).
    2. ``generate_led_0805`` lost its ``value`` parameter or the call
       sites stopped passing ``"PWR"`` / ``"STATUS"`` (D3 / D4 drift
       to ``LED``).
    3. The ``generate_pin_header("J4", ..., "SWD-6", ...)`` call-site
       string regressed to ``"SWD Debug"`` (J4 drifts).
    4. A new component was added to the schematic OR PCB without the
       matching counterpart -- the assertion's error message will list
       the new mismatch's ref/values.
    """
    from kicad_tools.sync.drift import analyze_drift

    module = _load_design_module()

    # Build the schematic + unrouted PCB directly.  Skip ERC / routing /
    # zones / DRC / manufacturing -- they don't affect symbol or
    # footprint value fields, and they would add minutes of runtime.
    sch_path = module.create_bldc_controller(tmp_path)
    pcb_path = module.create_bldc_pcb(tmp_path)

    assert sch_path.exists(), f"design.py did not write schematic to {sch_path!s}"
    assert pcb_path.exists(), f"design.py did not write PCB to {pcb_path!s}"

    analysis, resolved_sch = analyze_drift(pcb_path, sch_path)
    assert analysis is not None, (
        f"analyze_drift returned None for pcb={pcb_path!s} / sch={sch_path!s} "
        "(the helper silently skips on parse failure; check that both "
        "files were generated successfully)."
    )
    assert resolved_sch is not None

    if analysis.value_mismatches:
        # Render the mismatches as a human-readable list so failure
        # output points straight at the regressed references.  The
        # ``value_mismatches`` items are dicts with keys ``reference``,
        # ``schematic_value``, ``pcb_value`` (see
        # ``kicad_tools.sync.reconciler.Reconciler.analyze``).
        rendered = "\n".join(
            f"  - {m.get('reference', '?')}: "
            f"schematic={m.get('schematic_value', '?')!r}, "
            f"pcb={m.get('pcb_value', '?')!r}"
            for m in analysis.value_mismatches
        )
        pytest.fail(
            f"Board 05 fresh build has {len(analysis.value_mismatches)} "
            f"schematic<->PCB value mismatch(es); issue #3210 should keep "
            f"this at zero:\n{rendered}\n"
            f"See tests/test_board_05_drift_regression.py docstring for "
            f"the four pre-PR-3210 regression categories."
        )
