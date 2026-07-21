"""Cross-stage manufacturer-profile consistency regression guard (issue #3920).

Background -- the "split-brain"
--------------------------------
Boards 03 (USB joystick) and 04 (STM32 devboard) route with
``kct route --manufacturer jlcpcb-tier1`` (JLCPCB Tier 1 permits the
in-pad / micro-vias these boards deliberately place). But their
``project.kct`` used to declare ``target_fab: jlcpcb`` (the base tier,
which forbids via-in-pad), and ``kct build`` only consulted
``target_fab`` in its *export* step -- every other step
(route/verify/stitch) used ``BuildContext.mfr`` which was initialised
from the CLI default ``"jlcpcb"``.

The result was that the *same committed PCB* scored differently at each
surface: the recipe saw 0 violations (tier1), the build verify step saw
4 ``via_in_pad`` errors (base tier), and the export preflight / board.json
saw 5. This test pins the two halves of the fix so the disagreement
cannot silently return:

* **Part 1 (data):** ``project.kct target_fab`` now declares
  ``jlcpcb-tier1`` for boards 03 and 04, matching the tier the recipes
  route against.
* **Part 2 (pipeline):** ``kct build`` resolves the manufacturer once
  (``build_cmd._resolve_effective_mfr``) and threads it through
  ``BuildContext.mfr`` so route/verify/stitch/export all agree.

What this test asserts
----------------------
1. ``_resolve_effective_mfr`` precedence: explicit ``--mfr`` wins; else
   the spec's ``target_fab``; else the ``"jlcpcb"`` default.
2. For each board that declares ``target_fab``, ``kct check`` against the
   committed routed PCB at that profile reports **0 blocking DRC errors**.
3. Negative control: board 03's committed PCB *does* report ``via_in_pad``
   errors at the base ``jlcpcb`` tier -- proving the two tiers genuinely
   disagree and that the ``target_fab`` declaration is load-bearing, not
   vacuous. (This is the "reproduce the 0-vs-4 disagreement" half of the
   issue's test plan.)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kicad_tools.cli.build_cmd import _resolve_effective_mfr
from kicad_tools.spec import load_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOLERANCE_YAML = REPO_ROOT / ".github" / "routed-drc-tolerance.yml"

# Boards whose recipe routes at an explicit non-default fab tier and whose
# committed routed artifact must therefore verify clean at that same tier.
_BOARDS = {
    "03-usb-joystick": "output/usb_joystick_routed.kicad_pcb",
    "04-stm32-devboard": "output/stm32_devboard_routed.kicad_pcb",
}


def _project_kct(board: str) -> Path:
    return REPO_ROOT / "boards" / board / "project.kct"


def _routed_pcb(board: str) -> Path:
    return REPO_ROOT / "boards" / board / _BOARDS[board]


def _pcb_rel(board: str) -> str:
    return f"boards/{board}/{_BOARDS[board]}"


def _tolerance_doc() -> dict:
    return yaml.safe_load(_TOLERANCE_YAML.read_text())


def _tolerance_floor(board: str) -> int:
    """Max blocking errors the CI gate tolerates for this board (default 0)."""
    doc = _tolerance_doc()
    return int(doc.get("tolerances", {}).get(_pcb_rel(board), 0))


def _tolerance_mfr(board: str) -> str | None:
    """Manufacturer profile the CI gate uses for this board (default None)."""
    doc = _tolerance_doc()
    return doc.get("manufacturers", {}).get(_pcb_rel(board))


def _target_fab(board: str) -> str:
    spec = load_spec(_project_kct(board))
    assert spec.requirements is not None
    assert spec.requirements.manufacturing is not None
    fab = spec.requirements.manufacturing.target_fab
    assert fab, f"{board}/project.kct declares no manufacturing.target_fab"
    return fab


def _run_check(pcb: Path, mfr: str | None, cwd: Path | None = None) -> dict:
    """Run ``kct check --format json`` and return the parsed report.

    When ``mfr`` is ``None`` the ``--mfr`` flag is omitted entirely, so the
    CLI exercises its auto-resolution precedence (sidecar → project.kct →
    default). This routes through the real top-level dispatcher
    (``validation.run_check_command``), which is the exact surface issue
    #3920's precedence-forwarding regression lived on.
    """
    argv = [sys.executable, "-m", "kicad_tools.cli", "check", str(pcb)]
    if mfr is not None:
        argv += ["--mfr", mfr]
    argv += ["--format", "json"]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else REPO_ROOT,
    )
    assert proc.stdout, (
        f"kct check produced no stdout for {pcb.name} @ {mfr}.\nstderr:\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------
# Part 2 unit coverage: the single-source-of-truth resolver.
# --------------------------------------------------------------------------


class _MockManufacturing:
    def __init__(self, target_fab: str | None) -> None:
        self.target_fab = target_fab


class _MockRequirements:
    def __init__(self, target_fab: str | None) -> None:
        self.manufacturing = _MockManufacturing(target_fab)


class _MockSpec:
    def __init__(self, target_fab: str | None) -> None:
        self.requirements = _MockRequirements(target_fab)


def test_resolve_explicit_mfr_wins_over_spec() -> None:
    """An explicit ``--mfr`` flag overrides the spec's target_fab."""
    spec = _MockSpec(target_fab="jlcpcb-tier1")
    assert _resolve_effective_mfr("jlcpcb", spec) == "jlcpcb"  # type: ignore[arg-type]


def test_resolve_spec_target_fab_when_no_flag() -> None:
    """With no ``--mfr`` flag (None), the spec's target_fab is used.

    This is the crux of #3920: before the fix, route/verify/stitch fell
    back to the CLI default ``"jlcpcb"`` and ignored target_fab entirely.
    """
    spec = _MockSpec(target_fab="jlcpcb-tier1")
    assert _resolve_effective_mfr(None, spec) == "jlcpcb-tier1"  # type: ignore[arg-type]


def test_resolve_default_when_no_flag_and_no_target_fab() -> None:
    """Falls back to the historical ``"jlcpcb"`` default when nothing else set."""
    assert _resolve_effective_mfr(None, None) == "jlcpcb"
    assert _resolve_effective_mfr(None, _MockSpec(target_fab=None)) == "jlcpcb"  # type: ignore[arg-type]


def test_resolve_real_spec_board_03() -> None:
    """The real board-03 spec resolves to its declared tier with no flag."""
    spec = load_spec(_project_kct("03-usb-joystick"))
    assert _resolve_effective_mfr(None, spec) == "jlcpcb-tier1"


# --------------------------------------------------------------------------
# Part 1 data coverage: the declaration matches the routed tier.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("board", sorted(_BOARDS))
def test_project_kct_declares_tier1(board: str) -> None:
    """Boards 03/04 declare jlcpcb-tier1 (the tier their recipes route at)."""
    assert _target_fab(board) == "jlcpcb-tier1", (
        f"{board}/project.kct target_fab must match the tier its "
        f"generate_design.py routes against (jlcpcb-tier1). If the board "
        f"was retargeted, update the recipe --manufacturer arg and "
        f".github/routed-drc-tolerance.yml in lockstep."
    )


@pytest.mark.parametrize("board", sorted(_BOARDS))
def test_declarations_agree_across_files(board: str) -> None:
    """The two profile declaration files must name the same manufacturer.

    ``project.kct target_fab`` (consumed by ``kct build``) and the
    ``manufacturers:`` block of ``.github/routed-drc-tolerance.yml``
    (consumed by the CI gate) are the two surfaces that declare which
    profile a board is judged against. Issue #3920's whole point is a
    single source of truth -- if these two disagree, the split-brain is
    back on a different pair of surfaces.
    """
    spec_fab = _target_fab(board)
    ci_fab = _tolerance_mfr(board)
    assert ci_fab == spec_fab, (
        f"{board}: project.kct target_fab={spec_fab!r} disagrees with the "
        f".github/routed-drc-tolerance.yml manufacturers: entry {ci_fab!r}. "
        f"These must be kept in lockstep so kct build and the CI DRC gate "
        f"judge the same copper against the same fab tier (issue #3920)."
    )


# --------------------------------------------------------------------------
# Cross-stage agreement: committed PCB verifies clean at the declared tier.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("board", sorted(_BOARDS))
def test_committed_pcb_within_gate_at_declared_tier(board: str) -> None:
    """``kct check`` at ``project.kct target_fab`` passes the CI DRC gate.

    Core acceptance criterion: the profile the board declares is the
    profile under which its committed copper meets the CI gate, so every
    ``kct build`` stage (which now sources ``ctx.mfr`` from ``target_fab``)
    agrees with the gate.

    The bar is the ``tolerances:`` floor from
    ``.github/routed-drc-tolerance.yml`` -- 0 for board 03 (strict clean),
    2 for board 04 (two documented pre-existing sub-0.5mm drill pairs,
    a layout issue tracked separately in #3847 and *out of scope* for the
    profile-consistency fix). Anchoring to the same floor the CI gate uses
    keeps this test honest about what "consistent" means without pretending
    the drill residual is a profile bug.
    """
    pcb = _routed_pcb(board)
    if not pcb.exists():
        pytest.skip(f"committed routed PCB not present: {pcb}")

    fab = _target_fab(board)
    floor = _tolerance_floor(board)
    report = _run_check(pcb, fab)

    assert report["manufacturer"] == fab
    errors = report["summary"]["errors"]
    error_rules = [v["rule_id"] for v in report["violations"] if v["severity"] == "error"]
    assert errors <= floor, (
        f"{board} committed PCB reports {errors} blocking DRC error(s) at "
        f"its declared tier {fab!r}, exceeding the CI gate floor of {floor}. "
        f"Either the copper regressed or target_fab drifted out of "
        f"agreement with the routed artifact. Error rules: {error_rules}"
    )


def test_board_03_reproduces_split_brain_at_base_tier() -> None:
    """Negative control: board-03 PCB is NOT clean at the base jlcpcb tier.

    Reproduces the original 0-vs-4 disagreement (issue #3920): the same
    committed copper that is clean at jlcpcb-tier1 reports ``via_in_pad``
    errors at the base ``jlcpcb`` tier, because that tier forbids the
    in-pad vias on the USB-C F.Cu/GND pads. This proves the target_fab
    declaration is load-bearing -- if this test ever passes with 0 errors,
    the two tiers no longer differ on this board and the guard above is
    vacuous.
    """
    pcb = _routed_pcb("03-usb-joystick")
    if not pcb.exists():
        pytest.skip(f"committed routed PCB not present: {pcb}")

    report = _run_check(pcb, "jlcpcb")
    error_rules = {v["rule_id"] for v in report["violations"] if v["severity"] == "error"}
    assert report["summary"]["errors"] > 0
    assert "via_in_pad" in error_rules, (
        "Expected via_in_pad errors at the base jlcpcb tier (the "
        "split-brain the fix eliminates at tier1). If the board's copper "
        "changed so it no longer uses in-pad vias, this negative control "
        "and the issue-3920 rationale need revisiting."
    )


def test_explicit_base_tier_overrides_sidecar_via_real_cli(tmp_path: Path) -> None:
    """Explicit ``--mfr jlcpcb`` beats a higher sidecar AND project tier (#3920).

    Real-CLI (subprocess) negative control for the dispatcher-forwarding
    regression the judge caught on PR #4422. The in-process resolver test
    (``test_resolve_explicit_mfr_wins_over_spec``) exercises
    ``build_cmd._resolve_effective_mfr`` directly and therefore *bypasses*
    ``validation.run_check_command`` -- the top-level ``check`` subparser
    dispatcher that forwards ``--mfr`` into ``check_cmd.main``. That dispatcher
    is exactly where the bug lived: its ``--mfr`` default was ``"jlcpcb"`` and
    it only forwarded ``if args.mfr != "jlcpcb"``, so an explicit
    ``--mfr jlcpcb`` was silently dropped and ``check_main`` auto-resolved from
    the sidecar / ``project.kct`` to a higher tier -- violating the PR's own
    "explicit --mfr always wins" precedence rule 1.

    This test pins the whole CLI path end-to-end: it stages board-03's copper
    with a ``fab_profile.json`` sidecar declaring the higher ``jlcpcb-tier1``
    profile, then proves that

    * omitting ``--mfr`` auto-resolves to the sidecar tier (clean, tier1), and
    * an explicit ``--mfr jlcpcb`` overrides both the sidecar and the copy's
      residual ``project.kct`` and forces base-tier evaluation (via_in_pad).

    If the dispatcher ever drops an explicit base-tier flag again, the second
    assertion fails with ``manufacturer == 'jlcpcb-tier1'`` / ``errors == 0``.
    """
    src_pcb = _routed_pcb("03-usb-joystick")
    if not src_pcb.exists():
        pytest.skip(f"committed routed PCB not present: {src_pcb}")

    # Stage the copper in an isolated dir with a tier1 fab-profile sidecar so
    # the auto-resolution precedence (sidecar → project.kct → default) has a
    # concrete, higher-than-base tier to resolve to.
    staged_pcb = tmp_path / "usb_joystick_routed.kicad_pcb"
    staged_pcb.write_bytes(src_pcb.read_bytes())
    (tmp_path / "fab_profile.json").write_text(json.dumps({"mfr": "jlcpcb-tier1"}))

    # No flag: the sidecar wins → clean at tier1.
    auto = _run_check(staged_pcb, None, cwd=tmp_path)
    assert auto["manufacturer"] == "jlcpcb-tier1", (
        "Sanity: with no --mfr flag the fab_profile.json sidecar should "
        f"auto-resolve to jlcpcb-tier1, got {auto['manufacturer']!r}."
    )
    assert auto["summary"]["errors"] == 0

    # Explicit base tier: must override the sidecar via the real dispatcher.
    forced = _run_check(staged_pcb, "jlcpcb", cwd=tmp_path)
    assert forced["manufacturer"] == "jlcpcb", (
        "Explicit --mfr jlcpcb was dropped by the CLI dispatcher: check "
        f"judged at {forced['manufacturer']!r} instead of the forced base "
        "tier. This is the issue-3920 precedence regression -- "
        "validation.run_check_command must forward an explicit --mfr "
        "regardless of value (default None, forward when `is not None`)."
    )
    forced_error_rules = {v["rule_id"] for v in forced["violations"] if v["severity"] == "error"}
    assert forced["summary"]["errors"] > 0
    assert "via_in_pad" in forced_error_rules, (
        "Expected via_in_pad errors once the explicit base tier is honored; "
        f"got error rules {sorted(forced_error_rules)}."
    )


if __name__ == "__main__":  # pragma: no cover -- manual debugging convenience.
    pytest.main([__file__, "-v"])
