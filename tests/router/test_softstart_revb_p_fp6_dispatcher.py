"""Direct verification of P_FP6 SOP in-pad rescue on softstart rev B (Issue #3390).

This is the **fast smoke equivalent** of the heavyweight consumer test
``test_softstart_revb_fine_pitch_escape.py``.  It bypasses the slow
end-to-end ``route_with_escape`` pipeline and directly verifies:

  1. The fine-pitch region detector installs regions for the three
     UCC27211/LM393 SOIC-8 packages (U5, U6, U7) and the U1 LQFP-32.
  2. The P_FP6 SOP in-pad rescue dispatcher
     (:meth:`EscapeRouter._sop_in_pad_rescue_eligible`) returns True
     for each UCC27211 SOIC-8 -- the geometry, manufacturer
     capability, fine-pitch region, and long-axis headroom gates all
     pass at jlcpcb-tier1 with 0.30 mm trace + 0.20 mm clearance.
  3. When dispatched directly the SOP staggered escape produces
     in-pad vias on all 8 pins of each UCC27211 (positive empirical
     evidence that the rescue *would* lift these nets if the main
     ``route_with_escape`` dispatcher were to call it).

Critically, this test also documents the **dispatcher gap** found
during #3390 verification and re-confirmed by #3395 measurement:
the SOIC-8 1.27 mm-pitch packages do not pass
:func:`kicad_tools.router.escape.is_dense_package` at the recipe
parameters above (dynamic threshold = 2 * (0.30 + 0.20) = 1.0 mm <
1.27 mm pitch), so ``Autorouter.detect_dense_packages`` excludes
them from the escape pre-pass.  P_FP6 wires the rescue path
correctly but the dispatcher never invokes it on this fixture.

Issue #3395 investigated raising the dispatcher gate and found
that **opening the gate REGRESSES softstart rev B reach 18 -> 8**
at L=2 single-attempt (the SOP rescue's in-pad vias collide with
GATE/UCC bus routing downstream).  The dispatcher gap is therefore
INTENTIONAL today, pending the P_FP6 rescue ↔ main-router
interaction fix tracked in #3398.  See
``test_softstart_revb_dispatcher_gap_documents_p_fp6_unreached``
below for the empirical detail.

Runtime: <10 s.  Not gated on ``KICAD_RUN_SLOW_SOFTSTART_REACH=1``.

To run locally::

    uv run pytest tests/router/test_softstart_revb_p_fp6_dispatcher.py -v --no-cov

Issue: https://github.com/rjwalters/kicad-tools/issues/3390
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_DIR = REPO_ROOT / "boards" / "external" / "softstart"


def _regenerate_softstart_pcb(output_dir: Path) -> Path:
    """Regenerate softstart rev B PCB on demand (schematic + PCB only)."""
    sys.path.insert(0, str(BOARD_DIR))
    try:
        import generate_design  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    output_dir.mkdir(parents=True, exist_ok=True)
    generate_design.create_project(output_dir, "softstart")
    generate_design.create_softstart_schematic(output_dir)
    pcb_path = generate_design.create_softstart_pcb(output_dir)
    return pcb_path


def _load_router(pcb_path: Path):
    from kicad_tools.router import DesignRules, load_pcb_for_routing

    rules = DesignRules(
        trace_width=0.30,
        trace_clearance=0.20,
        via_diameter=0.6,
        via_drill=0.3,
        min_trace_width=0.127,
        manufacturer="jlcpcb-tier1",
    )

    router, _ = load_pcb_for_routing(
        str(pcb_path),
        rules=rules,
        skip_nets=[
            "AC_LINE", "AC_NEUTRAL", "FUSED_LINE", "GND",
            "+3.3V", "VRECT",
            "SCAP_POS+", "SCAP_POS_GND", "SCAP_NEG+", "SCAP_NEG_GND",
            "ISENSE_POS",
        ],
    )
    # ``load_pcb_for_routing`` does not carry the manufacturer through
    # to the rules object today (mirroring the U1 LQFP test fixture).
    router.rules.manufacturer = "jlcpcb-tier1"
    return router


# UCC27211 SOIC-8 packages on softstart rev B.  U5 = positive bus half,
# U6 = negative bus half, U7 = LM393 comparator (also SOIC-8).
UCC_REFS = ("U5", "U6", "U7")


def test_softstart_revb_fine_pitch_regions_install_for_soic8(tmp_path: Path) -> None:
    """``load_pcb_for_routing`` installs fine-pitch regions for the SOIC-8s.

    Issue #3390 AC #2 sub-check: this is the precondition for the
    P_FP6 dispatcher's ``_escape_clearance_for_ref`` gate.  Without
    a region match the SOP rescue eligibility check returns False
    even when manufacturer + pitch + long-axis gates would otherwise
    pass.
    """
    pcb_path = _regenerate_softstart_pcb(tmp_path / "softstart_fp_regions")
    router = _load_router(pcb_path)

    regions = router.grid.get_fine_pitch_regions()
    assert regions, "Expected fine-pitch regions to be installed"
    region_refs = {r.package_ref for r in regions}

    for ref in UCC_REFS:
        assert ref in region_refs, (
            f"Expected fine-pitch region for {ref}, got: {sorted(region_refs)}"
        )


def test_softstart_revb_p_fp6_dispatcher_eligible(tmp_path: Path) -> None:
    """P_FP6 SOP rescue gate returns True for UCC27211/LM393 SOIC-8 packages.

    Issue #3390 AC #3 sub-check: the four-gate eligibility check
    (manufacturer capability, pitch band, region installed, long-axis
    headroom) must pass for the rescue to fire when the dispatcher
    invokes it.  This is a direct unit-level assertion -- if the SOP
    rescue dispatcher gap is ever closed (so
    ``detect_dense_packages`` includes SOIC-8 at 1.27 mm pitch + tight
    clearance), the rescue WILL fire on these packages.
    """
    from kicad_tools.router.escape import EscapeRouter

    pcb_path = _regenerate_softstart_pcb(tmp_path / "softstart_p_fp6_eligible")
    router = _load_router(pcb_path)

    er = EscapeRouter(router.grid, router.rules)
    assert er.via_in_pad_supported, (
        "Expected via_in_pad_supported=True at jlcpcb-tier1; "
        "the rescue would never fire otherwise."
    )

    for ref in UCC_REFS:
        pads = [p for p in router.pads.values() if p.ref == ref]
        assert pads, f"Expected pads for {ref}"
        package_info = er.analyze_package(pads)
        eligible = er._sop_in_pad_rescue_eligible(package_info, pads)
        assert eligible, (
            f"{ref} ({package_info.package_type.name}, "
            f"pitch={package_info.pin_pitch:.3f} mm) failed the P_FP6 "
            f"rescue gate at jlcpcb-tier1.  "
            f"via_in_pad_supported={er.via_in_pad_supported}, "
            f"per_ref_clearance={er._escape_clearance_for_ref(ref, pads):.3f}, "
            f"trace_clearance={er.rules.trace_clearance:.3f}, "
            f"pad long_axis={max(pads[0].width, pads[0].height):.3f}"
        )


def test_softstart_revb_p_fp6_dispatcher_emits_in_pad_vias(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Dispatch is defer-all by default; env cap=1 rescues pin 8 only.

    Issue #3390 AC #4 sub-check, REVISED by Issue #3398: the UCC27211
    SOIC-8 at 1.27 mm pitch sits in the rescue-only band (pitch above
    the 1.0 mm dynamic threshold), so the SOP staggered dispatcher is
    "rescue or nothing" and the rescue is consumer-aware:

    - PRODUCTION DEFAULT (``KICAD_TOOLS_SOP_RESCUE_ROW_CAP`` unset =
      cap 0): every pad defers and the packages emit NO escape
      geometry at all -- the Jun 9 2026 same-machine A/B measurements
      showed every rescue-firing configuration is net-negative on
      softstart under production budgets (L=2: 17/30 main vs
      15-16/30; L=4 floor test: 22/30 main vs 20/30).
    - With ``KICAD_TOOLS_SOP_RESCUE_ROW_CAP=1`` (experiment mode):
      pads whose net's nearest off-package consumer is LOCAL
      (<= 15 mm: bootstrap caps, TVS clamps, gate resistors) defer;
      among the FAR-consumer pads of a row (U5/U6 pins 7-8:
      GATE_*_A/B driver inputs, 20-54 mm from the MCU) only the
      FARTHEST one (pin 8: GATE_*_A) wins the row's single rescue.
      Rescuing pins 7+8 together walled the row's B.Cu launch
      corridor and turned the pin-7 nets into ``blocked_path``
      failures (the original 18 -> 8/30 regression of #3395).
    - U7 (LM393) has only local consumers (2-12 mm) -> zero rescues
      at any cap.
    """
    from kicad_tools.router.escape import EscapeRouter

    pcb_path = _regenerate_softstart_pcb(tmp_path / "softstart_p_fp6_emit")
    router = _load_router(pcb_path)

    er = EscapeRouter(
        router.grid,
        router.rules,
        net_target_positions=router._build_net_target_positions(),
    )
    caplog.set_level(logging.INFO, logger="kicad_tools.router.escape")

    # --- Phase 1: production default (cap 0) -> NO geometry at all. ---
    clean_env = {
        k: v for k, v in os.environ.items()
        if k != "KICAD_TOOLS_SOP_RESCUE_ROW_CAP"
    }
    with mock.patch.dict(os.environ, clean_env, clear=True):
        for ref in UCC_REFS:
            pads = [p for p in router.pads.values() if p.ref == ref]
            package_info = er.analyze_package(pads)
            routes = er.generate_escapes(package_info)
            assert routes == [], (
                f"{ref}: production default (row cap 0) must emit NO "
                f"escape geometry on the rescue-only band; got "
                f"{len(routes)} routes"
            )

    # --- Phase 2: experiment mode (cap 1) -> pin 8 per row only. ---
    # Expected far-consumer rescue pins per ref (Issue #3398 diag +
    # per-row cap: pin 8's consumer is farther than pin 7's, so pin 8
    # wins each row's single rescue).
    expected_rescued = {
        "U5": {"8"},   # GATE_POS_A -> MCU, 53.8 mm (beats pin 7 @ 52.5)
        "U6": {"8"},   # GATE_NEG_A -> MCU, 21.8 mm (beats pin 7 @ 20.5)
        "U7": set(),   # all consumers local (2-12 mm)
    }

    with mock.patch.dict(
        os.environ, {"KICAD_TOOLS_SOP_RESCUE_ROW_CAP": "1"},
    ):
        for ref in UCC_REFS:
            pads = [p for p in router.pads.values() if p.ref == ref]
            package_info = er.analyze_package(pads)
            routes = er.generate_escapes(package_info)
            in_pad_pins = {
                r.pad.pin
                for r in routes
                if r.via is not None and getattr(r.via, "in_pad", False)
            }
            assert in_pad_pins == expected_rescued[ref], (
                f"{ref}: expected in-pad rescues exactly on far-consumer "
                f"pins {sorted(expected_rescued[ref])}, got "
                f"{sorted(in_pad_pins)}"
            )
            # Rescue-only band: no staggered fallback geometry may appear.
            staggered = [
                r for r in routes
                if r.via is not None and not getattr(r.via, "in_pad", False)
            ]
            assert not staggered, (
                f"{ref}: rescue-only-band package emitted "
                f"{len(staggered)} legacy staggered escapes (Issue #3398 "
                "requires rescue-or-nothing)"
            )

    # Sanity check on log output -- the far-consumer rescues log the
    # ``SOP in-pad rescue`` diagnostic; U5 and U6 must each appear
    # exactly once (cap=1 phase only; the cap-0 phase logs nothing).
    rescue_lines = [
        rec.getMessage() for rec in caplog.records
        if "SOP in-pad rescue" in rec.getMessage()
    ]
    assert len(rescue_lines) == 2, (
        f"Expected exactly 2 rescue log lines (U5 pin 8 + U6 pin 8), "
        f"got {len(rescue_lines)}: {rescue_lines}"
    )
    for ref in ("U5", "U6"):
        matching = [l for l in rescue_lines if f" {ref} " in l]
        assert len(matching) == 1, (
            f"Expected 1 SOP in-pad rescue log for {ref} (per-row cap); "
            f"all rescue lines: {rescue_lines}"
        )


def test_softstart_revb_dispatcher_gate_open_for_soic8(
    tmp_path: Path,
) -> None:
    """The #3398 SOIC-8 band classifies UCC27211/LM393 as dense.

    History: #3390/#3395 documented the original dispatcher GAP
    (UCC27211 at 1.27 mm pitch > dynamic threshold 1.0 mm -> not
    dense -> P_FP6 unreachable end-to-end).  Naively opening the gate
    regressed softstart rev B reach 18 -> 8/30 at L=2 because the
    full-row rescue's 19-via field blocked the
    GATE_POS/GATE_NEG/UCC_HO/UCC_LO/VGATE bus + snubber routing
    around the FET pairs.

    Issue #3398 closed the gap properly: the SOIC-8-class band in
    :func:`kicad_tools.router.escape.is_dense_package` admits these
    packages to the escape pre-pass, and the rescue-only-band
    consumer-aware deferral in ``_create_staggered_row_escapes``
    ensures they emit escape geometry ONLY for each row's single
    farthest-consumer pad (see
    ``test_softstart_revb_p_fp6_dispatcher_emits_in_pad_vias``).
    This test is the flipped positive-assertion the original
    negative-control (``..._dispatcher_gap_documents_p_fp6_unreached``)
    promised once #3398 landed.
    """
    from kicad_tools.router.escape import is_dense_package

    pcb_path = _regenerate_softstart_pcb(tmp_path / "softstart_dispatcher_gap")
    router = _load_router(pcb_path)

    for ref in UCC_REFS:
        pads = [p for p in router.pads.values() if p.ref == ref]
        assert pads, f"Expected pads for {ref}"
        dense = is_dense_package(
            pads,
            trace_width=router.rules.trace_width,
            clearance=router.rules.trace_clearance,
        )
        # Issue #3398: the SOIC-8 band (all-SMD dual-row, >= 8 pads,
        # pitch in (0.75, 1.5] mm) now admits UCC27211/LM393 so the
        # consumer-aware P_FP6 rescue can fire end-to-end.
        assert dense, (
            f"{ref} is no longer classified as dense at trace=0.30, "
            "clearance=0.20.  The #3398 SOIC-8-class band in "
            "is_dense_package appears to have regressed; without it "
            "the P_FP6 SOP rescue is unreachable end-to-end and the "
            "GATE_*_A/B reach contribution on softstart rev B is lost."
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s", "--no-cov"]))
