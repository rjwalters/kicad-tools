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
during #3390 verification: the SOIC-8 1.27 mm-pitch packages do not
pass :func:`kicad_tools.router.escape.is_dense_package` at the recipe
parameters above (dynamic threshold = 2 * (0.30 + 0.20) = 1.0 mm <
1.27 mm pitch), so ``Autorouter.detect_dense_packages`` excludes
them from the escape pre-pass.  P_FP6 wires the rescue path
correctly but the dispatcher never invokes it on this fixture; the
+3 UCC27211 net reach lift estimated by the architect (#3381 comment)
is therefore unrealised in the empirical end-to-end run.  Closing
this gap is tracked separately (out of scope for #3390).

Runtime: <10 s.  Not gated on ``KICAD_RUN_SLOW_SOFTSTART_REACH=1``.

To run locally::

    uv run pytest tests/router/test_softstart_revb_p_fp6_dispatcher.py -v --no-cov

Issue: https://github.com/rjwalters/kicad-tools/issues/3390
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

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
    """Direct dispatch of the SOP escape generates in-pad vias on UCC27211.

    Issue #3390 AC #4 sub-check: when ``generate_escapes`` is called
    directly on the UCC27211 SOIC-8 package (bypassing the
    ``detect_dense_packages`` gate that excludes it from the
    end-to-end ``route_with_escape`` pipeline), the SOP staggered
    dispatcher produces an :class:`~kicad_tools.router.escape.EscapeRoute`
    with a via for every pin on every signal net.  This is the
    positive evidence that the P_FP6 wiring is correct -- the rescue
    fires, places in-pad vias, and logs the
    ``SOP in-pad rescue for ... (Issue #3381 / P_FP6)`` line.
    """
    from kicad_tools.router.escape import EscapeRouter

    pcb_path = _regenerate_softstart_pcb(tmp_path / "softstart_p_fp6_emit")
    router = _load_router(pcb_path)

    er = EscapeRouter(router.grid, router.rules)
    caplog.set_level(logging.INFO, logger="kicad_tools.router.escape")

    total_vias = 0
    total_pins = 0
    for ref in UCC_REFS:
        pads = [p for p in router.pads.values() if p.ref == ref]
        package_info = er.analyze_package(pads)
        routes = er.generate_escapes(package_info)
        vias = sum(1 for r in routes if r.via is not None)
        total_vias += vias
        total_pins += len(pads)
        # Each UCC27211 SOIC-8 has 8 pads -- but pads on net 0 (plane
        # net) are skipped by the rescue gate.  At minimum every
        # signal-net pad gets a via.
        signal_pins = sum(1 for p in pads if p.net != 0)
        assert vias >= signal_pins, (
            f"{ref}: expected at least {signal_pins} in-pad vias "
            f"(one per signal-net pin), got {vias}"
        )

    # Sanity check on log output -- at least one rescue line per ref
    # confirms the P_FP6 path emitted the diagnostic.
    rescue_lines = [
        rec.getMessage() for rec in caplog.records
        if "SOP in-pad rescue" in rec.getMessage()
    ]
    assert len(rescue_lines) > 0, (
        "Expected SOP in-pad rescue log lines from the P_FP6 path"
    )
    # Each of U5/U6/U7 should have at least one rescue line.
    for ref in UCC_REFS:
        matching = [l for l in rescue_lines if f" {ref} " in l]
        assert matching, (
            f"Expected at least one SOP in-pad rescue log for {ref}; "
            f"all rescue lines: {rescue_lines[:5]}"
        )


def test_softstart_revb_dispatcher_gap_documents_p_fp6_unreached(
    tmp_path: Path,
) -> None:
    """Documents the P_FP6 dispatcher-gap finding from #3390 verification.

    UCC27211 SOIC-8 at 1.27 mm pitch + 0.30 mm trace + 0.20 mm
    clearance does *not* pass
    :func:`kicad_tools.router.escape.is_dense_package` -- the dynamic
    threshold is ``2 * (0.30 + 0.20) = 1.0 mm`` which is below 1.27 mm.
    Therefore ``Autorouter.detect_dense_packages`` excludes UCC27211
    from the escape pre-pass and the P_FP6 SOP rescue path is
    unreachable on this fixture during the actual end-to-end route.

    The wiring is correct (verified by
    ``test_softstart_revb_p_fp6_dispatcher_emits_in_pad_vias`` above);
    only the dispatcher gate prevents the rescue from firing.

    This test is the negative control: if a future change updates the
    is_dense_package heuristic to include SOIC-8 at fine-pitch, this
    assertion will fail and the P_FP6 rescue will start contributing
    to the end-to-end reach number.  At that point this test should
    be updated to assert the new expected dense-package set, and
    ``test_softstart_revb_reach_floor`` should be tightened.
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
        # The gap finding: NONE of the UCC27211 SOIC-8 packages are
        # classified as dense under the current recipe.  When the
        # heuristic is updated to fix this, this assertion flips.
        assert not dense, (
            f"{ref} is now classified as dense at trace=0.30, clearance=0.20; "
            "this is a positive change -- the P_FP6 SOP rescue should now "
            "contribute to end-to-end reach.  Update the dispatcher-gap "
            "documentation in test_softstart_revb_reach_floor and tighten "
            "the reach floor accordingly."
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s", "--no-cov"]))
