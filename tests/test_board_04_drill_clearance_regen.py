"""Regression guard: board-04 fresh regen is jlcpcb-tier1 hole-to-hole clean.

Issue #4408.  Board-04's LQFP-48 west escape routes three in-pad micro-vias
(OSC_OUT / NRST / GND) stacked at the 0.5 mm pin pitch, leaving two 0.350 mm
drill pairs below the jlcpcb-tier1 0.500 mm ``min_hole_to_hole_mm`` floor.  #4017
fixed this ARTIFACT-ONLY (a hand nudge that was deliberately not back-ported),
so every fresh regen reintroduced 2 ``hole_to_hole_clearance`` errors and the
committed board diverged from the recipe.

#4408 back-ports the fix as a generic, ``--mfr``-driven post-route pass
(``relocate_drill_clearance_step`` in the recipe, wired between
``tie_power_pads`` and ``quantize_escapes``).  Three guards:

1. A fast source-pin (no routing) that the recipe still wires the step -- so a
   future recipe cleanup that drops it fails at PR time rather than silently
   regressing to artifact-only.
2. A fast pin on the reviewed paid-drill process's override set (see below), so
   the ``@slow`` test's exemption list cannot silently grow.
3. A ``@slow`` end-to-end regen that runs ``generate_design.py`` and asserts the
   FRESH routed board is clean against **the process board 04 actually ships**.

Why the regen is NOT checked at strict-0 against *stock* ``jlcpcb-tier1``
(Issue #5852)
-----------------------------------------------------------------------
Board 04 ships a **reviewed paid 0.15 mm mechanical through-drill option**
(issue #5009): ``boards/04-stm32-devboard/manufacturing_process.py`` declares
``via_type="through"`` with 0.15 mm drill / 0.30 mm diameter / 0.075 mm annular
floors, pins the board's physical fingerprint, writes those floors into the
board's native ``.kicad_dru`` + ``.kicad_pro``
(``apply_native_floors``), and refuses -- ``validate_process`` raises
``"Only mechanical through vias are supported"`` -- any via tagged
``micro``/``blind``/``buried``.  ``_repair_staged`` deliberately *strips* the
``micro`` token from the ``tie_power_pads`` in-pad ties for exactly that reason,
because the option is mechanical drilling, not laser microvias.

Stock ``--mfr jlcpcb-tier1`` cannot express that paid option, so it reports
every one of those vias against its own 0.30/0.60/0.15 floors:
``dimension_via_drill`` / ``dimension_via_diameter`` /
``dimension_annular_ring``, 24 findings on a 2026-10-01 regen, every
``actual_value`` sitting exactly ON the reviewed floor (0.15 / 0.30 / 0.075).
The committed artifact has always reported the same family (see
``docs/research/kicad-routing-tools-comparison.md``), so an unqualified
``assert not blocking`` here could never have passed -- it only went unnoticed
because this module's end-to-end test is ``@pytest.mark.slow`` and runs
nightly, never at PR time.

``scripts/ci/check_routed_drc.py`` -- the CI fresh-regen gate this module
mirrors -- already special-cases board 04 for this reason
(``PAID_DRILL_BOARD`` / ``_run_paid_drill_check``: *"Stock tier1 alone cannot
express that process.  Never turn this into an error-count allowance."*).  It
does not run stock ``kct check`` on board 04 at all; it runs the board's own
reviewed validator, ``check_manufacturing.py``.  So the test below does both,
and nothing is grandfathered by rule name alone:

* stock ``jlcpcb-tier1`` must be at **strict 0** for every rule family *except*
  the three the reviewed process overrides -- so a new rule family, or a
  ``hole_to_hole_clearance`` regression (#4408's actual acceptance criterion),
  still fails here;
* each excused dimension finding is **re-measured against the reviewed floors**
  read from ``manufacturing_process.process_rules()``, so a via that drops below
  the *paid* process's own 0.15/0.30/0.075 floor fails rather than hiding behind
  the rule name;
* the reviewed validator's DRC leg must report **0 errors and 0 warnings** --
  the same verdict CI gates on.
"""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_04_DIR = REPO_ROOT / "boards" / "04-stm32-devboard"
BOARD_04_RECIPE = BOARD_04_DIR / "generate_design.py"
BOARD_04_PROCESS = BOARD_04_DIR / "manufacturing_process.py"
BOARD_04_REVIEWED_CHECK = BOARD_04_DIR / "check_manufacturing.py"

# The three stock-profile rules the reviewed paid-drill option overrides, mapped
# to the ``DesignRules`` field that carries the reviewed floor.  Nothing else is
# excused, and each excused finding is still measured against that floor.
_PAID_DRILL_RULE_FLOORS = {
    "dimension_via_drill": "min_via_drill_mm",
    "dimension_via_diameter": "min_via_diameter_mm",
    "dimension_annular_ring": "min_annular_ring_mm",
}

# ``kct check`` compares with this slack (validate.rules.dimensions.DRC_TOLERANCE),
# so a finding may sit a hair under the reviewed floor without being a regression.
_FLOOR_TOLERANCE_MM = 1e-6


def _reviewed_design_rules():
    """The reviewed paid-drill ``DesignRules``, from the recipe's own source."""
    return runpy.run_path(str(BOARD_04_PROCESS))["process_rules"]()


def test_recipe_wires_hole_to_hole_relocation_step() -> None:
    """Fast source-pin: the recipe must invoke the hole-to-hole relocation pass.

    Pins both the step function and its call site so a future edit that removes
    either (silently reverting to the artifact-only #4017 state) trips here.
    """
    source = BOARD_04_RECIPE.read_text()

    assert "def relocate_drill_clearance_step(" in source, (
        "board-04 recipe no longer defines relocate_drill_clearance_step -- the "
        "generic #4408 hole-to-hole relocation pass.  Without it the fresh regen "
        "reintroduces the 2 grandfathered 0.350mm drill pairs and diverges from "
        "the committed artifact (the #4017 artifact-only state)."
    )
    assert "relocate_drill_clearance_step(routed_path)" in source, (
        "board-04 recipe defines relocate_drill_clearance_step but no longer "
        "CALLS it in the post-route pipeline.  Re-wire it between tie_power_pads "
        "and quantize_escapes (issue #4408)."
    )
    # It must import the library engine (the shared clearance-safe pass), not a
    # board-specific hack.
    assert (
        "from kicad_tools.drc.relocate_drill_clearance import relocate_drill_clearance" in source
    ), (
        "board-04 recipe no longer imports the library relocate_drill_clearance "
        "engine -- the fix must live in the library, not a board-local hack "
        "(issue #4408)."
    )


def test_reviewed_process_overrides_exactly_the_three_via_dimension_floors() -> None:
    """Fast pin: the paid-drill option relaxes ONLY the via-dimension floors.

    The ``@slow`` regen below excuses ``dimension_via_drill`` /
    ``dimension_via_diameter`` / ``dimension_annular_ring`` under stock
    ``jlcpcb-tier1`` because the reviewed paid 0.15 mm mechanical-drill process
    overrides exactly those floors (Issue #5852 / #5009).  If a future edit
    widens ``manufacturing_process.process_rules()`` to relax anything else,
    that excuse silently stops being justified -- so it fails HERE, at PR time,
    rather than quietly expanding the nightly test's blind spot.

    ``via_in_pad_supported`` is the one non-floor override: the reviewed option
    is explicitly NOT a via-in-pad process, which is a *tightening*, so it is
    expected rather than excused.
    """
    from dataclasses import fields

    from kicad_tools.manufacturers import get_profile

    stock = get_profile("jlcpcb-tier1").get_design_rules(2)
    reviewed = _reviewed_design_rules()

    differing = {
        f.name for f in fields(reviewed) if getattr(reviewed, f.name) != getattr(stock, f.name)
    }
    assert differing == set(_PAID_DRILL_RULE_FLOORS.values()) | {"via_in_pad_supported"}, (
        "boards/04-stm32-devboard/manufacturing_process.py::process_rules() no "
        "longer overrides exactly the three via-dimension floors (+ the "
        "via_in_pad tightening).  test_fresh_regen_is_tier1_hole_to_hole_clean "
        "excuses stock-tier1 findings for those three rules only; update both "
        f"sides together (issue #5852).  Differing fields: {sorted(differing)}"
    )

    # The reviewed floors must stay BELOW stock tier1's -- i.e. a genuine paid
    # relaxation.  A reviewed floor at or above stock would make the excuse in
    # the slow test meaningless (nothing would be excused) or, worse, hide a
    # tightening regression.
    for rule_id, field_name in _PAID_DRILL_RULE_FLOORS.items():
        reviewed_floor = getattr(reviewed, field_name)
        stock_floor = getattr(stock, field_name)
        assert 0 < reviewed_floor < stock_floor, (
            f"{field_name} ({rule_id}) must be a positive paid relaxation below "
            f"stock tier1's {stock_floor}mm; got {reviewed_floor}mm (issue #5852)"
        )


@pytest.mark.slow
def test_fresh_regen_is_tier1_hole_to_hole_clean(tmp_path: Path) -> None:
    """End-to-end: a fresh ``generate_design.py`` run is manufacturable.

    This is the #4408 acceptance criterion: the GENERATOR (not a committed
    artifact) produces a clean board unattended.  Mirrors the CI fresh-regen
    gate (``scripts/ci/check_routed_drc.py``) -- which, for board 04
    specifically, means stock ``jlcpcb-tier1`` at strict 0 for every rule
    family the reviewed paid-drill process does *not* override, PLUS the
    board's own reviewed validator.  See this module's docstring for why stock
    tier1 alone is the wrong yardstick here (Issue #5852).
    """
    out_dir = tmp_path / "board04"
    out_dir.mkdir()

    # Regen may exit non-zero on a partial route under load (documented
    # host-vs-CI router divergence, #3822); the DRC assertion below is the real
    # gate, so tolerate a non-zero recipe exit as long as the routed PCB exists.
    proc = subprocess.run(
        [sys.executable, str(BOARD_04_RECIPE), str(out_dir)],
        capture_output=True,
        text=True,
        timeout=1200,
        check=False,
    )

    routed = out_dir / "stm32_devboard_routed.kicad_pcb"
    if not routed.exists():
        pytest.fail(
            "board-04 regen did not produce a routed PCB.\n"
            f"stdout tail:\n{proc.stdout[-2000:]}\n"
            f"stderr tail:\n{proc.stderr[-2000:]}"
        )

    check = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(routed),
            "--mfr",
            "jlcpcb-tier1",
            "--errors-only",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    brace = check.stdout.find("{")
    assert brace >= 0, f"kct check produced no JSON payload:\n{check.stdout}"
    payload = json.loads(check.stdout[brace:])

    violations = payload.get("violations", [])
    # Blocking (non-advisory) errors, counted the way the CI gate counts them.
    blocking = [
        v for v in violations if v.get("severity") == "error" and v.get("rule_id") != "connectivity"
    ]
    hole_to_hole = [v for v in blocking if v.get("rule_id") == "hole_to_hole_clearance"]

    assert not hole_to_hole, (
        "Fresh board-04 regen still has hole_to_hole_clearance errors -- the "
        "relocation pass did not relieve the LQFP-48 west escape stack:\n"
        + "\n".join(f"  - {v.get('message')} at {v.get('location')}" for v in hole_to_hole)
    )

    # --- Leg 1: stock tier1, strict 0 outside the reviewed process's overrides.
    paid_drill = [v for v in blocking if v.get("rule_id") in _PAID_DRILL_RULE_FLOORS]
    other = [v for v in blocking if v.get("rule_id") not in _PAID_DRILL_RULE_FLOORS]

    assert not other, (
        "Fresh board-04 regen has blocking jlcpcb-tier1 DRC errors outside the "
        "reviewed paid-drill via-dimension overrides:\n"
        + "\n".join(f"  - {v.get('rule_id')}: {v.get('message')}" for v in other)
    )

    # --- Leg 2: the excused findings are re-measured, not grandfathered.
    # Every stock-tier1 via-dimension finding must still satisfy the REVIEWED
    # paid floor, so a via that regresses below 0.15/0.30/0.075 fails here even
    # though its rule_id is on the excused list.
    reviewed = _reviewed_design_rules()
    undersized = [
        v
        for v in paid_drill
        if v.get("actual_value") is None
        or v["actual_value"]
        < getattr(reviewed, _PAID_DRILL_RULE_FLOORS[v["rule_id"]]) - _FLOOR_TOLERANCE_MM
    ]
    assert not undersized, (
        "Fresh board-04 regen has vias below the REVIEWED paid 0.15mm "
        "mechanical-drill floors, not just below stock tier1's (issue #5852):\n"
        + "\n".join(
            f"  - {v.get('rule_id')}: actual {v.get('actual_value')} < reviewed "
            f"{getattr(reviewed, _PAID_DRILL_RULE_FLOORS[v['rule_id']], None)} "
            f"at {v.get('location')}"
            for v in undersized
        )
    )

    # --- Leg 3: the board's own reviewed validator -- the verdict CI gates on.
    # ``check_manufacturing.py`` runs the full check suite with
    # ``process_rules()`` (stock tier1 + the paid via floors), so a regression in
    # ANY rule family -- including the three excused above -- fails here.  Only
    # the DRC leg is asserted: ``meta_checks.overall`` also folds in strict ERC
    # and bundle-manifest legs, which are this recipe's other gates' business,
    # not this test's.
    reviewed_report = out_dir / "reviewed-check.json"
    reviewed_run = subprocess.run(
        [sys.executable, str(BOARD_04_REVIEWED_CHECK), str(routed), str(reviewed_report)],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert reviewed_report.is_file(), (
        "board-04's reviewed paid-drill validator produced no report "
        f"(exit {reviewed_run.returncode}):\n{reviewed_run.stdout[-2000:]}\n"
        f"{reviewed_run.stderr[-2000:]}"
    )
    reviewed_payload = json.loads(reviewed_report.read_text())
    assert reviewed_payload["meta_checks"]["drc"]["status"] == "PASSED", (
        "Fresh board-04 regen fails its OWN reviewed paid-drill DRC gate "
        "(boards/04-stm32-devboard/check_manufacturing.py), the verdict "
        "scripts/ci/check_routed_drc.py gates on:\n"
        f"  drc: {reviewed_payload['meta_checks']['drc']}\n"
        f"  summary: {reviewed_payload['summary']}\n"
        + "\n".join(
            f"  - {v.get('rule_id')}: {v.get('message')}"
            for v in reviewed_payload.get("violations", [])
        )
    )
