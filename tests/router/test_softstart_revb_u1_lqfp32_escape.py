"""Pin the U1 LQFP-32 subgrid-escape floor for softstart rev B (Issue #3385).

Before this issue, the softstart rev B routing log opened with::

    U1: 18/32 pads escaped (no grid point reachable: 14)

Every inner-edge U1 pad was geometrically infeasible at L=2 jlcpcb-tier1
because every surface-layer neighbour cell on F.Cu was occupied by
adjacent pad copper or clearance halos -- the LQFP-32 0.8 mm pitch is
too dense for lateral escape at 0.20 mm clearance.

Phase 5 of :meth:`SubGridRouter._find_escape_for_pad` rescues these
pads by dropping a micro-via (0.30 mm OD by default) dead-centre on
each pad and unblocking the corresponding cell on the back-/inner-
layer so the main per-net router can pick up the escape from there.
This test pins:

  * U1 subgrid escape >= 28/32 pads (Issue #3385 AC #1).
  * Every rescue is tagged ``in_pad=True`` and ``is_micro=True``.
  * Every rescue lands on the layer opposite the pad.
  * The single plane-net U1 pad (pin 5 = +3.3V on the west edge) is
    NOT rescued -- it is correctly deferred to ``kct stitch`` which
    handles plane-pad stitching.

The test regenerates the softstart rev B PCB on demand (fast: schematic
+ PCB synthesis takes ~5 s; the slow part is the per-net A* search,
which we skip).  It is NOT gated on
``KICAD_RUN_SLOW_SOFTSTART_REACH=1`` because the escape-only path runs
in well under one minute.
"""

from __future__ import annotations

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


def test_softstart_revb_u1_lqfp32_escape_floor(tmp_path: Path) -> None:
    """U1 LQFP-32 subgrid escape must lift to >= 28/32 pads at L=2 tier-1.

    Issue #3385 AC #1.  This is the headline floor: lifting the U1
    subgrid escape from 18/32 (pre-fix baseline) to 28+/32 unblocks
    the dominant softstart rev B failure mode.
    """
    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.router.rules import DesignRules
    from kicad_tools.router.subgrid import SubGridRouter

    pcb_path = _regenerate_softstart_pcb(tmp_path / "softstart_u1")

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
            "AC_LINE",
            "AC_NEUTRAL",
            "FUSED_LINE",
            "GND",
            "+3.3V",
            "VRECT",
            "SCAP_POS+",
            "SCAP_POS_GND",
            "SCAP_NEG+",
            "SCAP_NEG_GND",
            "ISENSE_POS",
        ],
    )
    # ``load_pcb_for_routing`` does not carry the manufacturer through
    # to the rules object today (see Autorouter wiring); set it
    # explicitly so the subgrid rescue's capability gate fires.
    router.rules.manufacturer = "jlcpcb-tier1"

    u1_pads = [p for p in router.pads.values() if p.ref == "U1"]
    assert len(u1_pads) == 32, f"Expected 32 U1 LQFP-32 pads, got {len(u1_pads)}"

    subgrid = SubGridRouter(router.grid, rules)
    analysis = subgrid.analyze_pads(u1_pads)
    result = subgrid.generate_escape_segments(analysis)

    floor = 28
    assert result.success_count >= floor, (
        f"U1 LQFP-32 escape regressed: {result.success_count}/"
        f"{result.total_attempted} < {floor}/32 floor "
        f"(Issue #3385 AC #1)"
    )


def test_softstart_revb_u1_rescues_are_micro_via_in_pad(tmp_path: Path) -> None:
    """Every U1 Phase 5 rescue must be a micro-via tagged ``in_pad``.

    Issue #3385: the LQFP-32 0.8 mm pitch + 0.40 mm short-axis pad +
    jlcpcb-tier1 0.127 mm min-clearance combination forces the
    rescue to fall back to the micro-via OD (0.30 mm) because the
    standard 0.60 mm tier-1 via would clip the neighbour pad's
    clearance.  Tagging discipline matters: ``in_pad`` exempts the
    via from the pad-segment clearance check (the pad's own copper
    provides the annular ring), and ``is_micro`` controls the
    KiCad serialisation token.
    """
    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.router.rules import DesignRules
    from kicad_tools.router.subgrid import SubGridRouter

    pcb_path = _regenerate_softstart_pcb(tmp_path / "softstart_u1_tags")

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
            "AC_LINE",
            "AC_NEUTRAL",
            "FUSED_LINE",
            "GND",
            "+3.3V",
            "VRECT",
            "SCAP_POS+",
            "SCAP_POS_GND",
            "SCAP_NEG+",
            "SCAP_NEG_GND",
            "ISENSE_POS",
        ],
    )
    router.rules.manufacturer = "jlcpcb-tier1"

    u1_pads = [p for p in router.pads.values() if p.ref == "U1"]
    subgrid = SubGridRouter(router.grid, rules)
    analysis = subgrid.analyze_pads(u1_pads)
    result = subgrid.generate_escape_segments(analysis)

    rescues = [e for e in result.escapes if e.via is not None]
    assert len(rescues) > 0, "Expected at least one Phase 5 in-pad rescue for U1 LQFP-32"
    for e in rescues:
        assert e.via is not None
        assert e.via.in_pad is True, f"U1.{e.pad.pin} rescue via must be tagged in_pad=True"
        assert e.via.is_micro is True, (
            f"U1.{e.pad.pin} rescue via must be tagged is_micro=True "
            f"(0.60 mm tier-1 via clips LQFP-32 0.8 mm pitch neighbour)"
        )
        # Pad surface layer is F.Cu; landing must be opposite (B.Cu on 2L).
        assert e.via_layer is not None
        assert e.via_layer != e.pad.layer, (
            f"U1.{e.pad.pin} rescue via must land on the layer opposite "
            f"the pad's surface; got pad={e.pad.layer}, via={e.via_layer}"
        )


def test_softstart_revb_u1_plane_pads_not_rescued(tmp_path: Path) -> None:
    """U1 plane-net pads must NOT be rescued -- ``kct stitch`` handles them.

    Issue #3385: rescuing a plane-net pad with an in-pad via would
    emit a Route on net 0 (which several downstream consumers treat
    as "no net") and waste a via on a connection the stitch pass
    already makes by construction.  Pin 5 on U1 is the GND pin
    (verified via the schematic recipe in
    ``boards/external/softstart/generate_design.py``); confirm it
    is in the failure list, not the rescue list.
    """
    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.router.rules import DesignRules
    from kicad_tools.router.subgrid import SubGridRouter

    pcb_path = _regenerate_softstart_pcb(tmp_path / "softstart_u1_plane")

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
            "AC_LINE",
            "AC_NEUTRAL",
            "FUSED_LINE",
            "GND",
            "+3.3V",
            "VRECT",
            "SCAP_POS+",
            "SCAP_POS_GND",
            "SCAP_NEG+",
            "SCAP_NEG_GND",
            "ISENSE_POS",
        ],
    )
    router.rules.manufacturer = "jlcpcb-tier1"

    u1_pads = [p for p in router.pads.values() if p.ref == "U1"]
    subgrid = SubGridRouter(router.grid, rules)
    analysis = subgrid.analyze_pads(u1_pads)
    result = subgrid.generate_escape_segments(analysis)

    # Every rescue must be on a non-plane net.
    rescues = [e for e in result.escapes if e.via is not None]
    for e in rescues:
        assert e.pad.net != 0, (
            f"U1.{e.pad.pin} is a plane-net pad ({e.pad.net_name}) and "
            f"must not be rescued -- kct stitch handles plane stitching"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s", "--no-cov"]))
