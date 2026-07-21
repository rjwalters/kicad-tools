#!/usr/bin/env python3
"""
Differential Pair Test Board - Complete Design Generation

Epic #2556 Phase 4L (issue #2658) regression testbench.

This script orchestrates the full pipeline for board 06:
    1. Create the project file (.kicad_pro)
    2. Generate the schematic (.kicad_sch)
    3. Generate the unrouted PCB (.kicad_pcb)
    4. Route the PCB (...routed.kicad_pcb)
    5. Run DRC via ``kct check --mfr jlcpcb``

The board is a 4-layer JLCPCB tier-1 stackup
(F.Cu / In1.Cu GND / In2.Cu PWR / B.Cu) carrying 9 differential pairs
across 4 protocol families (USB 2.0, USB 3.0, PCIe Gen1, MIPI D-PHY).

The router is configured with custom ``NetClassRouting`` instances per
protocol that opt into each Phase 1-3 feature:

    - intra_pair_clearance (Phase 1A/1C)
    - coupled_routing (Phase 2E)
    - coupled_continuity_threshold (Phase 2G)
    - target_diff_impedance (Phase 3K)
    - target_single_impedance (Phase 3K)
    - skew_tolerance_mm (Phase 3H)

Usage:
    python generate_design.py [output_dir]

If no output directory is specified, files are written to ./output/.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.dev import warn_if_stale
from kicad_tools.lvs import write_lvs_report
from kicad_tools.recipes.gate import evaluate_pipeline_gate
from kicad_tools.router.rules import NET_CLASS_HIGH_SPEED, NET_CLASS_POWER, NetClassRouting

# Re-export net definitions and footprint generators from generate_pcb.
sys.path.insert(0, str(Path(__file__).parent))
import generate_pcb  # noqa: E402
import generate_schematic  # noqa: E402

warn_if_stale()

# Route-step discovery sentinel (Tier 2).  ``kct build``'s _run_step_route
# greps for this literal to decide whether to invoke ``generate_design.py
# --step route`` instead of falling back to the generic ``kct route``
# autorouter (which drops this board's diff-pair / seed / 4-layer flags).
# project.kct's ``build.route_recipe`` is the primary (Tier 1) signal; this
# sentinel is the heuristic fallback for callers without the spec key.
SUPPORTS_STEP_ROUTE = True


# =============================================================================
# Board routing contract (Issue #3413 phase 5)
# =============================================================================
# POUR_NETS is the authoritative plane-net declaration: these nets are
# excluded from the trace router (``route_pcb``'s skip list) and carried
# by copper pours + stitching vias instead.  The CI gate
# (``scripts/ci/check_diffpair_coverage.py``) reads both constants to
# assert the re-routed artifact's reach: every non-pour net with >= 2
# pads must be COMPLETE per ``NetStatusAnalyzer``.  21 = 26 declared
# nets - 5 pour nets (the issue's original ``kct route`` repro counted
# 22 because it included VBUS_USB as a signal net; the recipe's 21 is
# the canonical number).
# =============================================================================
POUR_NETS: list[str] = ["GND", "VBUS_USB", "+3V3", "+1V8", "+1V2"]
REQUIRED_SIGNAL_REACH: int = 21

# Issue #3509: pour-connectivity contract.  When True, the CI gate runs
# this recipe's shapely copper-union audit (``_audit_pour_nets``) against
# the re-routed artifact and FAILS the job if any pour net is disjoint or
# any fill-enabled zone has zero filled polygons.  Before this contract
# the recipe printed "POUR CONNECTIVITY: FAIL" in the CI log while the
# gate stayed green (PR #3506 run 27343006197) -- a latent artifact-
# refresh trap.  A board that cannot yet pass must set this to False
# WITH a tracking-issue comment (the explicit exit clause; mirrors the
# .github/routed-drc-tolerance.yml grandfathering convention) -- the
# verdict must never be silently ignored.
REQUIRE_POUR_CONNECTIVITY: bool = True

# Issue #3509: pour repair <-> re-fill iteration budget.  Was hardcoded
# at 3, which the CI re-route's convergence trend (GND 79 -> 45 -> 12
# disjoint groups across rounds) showed is too low when the routed
# copper differs from the local artifact.  The loop breaks early on
# audit PASS, so a higher cap only costs wall time in failure scenarios.
MAX_POUR_REPAIR_ROUNDS: int = 6

# Issue #3855 / #3532: SEED skip set for the 45-degree quantizer -- off-angle
# chords whose BOTH dogleg variants clip a neighbouring via barrel (a new
# ``clearance_segment_via`` error), so the skewed chord is the only
# manufacturable path through the corridor.  The committed ``--seed 42``
# artifact's diff-pair crossover chord on net 2 (USB2_D-,
# ``(112.679,109.947)->(116.616,108.602)``) is such a residual, pinned by
# uuid in ``tests/test_fleet_45_census.py::DOCUMENTED_OFF_ANGLE``.
#
# IMPORTANT (#3913): this set is only a *seed* for
# ``_resolve_quantize_treatment``, which recomputes the real flip/skip
# assignment from the CURRENT run's uuids at build time.  A uuid-keyed skip
# is unreliable on a fresh re-route because pour-repair *bridge* traces are
# minted outside the seeded router path, so their uuids change every route
# (the trap flagged in ``.claude/fresh-sweep-findings.md``): the #3946
# clearance/connectivity landing shifted board-06's geometry and introduced a
# GND-bridge dogleg that grazes a via near board-XY (57.050, 22.050), and a
# hard-coded uuid for it silently no-ops in CI.  The dynamic resolver catches
# it by geometry instead.
#
# 2026-07-08 (gallery-ready refresh): EMPTY.  The historical 864fb9ee seed
# documented the committed artifact's USB2_D- crossover chord, which the
# fresh re-route no longer produces; and any chord the resolver still has
# to skip is now picked up by the step-13 ``_split_offangle_chords``
# mid-split pass, so the committed artifact ships 0 off-angle segments
# (the fleet-45-census exemption for this board is retired).
QUANTIZE_SKIP_UUIDS: frozenset[str] = frozenset()


# =============================================================================
# Per-Protocol Net Class Declarations
# =============================================================================
# These NetClassRouting instances are the authoritative "scenario" data the
# board exercises.  ``build_net_class_map()`` below assembles them into a
# net-name -> NetClassRouting dict that ``generate_design.create_net_class_map``
# consumes during routing.
#
# Each protocol class explicitly opts into Phase 1-3 features.  AC#6 of issue
# #2658 asserts that at least one pair engages each feature; this dict is
# the single source of truth for that audit.
# =============================================================================


def usb2_net_class() -> NetClassRouting:
    """USB 2.0 High-Speed net class (1 pair).

    Reuses NET_CLASS_HIGH_SPEED as the template (intra_pair_clearance=0.075
    from Phase 1C, coupled_routing=True from Phase 2.5a) and adds the
    DRC-side coupled_continuity_threshold (Phase 2G) and target_diff_impedance
    (Phase 3K).
    """
    return NetClassRouting(
        name="USB2",
        priority=2,
        trace_width=NET_CLASS_HIGH_SPEED.trace_width,
        clearance=NET_CLASS_HIGH_SPEED.clearance,
        intra_pair_clearance=0.075,  # Phase 1C: tight intra-pair separation
        coupled_routing=True,  # Phase 2E: opt into coupled engagement
        coupled_continuity_threshold=0.7,  # Phase 2G: relax for short pair
        target_diff_impedance=90.0,  # Phase 3K: USB 2.0 90 Ohm diff
        impedance_tolerance_percent=15.0,
        skew_tolerance_mm=3.0,  # Phase 3H: USB 2.0 HS budget
        length_critical=True,
    )


def usb3_net_class() -> NetClassRouting:
    """USB 3.0 SuperSpeed net class (4 pairs).

    Tighter than USB 2.0: target_diff_impedance=90, coupled_continuity_threshold
    bumped to 0.9 (HSDI demands tight coupling), skew tolerance dropped to
    0.5mm (USB 3.0 spec ~0.4 mm).
    """
    return NetClassRouting(
        name="USB3",
        priority=2,
        trace_width=0.2,
        clearance=0.15,
        intra_pair_clearance=0.10,  # Phase 1C
        coupled_routing=True,  # Phase 2E
        coupled_continuity_threshold=0.9,  # Phase 2G: HSDI tight coupling
        target_diff_impedance=90.0,  # Phase 3K
        impedance_tolerance_percent=10.0,
        skew_tolerance_mm=0.5,  # Phase 3H: USB 3.0 budget
        length_critical=True,
    )


def pcie_net_class() -> NetClassRouting:
    """PCIe Gen1 net class (2 pairs).

    Phase 3I/3J focal point.  100 Ohm differential, 0.5mm skew is the
    tightest constraint that engages Phase 3I serpentine insertion.
    """
    return NetClassRouting(
        name="PCIe",
        priority=2,
        trace_width=0.2,
        clearance=0.15,
        intra_pair_clearance=0.10,  # Phase 1C
        coupled_routing=True,  # Phase 2E
        coupled_continuity_threshold=0.85,  # Phase 2G
        target_diff_impedance=100.0,  # Phase 3K: PCIe 100 Ohm
        impedance_tolerance_percent=10.0,
        skew_tolerance_mm=0.5,  # Phase 3H: PCIe Gen1 budget
        length_critical=True,
    )


def mipi_net_class() -> NetClassRouting:
    """MIPI D-PHY net class (2 lanes: CLK + D0).

    Tight skew (0.3mm) and 100 Ohm differential.  Exercises Phase 3I
    serpentine for the CLK pair (which is typically shortest and least
    matched).
    """
    return NetClassRouting(
        name="MIPI",
        priority=2,
        trace_width=0.2,
        clearance=0.15,
        intra_pair_clearance=0.10,  # Phase 1C
        coupled_routing=True,  # Phase 2E
        coupled_continuity_threshold=0.85,  # Phase 2G
        target_diff_impedance=100.0,  # Phase 3K: MIPI 100 Ohm
        impedance_tolerance_percent=10.0,
        skew_tolerance_mm=0.3,  # Phase 3H: tight MIPI lane budget
        length_critical=True,
    )


def sideband_net_class() -> NetClassRouting:
    """Single-ended sideband (USB_CC1, USB_CC2, MIPI_RST).

    Exercises target_single_impedance (Phase 3K) on a non-diff-pair net,
    which is the orthogonal axis to target_diff_impedance.
    """
    return NetClassRouting(
        name="Sideband",
        priority=4,
        trace_width=0.2,
        clearance=0.15,
        target_single_impedance=50.0,  # Phase 3K: 50 Ohm SE
        impedance_tolerance_percent=15.0,
    )


def build_net_class_map() -> dict[str, NetClassRouting]:
    """Build the canonical net-name -> NetClassRouting mapping.

    This is the single source of truth for both the router (consumed in
    ``route_pcb`` below) and the regression test
    (``tests/test_board_06_diffpair_test.py::test_phase_features_exercised``).
    Importing this function from the test guarantees test/implementation
    parity --- the test cannot drift from the routing config.

    Known trap (PR #3273 / refresh attempt 2026-06-07 verified):
        The ``trace_width=0.2`` settings above produce ~68 ohm against the
        50/90/100 ohm impedance targets at the JLCPCB tier-1 stackup, so
        ``kct check --net-class-map`` reports ~30 impedance violations on
        the committed PCB.  A naive ``--step route`` refresh extends those
        violations across every newly-routed segment, producing 500+
        impedance errors WITH SIDECAR even though the count WITHOUT
        SIDECAR looks like an improvement (3 vs 34).  PR #3273 fell into
        this trap; the strict CI gate (``check_routed_drc.py``) catches it
        but only on PRs that touch the committed PCB.  Fix requires
        router trace-width-by-impedance work (tracking #3313) that picks
        ~0.387mm widths for 50-ohm SE and proportionally for the diff-pair
        targets.  Until that work lands, DO NOT refresh
        ``diffpair_test_routed.kicad_pcb`` without re-running
        ``check_routed_drc.py`` WITH the sidecar.
    """
    usb2 = usb2_net_class()
    usb3 = usb3_net_class()
    pcie = pcie_net_class()
    mipi = mipi_net_class()
    sideband = sideband_net_class()

    return {
        # USB 2.0
        "USB2_D+": usb2,
        "USB2_D-": usb2,
        # USB 3.0 (4 pairs)
        "USB3_TX1+": usb3,
        "USB3_TX1-": usb3,
        "USB3_RX1+": usb3,
        "USB3_RX1-": usb3,
        "USB3_TX2+": usb3,
        "USB3_TX2-": usb3,
        "USB3_RX2+": usb3,
        "USB3_RX2-": usb3,
        # PCIe (2 pairs)
        "PCIE_TX+": pcie,
        "PCIE_TX-": pcie,
        "PCIE_RX+": pcie,
        "PCIE_RX-": pcie,
        # MIPI (2 lanes)
        "MIPI_CLK+": mipi,
        "MIPI_CLK-": mipi,
        "MIPI_D0+": mipi,
        "MIPI_D0-": mipi,
        # Single-ended sideband
        "USB_CC1": sideband,
        "USB_CC2": sideband,
        "MIPI_RST": sideband,
        # Power
        "VBUS_USB": NET_CLASS_POWER,
        "+3V3": NET_CLASS_POWER,
        "+1V8": NET_CLASS_POWER,
        "+1V2": NET_CLASS_POWER,
        "GND": NET_CLASS_POWER,
    }


# =============================================================================
# Plane-connectivity helpers (Issue #3413 phase 4)
# =============================================================================
# Recipe-local copies of the softstart hardened pour pipeline (PR #3481).
# ``NetStatusAnalyzer`` counts a pad as zone-connected when it falls inside
# the zone's *boundary* polygon even if the zone produced zero (or islanded)
# filled polygons -- the false-positive mode tracked in issue #3482.  The
# audit below is geometric (shapely copper union), so it cannot be fooled
# by a dead pour.  Kept recipe-local per the softstart precedent: the gate
# must not wait for the analyzer fix.
# =============================================================================


def _find_sexp_blocks(text: str, token: str) -> list[str]:
    """Return every balanced S-expression block starting with ``token``."""
    blocks: list[str] = []
    i = 0
    while True:
        j = text.find(token, i)
        if j < 0:
            break
        depth = 0
        k = j
        while True:
            c = text[k]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        blocks.append(text[j : k + 1])
        i = k
    return blocks


def _generate_uuid() -> str:
    import uuid as _uuid

    return str(_uuid.uuid4())


def _audit_pour_nets(pcb_path: Path, net_names: list[str]) -> dict:
    """Geometric per-net copper-connectivity audit (PR #3481 pattern).

    For each net, builds the set of physical copper elements (zone
    ``filled_polygon`` regions, segments at their actual width, via
    barrels, pad copper) and unions elements that geometrically overlap
    on a shared copper layer.  A net is electrically continuous iff all
    of its pads land in ONE connected component.

    Pad copper is approximated by the pad's *inscribed* circle, which is
    conservative (an audit "connected" verdict implies real overlap; a
    thermal-spoke connection always overlaps the inscribed circle).

    Returns:
        ``{net_name: {"connected": bool, "pad_groups": [[(pad, is_th)]],
        "zero_fill_zones": int}}``.  Requires shapely; raises
        ImportError if unavailable (a silent skip is how dead pours
        shipped on softstart in the first place -- see PR #3481).
    """
    import math
    import re

    from shapely.geometry import LineString, Point, Polygon

    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    text = pcb_path.read_text()
    all_layers = frozenset({"F.Cu", "B.Cu", "In1.Cu", "In2.Cu"})

    # Zone fills per net (+ zero-fill bookkeeping for the explicit gate).
    fills: dict[str, list] = {n: [] for n in net_names}
    zero_fill_zones: dict[str, int] = dict.fromkeys(net_names, 0)
    for zone in _find_sexp_blocks(text, "\n\t(zone") + _find_sexp_blocks(text, "\n  (zone"):
        # The zone's net is serialized as ``(net "NAME")`` by the
        # ``zones fill`` round-trip writer and as ``(net N)`` +
        # ``(net_name "NAME")`` by KiCad itself -- accept both.
        m = re.search(r'\(net_name "([^"]*)"\)', zone) or re.search(r'\(net "([^"]*)"\)', zone)
        if not m or m.group(1) not in fills:
            continue
        net = m.group(1)
        polys = _find_sexp_blocks(zone, "(filled_polygon")
        if "(fill yes" in zone and not polys:
            zero_fill_zones[net] += 1
        for block in polys:
            lay = re.search(r'\(layer "([^"]*)"\)', block).group(1)
            pts = re.findall(r"\(xy ([\d.-]+) ([\d.-]+)\)", block)
            poly = Polygon([(float(a), float(b)) for a, b in pts])
            if not poly.is_valid:
                poly = poly.buffer(0)
            fills[net].append((poly, frozenset({lay})))

    # Segments (actual width) and via barrels per net.
    net_ids = dict(re.findall(r'\(net (\d+) "([^"]*)"\)', text))
    segs: dict[str, list] = {n: [] for n in net_names}
    vias: dict[str, list] = {n: [] for n in net_names}
    for seg in _find_sexp_blocks(text, "\n\t(segment") + _find_sexp_blocks(text, "\n  (segment"):
        name = net_ids.get(re.search(r"\(net (\d+)\)", seg).group(1))
        if name not in segs:
            continue
        st = re.search(r"\(start ([\d.-]+) ([\d.-]+)\)", seg)
        en = re.search(r"\(end ([\d.-]+) ([\d.-]+)\)", seg)
        wd = re.search(r"\(width ([\d.]+)\)", seg)
        lay = re.search(r'\(layer "([^"]*)"\)', seg).group(1)
        width = float(wd.group(1)) if wd else 0.3
        line = LineString(
            [
                (float(st.group(1)), float(st.group(2))),
                (float(en.group(1)), float(en.group(2))),
            ]
        )
        segs[name].append((line.buffer(width / 2.0), frozenset({lay})))
    for via in _find_sexp_blocks(text, "\n\t(via") + _find_sexp_blocks(text, "\n  (via"):
        name = net_ids.get(re.search(r"\(net (\d+)\)", via).group(1))
        if name not in vias:
            continue
        at = re.search(r"\(at ([\d.-]+) ([\d.-]+)\)", via)
        sz = re.search(r"\(size ([\d.]+)\)", via)
        radius = (float(sz.group(1)) if sz else 0.6) / 2.0
        vias[name].append(
            (Point(float(at.group(1)), float(at.group(2))).buffer(radius), all_layers)
        )

    # Pads (absolute sheet coordinates via the analyzer's PCB model).
    analyzer = NetStatusAnalyzer(pcb_path)
    origin_x, origin_y = analyzer.pcb.board_origin
    pads: dict[str, list] = {n: [] for n in net_names}
    for fp in analyzer.pcb.footprints:
        theta = math.radians(fp.rotation or 0.0)
        for pad in fp.pads:
            if pad.net_name not in pads:
                continue
            px, py = pad.position
            rx = px * math.cos(theta) + py * math.sin(theta)
            ry = -px * math.sin(theta) + py * math.cos(theta)
            x = fp.position[0] + rx + origin_x
            y = fp.position[1] + ry + origin_y
            is_th = any("*" in str(layer) for layer in pad.layers)
            layers = (
                all_layers if is_th else frozenset({l for l in pad.layers if l.endswith(".Cu")})
            )
            radius = min(pad.size) / 2.0
            pads[pad.net_name].append(
                (
                    f"{fp.reference}.{pad.number}",
                    Point(x, y).buffer(radius),
                    layers,
                    is_th,
                )
            )

    results: dict[str, dict] = {}
    for net in net_names:
        elems: list[tuple] = list(fills[net]) + segs[net] + vias[net]
        n_fills = len(fills[net])
        pad_indices: list[tuple[int, str, bool]] = []
        for name, geom, layers, is_th in pads[net]:
            elems.append((geom, layers))
            pad_indices.append((len(elems) - 1, name, is_th))

        parent = list(range(len(elems)))

        def _find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(elems)):
            gi, li = elems[i]
            for j in range(i + 1, len(elems)):
                gj, lj = elems[j]
                if (li & lj) and gi.intersects(gj):
                    parent[_find(i)] = _find(j)

        groups: dict[int, list[tuple[str, bool]]] = {}
        for idx, name, is_th in pad_indices:
            groups.setdefault(_find(idx), []).append((name, is_th))
        pad_groups = sorted(groups.values(), key=len, reverse=True)

        # Fill-anchored stranded set (Issue #3413 phase 4 repair input):
        # a pad is "stranded" when its copper component contains NO zone
        # fill element.  This matters when NO pad reaches the pour --
        # the naive "repair everything but the largest pad group" rule
        # would leave the largest group unrepaired even though it floats
        # (the exact shape of board 06's +3V3/+1V8/+1V2 B.Cu pours,
        # whose pads all live on F.Cu).  Nets with zero fill elements
        # fall back to "all but the largest group" (trace-skeleton nets).
        anchored_roots = {_find(i) for i in range(n_fills)}
        stranded: list[tuple[str, bool]] = []
        if anchored_roots:
            for idx, name, is_th in pad_indices:
                if _find(idx) not in anchored_roots:
                    stranded.append((name, is_th))
        else:
            for group in pad_groups[1:]:
                stranded.extend(group)

        results[net] = {
            "connected": len(pad_groups) <= 1,
            "pad_groups": pad_groups,
            "stranded_pads": stranded,
            "zero_fill_zones": zero_fill_zones[net],
        }
    return results


def _repair_pour_connectivity(pcb_path: Path, net_names: list[str]) -> tuple[int, int]:
    """Repair pour-net connectivity: offset vias + stubs + island bridges.

    Issue #3413 phase 4.  Three residual classes survive the zone fill +
    ``kct stitch --avoid-pad-overlap`` pipeline on board 06:

    1. **Stranded pads** -- the stitcher's via-in-pad filter / "manual
       placement needed" skips (the GND 48/122 residual).  Fixed with an
       offset via + 0.15 mm F.Cu stub that stays legal at the jlcpcb
       standard tier (NO via-in-pad).  Candidate via positions ring the
       pad on 8 compass directions at increasing offsets (the diagonal
       hollow of the 1.27 mm BGA field sits at 0.898 mm); candidates
       whose barrel lands on the net's PRIMARY copper component are
       preferred.

    2. **Enclave pads** -- the ``_compute_pour_outlines`` carve makes
       sibling outlines disjoint, so a pad cluster of net X inside net
       Y's region has no own-net pour underneath (board 06: the +3V3
       BGA corners inside the +1V2 region, U4.12 inside the +1V8 strip,
       J3.3/J3.10 on the card edge).  The via lands locally and a
       **bridge trace** carries the connection to the net's primary
       copper on a shared layer.

    3. **Fill islands** -- signal traces on a plane layer can moat off a
       pocket of fill (board 06: an In1.Cu GND island west of the
       BGA-49).  Bridged like (2): the nearest element pair across the
       two components that shares a routable layer gets a straight
       validated trace (vias span all layers, so via-to-via bridges can
       cross on B.Cu where F.Cu/In1.Cu are blocked).

    Every via and trace is validated with shapely against ALL existing
    copper: foreign pads / segments / vias by >= 0.15 mm on the relevant
    layers, no overlap with ANY pad (the via-in-pad ban, same-net
    included), drill-to-drill >= 0.45 mm center distance.  Foreign zone
    FILLS are not obstacles -- the re-fill that follows this pass
    recomputes them and carves clearance around the new copper.

    Placed geometry is registered in the obstacle index immediately so
    later placements cannot collide with it.

    Returns:
        ``(vias_placed, bridges_placed)``.
    """
    import math
    import re

    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import nearest_points

    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    text = pcb_path.read_text()
    net_id_by_name = {name: int(num) for num, name in re.findall(r'\(net (\d+) "([^"]*)"\)', text)}
    id_to_name = {str(v): k for k, v in net_id_by_name.items()}
    all_layers = frozenset({"F.Cu", "B.Cu", "In1.Cu", "In2.Cu"})

    # --- global obstacle index ---------------------------------------------
    analyzer = NetStatusAnalyzer(pcb_path)
    origin_x, origin_y = analyzer.pcb.board_origin

    # Pads: (geom, net, layers, drill_r, center, name, is_th)
    pad_index: list[tuple] = []
    for fp in analyzer.pcb.footprints:
        theta = math.radians(fp.rotation or 0.0)
        for pad in fp.pads:
            px, py = pad.position
            rx = px * math.cos(theta) + py * math.sin(theta)
            ry = -px * math.sin(theta) + py * math.cos(theta)
            x = fp.position[0] + rx + origin_x
            y = fp.position[1] + ry + origin_y
            is_th = any("*" in str(layer) for layer in pad.layers)
            layers = (
                all_layers if is_th else frozenset({l for l in pad.layers if l.endswith(".Cu")})
            )
            # Conservative obstacle: circumscribed half-diagonal so square
            # pad corners are respected.  The CONNECTIVITY geometry uses
            # the inscribed circle instead (matching ``_audit_pour_nets``)
            # -- using the circumscribed circle for own-net union would
            # make the repair believe pads are connected that the audit
            # (correctly, conservatively) reports stranded.
            half_diag = math.hypot(pad.size[0], pad.size[1]) / 2.0
            inscribed_r = min(pad.size) / 2.0
            drill_r = float(getattr(pad, "drill", 0.0) or 0.0) / 2.0
            pad_index.append(
                (
                    Point(x, y).buffer(half_diag),
                    pad.net_name,
                    layers,
                    drill_r,
                    (x, y),
                    f"{fp.reference}.{pad.number}",
                    is_th,
                    Point(x, y).buffer(inscribed_r),
                )
            )

    # Segments: (geom, net, layer)
    seg_index: list[tuple] = []
    for seg in _find_sexp_blocks(text, "\n\t(segment") + _find_sexp_blocks(text, "\n  (segment"):
        st = re.search(r"\(start ([\d.-]+) ([\d.-]+)\)", seg)
        en = re.search(r"\(end ([\d.-]+) ([\d.-]+)\)", seg)
        wd = re.search(r"\(width ([\d.]+)\)", seg)
        lay = re.search(r'\(layer "([^"]+)"\)', seg).group(1)
        nid = re.search(r"\(net (\d+)\)", seg).group(1)
        width = float(wd.group(1)) if wd else 0.3
        line = LineString(
            [
                (float(st.group(1)), float(st.group(2))),
                (float(en.group(1)), float(en.group(2))),
            ]
        )
        seg_index.append((line.buffer(width / 2.0), id_to_name.get(nid, ""), lay))

    # Vias: (center_point, net, radius)
    via_index: list[tuple] = []
    for via in _find_sexp_blocks(text, "\n\t(via") + _find_sexp_blocks(text, "\n  (via"):
        at = re.search(r"\(at ([\d.-]+) ([\d.-]+)\)", via)
        sz = re.search(r"\(size ([\d.]+)\)", via)
        nid = re.search(r"\(net (\d+)\)", via).group(1)
        radius = (float(sz.group(1)) if sz else 0.6) / 2.0
        via_index.append(
            (Point(float(at.group(1)), float(at.group(2))), id_to_name.get(nid, ""), radius)
        )

    # Zone fills: net -> [(poly, layer)]
    fills_by_net: dict[str, list] = {n: [] for n in net_names}
    for zone in _find_sexp_blocks(text, "\n\t(zone") + _find_sexp_blocks(text, "\n  (zone"):
        m = re.search(r'\(net_name "([^"]*)"\)', zone) or re.search(r'\(net "([^"]*)"\)', zone)
        if not m or m.group(1) not in fills_by_net:
            continue
        for block in _find_sexp_blocks(zone, "(filled_polygon"):
            lay = re.search(r'\(layer "([^"]*)"\)', block).group(1)
            pts = re.findall(r"\(xy ([\d.-]+) ([\d.-]+)\)", block)
            poly = Polygon([(float(a), float(b)) for a, b in pts])
            if not poly.is_valid:
                poly = poly.buffer(0)
            fills_by_net[m.group(1)].append((poly, lay))

    # Board outline (inset 0.5 mm) from generate_pcb constants.
    min_x = generate_pcb.BOARD_ORIGIN_X + 0.5
    min_y = generate_pcb.BOARD_ORIGIN_Y + 0.5
    max_x = generate_pcb.BOARD_ORIGIN_X + generate_pcb.BOARD_WIDTH - 0.5
    max_y = generate_pcb.BOARD_ORIGIN_Y + generate_pcb.BOARD_HEIGHT - 0.5

    VIA_R = 0.225  # 0.45 mm via
    CLEAR = 0.15
    STUB_W = 0.15
    BRIDGE_W = 0.2
    # Issue #3855: the repair via's DRILL.  Drill spacing must be enforced
    # EDGE-TO-EDGE against the fab hole-to-hole floor (0.5 mm), not via a
    # hardcoded center-to-center constant -- the same defect class #3851
    # fixed in ``generate_pcb._via_ok``.  The old ``DRILL_CC = 0.45`` (c-c)
    # let two 0.25 mm-drill vias sit 0.20 mm edge-to-edge (a
    # ``hole_to_hole_clearance`` true positive); 0.5 mm edge-to-edge is
    # the manufacturable floor checked by the DRC rule (#3842).
    REPAIR_VIA_DRILL = 0.25  # matches the "(drill 0.25)" emitted below
    MIN_HOLE_TO_HOLE = 0.5  # fab drill-to-drill edge-to-edge floor

    def _drills_clear(vx: float, vy: float, ex: float, ey: float, e_drill: float) -> bool:
        """Edge-to-edge hole-to-hole guard (canonical DRC formula).

        ``e_drill`` is the EXISTING drill diameter (mm).  Mirrors
        :func:`kicad_tools.router.via_clearance.drill_hole_to_hole_clear`
        so the recipe pre-check agrees with the DRC post-check.
        """
        center = math.hypot(vx - ex, vy - ey)
        edge = center - REPAIR_VIA_DRILL / 2.0 - e_drill / 2.0
        return edge + 1e-3 >= MIN_HOLE_TO_HOLE

    def _via_ok(net: str, vx: float, vy: float) -> bool:
        if not (min_x <= vx <= max_x and min_y <= vy <= max_y):
            return False
        vpt = Point(vx, vy)
        vgeom = vpt.buffer(VIA_R)
        for entry in pad_index:
            geom, pnet, _layers, drill_r, (px, py) = entry[:5]
            if vgeom.intersects(geom):
                return False  # via-in-pad ban (same-net included)
            if pnet != net and vgeom.distance(geom) < CLEAR:
                return False
            # Issue #3855: drill hole-to-hole vs through-hole pad drills,
            # edge-to-edge (drill_r is the pad's drill RADIUS).
            if drill_r > 0 and not _drills_clear(vx, vy, px, py, drill_r * 2.0):
                return False
        for geom, snet, _lay in seg_index:
            if snet != net and vgeom.distance(geom) < CLEAR:
                return False
        for pt, vnet, radius in via_index:
            # Issue #3855: drill hole-to-hole vs existing repair vias
            # (all 0.25 mm drill), edge-to-edge against the 0.5 mm floor.
            if not _drills_clear(vx, vy, pt.x, pt.y, REPAIR_VIA_DRILL):
                return False
            if vnet != net and vgeom.distance(pt.buffer(radius)) < CLEAR:
                return False
        return True

    def _path_ok(
        net: str,
        p0: tuple[float, float],
        p1: tuple[float, float],
        layer: str,
        width: float,
    ) -> bool:
        for x, y in (p0, p1):
            if not (min_x - 0.3 <= x <= max_x + 0.3 and min_y - 0.3 <= y <= max_y + 0.3):
                return False
        path = LineString([p0, p1]).buffer(width / 2.0)
        for entry in pad_index:
            geom, pnet, layers = entry[:3]
            if pnet != net and layer in layers and path.distance(geom) < CLEAR:
                return False
        for geom, snet, lay in seg_index:
            if snet != net and lay == layer and path.distance(geom) < CLEAR:
                return False
        for pt, vnet, radius in via_index:
            if vnet != net and path.distance(pt.buffer(radius)) < CLEAR:
                return False
        return True

    directions = [(math.cos(a * math.pi / 4.0), math.sin(a * math.pi / 4.0)) for a in range(8)]
    # Near offsets first (the BGA diagonal hollow sits at 0.898 mm); the
    # long tail handles lane escapes -- a pad boxed in by parallel
    # traces (board 06: U2.F1 between USB3_TX2+/TX2- on F.Cu with
    # USB3_RX2+ on In1.Cu overhead) can still exit with a straight
    # inter-trace-lane stub to a via placed PAST the congestion.
    offsets = [0.55, 0.65, 0.75, 0.9, 1.1, 1.4, 1.8, 2.5, 3.5, 5.0, 7.0, 10.0, 14.0, 18.0]

    via_lines: list[str] = []
    seg_lines: list[str] = []
    vias_placed = 0
    bridges_placed = 0
    failed: list[str] = []

    def _emit_via(net: str, vx: float, vy: float) -> None:
        nonlocal vias_placed
        nid = net_id_by_name[net]
        via_lines.append(
            f"  (via (at {vx:.3f} {vy:.3f}) (size 0.45) (drill 0.25) "
            f'(layers "F.Cu" "B.Cu") (net {nid}) (uuid "{_generate_uuid()}"))'
        )
        via_index.append((Point(vx, vy), net, VIA_R))
        vias_placed += 1

    def _emit_seg(
        net: str,
        p0: tuple[float, float],
        p1: tuple[float, float],
        layer: str,
        width: float,
    ) -> None:
        nid = net_id_by_name[net]
        seg_lines.append(
            f"  (segment (start {p0[0]:.3f} {p0[1]:.3f}) (end {p1[0]:.3f} {p1[1]:.3f}) "
            f'(width {width}) (layer "{layer}") (net {nid}) '
            f'(uuid "{_generate_uuid()}"))'
        )
        seg_index.append((LineString([p0, p1]).buffer(width / 2.0), net, layer))

    # --- per-net connectivity loop ------------------------------------------
    for net in net_names:
        # Own elements: (geom, layerset, label).  Pads carry their name.
        own: list[tuple] = []
        for poly, lay in fills_by_net.get(net, []):
            own.append((poly, frozenset({lay}), "fill"))
        for geom, snet, lay in seg_index:
            if snet == net:
                own.append((geom, frozenset({lay}), "seg"))
        for pt, vnet, radius in via_index:
            if vnet == net:
                own.append((pt.buffer(radius), all_layers, "via"))
        for entry in pad_index:
            _obst, pnet, layers, _dr, center, name, is_th, inscribed = entry
            if pnet == net:
                # Inscribed-circle copper for own-net union (matches the
                # audit's conservative connectivity model).
                own.append((inscribed, layers, f"pad:{name}"))
        pad_center = {entry[5]: entry[4] for entry in pad_index if entry[1] == net}

        # Union-find over own elements, built ONCE and maintained
        # incrementally as repair geometry is appended (a full O(n^2)
        # shapely re-union per round is prohibitively slow for GND's
        # ~300 elements).
        parent = list(range(len(own)))

        def _find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(own)):
            gi, li, _ = own[i]
            for j in range(i + 1, len(own)):
                gj, lj, _ = own[j]
                if (li & lj) and gi.intersects(gj):
                    parent[_find(i)] = _find(j)

        def _append_own(elem: tuple) -> None:
            """Append a new element and union it against all others."""
            own.append(elem)
            parent.append(len(own) - 1)
            gi, li, _ = elem
            for j in range(len(own) - 1):
                gj, lj, _ = own[j]
                if (li & lj) and gi.intersects(gj):
                    parent[_find(len(own) - 1)] = _find(j)

        max_rounds = 40
        skipped_roots: set[int] = set()
        for _round in range(max_rounds):
            comps: dict[int, list[int]] = {}
            for i in range(len(own)):
                comps.setdefault(_find(i), []).append(i)
            # Only components carrying a pad or a fill matter (a floating
            # via/stub from an earlier round merges or is harmless).
            live = [
                idxs
                for idxs in comps.values()
                if any(own[i][2] == "fill" or own[i][2].startswith("pad:") for i in idxs)
            ]
            if len(live) <= 1:
                break

            def _score(idxs: list[int]) -> tuple:
                n_fill = sum(1 for i in idxs if own[i][2] == "fill")
                n_pad = sum(1 for i in idxs if own[i][2].startswith("pad:"))
                area = sum(own[i][0].area for i in idxs if own[i][2] == "fill")
                return (n_pad, n_fill, area)

            live.sort(key=_score, reverse=True)
            primary = live[0]
            primary_set = set(primary)
            # Connect the largest not-yet-skipped secondary component; a
            # component that failed every strategy is set aside so the
            # remaining components still get their repair attempt.
            candidates = [idxs for idxs in live[1:] if _find(idxs[0]) not in skipped_roots]
            if not candidates:
                break
            target = candidates[0]

            merged = False

            # Sub-stage A: lone SMD pad (or pad cluster) with no via -- try
            # an offset via + stub whose barrel lands on PRIMARY copper.
            comp_pads = [own[i][2][4:] for i in target if own[i][2].startswith("pad:")]
            comp_has_via = any(own[i][2] == "via" for i in target)
            if comp_pads and not comp_has_via:
                pad_name = comp_pads[0]
                x0, y0 = pad_center[pad_name]
                best = None
                best_touches = False
                for off in offsets:
                    for dx, dy in directions:
                        vx, vy = x0 + dx * off, y0 + dy * off
                        if not _via_ok(net, vx, vy):
                            continue
                        if not _path_ok(net, (x0, y0), (vx, vy), "F.Cu", STUB_W):
                            continue
                        vgeom = Point(vx, vy).buffer(VIA_R)
                        if any(vgeom.intersects(own[i][0]) for i in primary_set):
                            best = (vx, vy)
                            best_touches = True
                            break
                        if best is None:
                            best = (vx, vy)
                    if best_touches:
                        break
                if best is not None:
                    vx, vy = best
                    _emit_via(net, vx, vy)
                    _emit_seg(net, (x0, y0), (vx, vy), "F.Cu", STUB_W)
                    _append_own((Point(vx, vy).buffer(VIA_R), all_layers, "via"))
                    _append_own(
                        (
                            LineString([(x0, y0), (vx, vy)]).buffer(STUB_W / 2.0),
                            frozenset({"F.Cu"}),
                            "seg",
                        )
                    )
                    merged = True  # geometry changed; recompute components

            # Sub-stage B: bridge the target component to primary on a
            # shared layer (nearest element pairs first).
            if not merged:
                pairs: list[tuple[float, int, int, str]] = []
                for i in target:
                    gi, li, ki = own[i]
                    for j in primary:
                        gj, lj, kj = own[j]
                        shared = li & lj
                        if not shared:
                            continue
                        d = gi.distance(gj)
                        for lay in sorted(shared):
                            pairs.append((d, i, j, lay))
                pairs.sort(key=lambda t: t[0])
                for d, i, j, lay in pairs[:24]:
                    gi, gj = own[i][0], own[j][0]
                    pa, pb = nearest_points(gi, gj)
                    # Overshoot 0.35 mm into each geometry so the bridge
                    # endpoint survives fill re-quantisation.
                    vec = (pb.x - pa.x, pb.y - pa.y)
                    norm = math.hypot(*vec) or 1.0
                    ux, uy = vec[0] / norm, vec[1] / norm
                    p0 = (pa.x - ux * 0.35, pa.y - uy * 0.35)
                    p1 = (pb.x + ux * 0.35, pb.y + uy * 0.35)
                    if not _path_ok(net, p0, p1, lay, BRIDGE_W):
                        continue
                    _emit_seg(net, p0, p1, lay, BRIDGE_W)
                    _append_own(
                        (
                            LineString([p0, p1]).buffer(BRIDGE_W / 2.0),
                            frozenset({lay}),
                            "seg",
                        )
                    )
                    bridges_placed += 1
                    merged = True
                    break

            # Sub-stage C: ray-cast bridges.  The nearest-pair line is
            # often blocked by a pad row (e.g. a +3V3 BGA-corner exit must
            # thread the 0.82 mm inter-pad corridor, which only a
            # corridor-ALIGNED line clears).  Cast straight rays from the
            # target's via centers along the 8 compass directions; the
            # first intersection with primary copper within 12 mm becomes
            # the bridge endpoint.
            if not merged:
                ray_origins = [own[i][0].centroid for i in target if own[i][2] == "via"]
                for origin in ray_origins:
                    for dx, dy in directions:
                        ray = LineString(
                            [
                                (origin.x, origin.y),
                                (origin.x + dx * 12.0, origin.y + dy * 12.0),
                            ]
                        )
                        # First primary element the ray crosses (smallest
                        # distance along the ray), on any layer the
                        # origin via reaches.
                        best_t = None
                        best_lay = None
                        for j in primary:
                            gj, lj, _ = own[j]
                            if not ray.intersects(gj):
                                continue
                            hit = ray.intersection(gj)
                            t_along = ray.project(
                                hit.representative_point()
                                if hasattr(hit, "representative_point")
                                else hit
                            )
                            if best_t is None or t_along < best_t:
                                best_t = t_along
                                best_lay = sorted(lj)[0]
                        if best_t is None or best_t < 0.2:
                            continue
                        end = ray.interpolate(min(best_t + 0.35, 12.0))
                        p0 = (origin.x, origin.y)
                        p1 = (end.x, end.y)
                        if not _path_ok(net, p0, p1, best_lay, BRIDGE_W):
                            continue
                        _emit_seg(net, p0, p1, best_lay, BRIDGE_W)
                        _append_own(
                            (
                                LineString([p0, p1]).buffer(BRIDGE_W / 2.0),
                                frozenset({best_lay}),
                                "seg",
                            )
                        )
                        bridges_placed += 1
                        merged = True
                        break
                    if merged:
                        break

            # Sub-stage D: via-hop bridge.  When the target and primary
            # copper share a layer but the direct same-layer line is
            # blocked by a foreign trace (the B.Cu carve that splits a
            # bbox-carved pour), hop OVER it: drop a via inside each
            # copper region near the gap and cross on a different layer.
            if not merged:
                pairs_d: list[tuple[float, int, int]] = []
                for i in target:
                    gi = own[i][0]
                    for j in primary:
                        gj = own[j][0]
                        pairs_d.append((gi.distance(gj), i, j))
                pairs_d.sort(key=lambda x: x[0])
                for _d, i, j in pairs_d[:14]:
                    gi, gj = own[i][0], own[j][0]
                    pa, pb = nearest_points(gi, gj)
                    vec = (pb.x - pa.x, pb.y - pa.y)
                    norm = math.hypot(*vec) or 1.0
                    ux, uy = vec[0] / norm, vec[1] / norm
                    done = False
                    for back_a in (0.5, 0.9, 1.4):
                        va = (pa.x - ux * back_a, pa.y - uy * back_a)
                        if not Point(va).intersects(gi) or not _via_ok(net, *va):
                            continue
                        for back_b in (0.5, 0.9, 1.4):
                            vb = (pb.x + ux * back_b, pb.y + uy * back_b)
                            if not Point(vb).intersects(gj) or not _via_ok(net, *vb):
                                continue
                            for lay in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"):
                                if not _path_ok(net, va, vb, lay, BRIDGE_W):
                                    continue
                                _emit_via(net, *va)
                                _emit_via(net, *vb)
                                _emit_seg(net, va, vb, lay, BRIDGE_W)
                                _append_own((Point(va).buffer(VIA_R), all_layers, "via"))
                                _append_own((Point(vb).buffer(VIA_R), all_layers, "via"))
                                _append_own(
                                    (
                                        LineString([va, vb]).buffer(BRIDGE_W / 2.0),
                                        frozenset({lay}),
                                        "seg",
                                    )
                                )
                                bridges_placed += 1
                                merged = True
                                done = True
                                break
                            if done:
                                break
                        if done:
                            break
                    if done:
                        break

            if not merged:
                names = [own[i][2] for i in target if own[i][2].startswith("pad:")]
                failed.append(
                    f"{net}: cannot reconnect component {[n[4:] for n in names] or '(fill island)'}"
                )
                skipped_roots.add(_find(target[0]))

    if via_lines or seg_lines:
        content = pcb_path.read_text().rstrip().rstrip(")")
        content += "\n" + "\n".join(via_lines + seg_lines) + "\n)\n"
        pcb_path.write_text(content)
    for msg in failed:
        print(f"   UNREPAIRED: {msg}")
    return vias_placed, bridges_placed


# =============================================================================
# Gallery-ready legalization passes (2026-07-08)
# =============================================================================
# Two residual defect classes survive the router + optimizer + nudge + pour
# pipeline on this board.  Both are SAME-NET via-placement artifacts (no
# short risk), and both block the manufacturing bundle's "Errors = 0" gate:
#
#   1. ``hole_to_hole_clearance`` -- the A* leaves a two-via "staple"
#      (via -> short inter-via chain on one layer -> via) whose drills sit
#      closer than the 0.5 mm fab hole-to-hole floor.  This is the #3855
#      holdout (the guard covers the via PLACERS, not the A* path builder).
#
#   2. ``via_in_pad`` -- a layer-change via dropped at an SMD pad center
#      (the USB_CC1 In1.Cu -> U1-12 landing), illegal at the plain jlcpcb
#      tier this board targets.
#
# ``_legalize_signal_vias`` repairs both classes geometrically and
# transactionally, mirroring the ``_repair_pour_connectivity`` conventions:
# shapely validation against ALL existing copper, axis/45-aligned new
# copper by construction, and per-defect rollback when no legal repair
# exists (the residual is then reported honestly, never hidden).
# =============================================================================


def _parse_copper(text: str):
    """Parse (segments, vias, pads) with tolerant field extraction.

    Segment/via blocks appear in two serializations on this board (the
    KiCad multiline writer and the recipe's single-line emitters); fields
    are extracted per-block so ordering differences don't matter.

    Returns:
        ``(net_ids, segs, vias)`` where ``segs`` entries are
        ``{block, x1, y1, x2, y2, w, layer, net, uuid}`` and ``vias``
        entries are ``{block, x, y, size, drill, net, uuid}``.  ``net``
        is the net NAME.
    """
    import re

    net_ids = dict(re.findall(r'\(net (\d+) "([^"]*)"\)', text))
    segs = []
    for block in _find_sexp_blocks(text, "(segment"):
        st = re.search(r"\(start ([\d.-]+) ([\d.-]+)\)", block)
        en = re.search(r"\(end ([\d.-]+) ([\d.-]+)\)", block)
        wd = re.search(r"\(width ([\d.]+)\)", block)
        lay = re.search(r'\(layer "([^"]+)"\)', block)
        nid = re.search(r"\(net (\d+)\)", block)
        uid = re.search(r'\(uuid "([^"]+)"\)', block)
        if not (st and en and lay and nid):
            continue
        segs.append(
            {
                "block": block,
                "x1": float(st.group(1)),
                "y1": float(st.group(2)),
                "x2": float(en.group(1)),
                "y2": float(en.group(2)),
                "w": float(wd.group(1)) if wd else 0.2,
                "layer": lay.group(1),
                "net": net_ids.get(nid.group(1), ""),
                "uuid": uid.group(1) if uid else "",
            }
        )
    vias = []
    for block in _find_sexp_blocks(text, "(via"):
        at = re.search(r"\(at ([\d.-]+) ([\d.-]+)\)", block)
        sz = re.search(r"\(size ([\d.]+)\)", block)
        dr = re.search(r"\(drill ([\d.]+)\)", block)
        nid = re.search(r"\(net (\d+)\)", block)
        uid = re.search(r'\(uuid "([^"]+)"\)', block)
        if not (at and nid):
            continue
        vias.append(
            {
                "block": block,
                "x": float(at.group(1)),
                "y": float(at.group(2)),
                "size": float(sz.group(1)) if sz else 0.6,
                "drill": float(dr.group(1)) if dr else 0.25,
                "net": net_ids.get(nid.group(1), ""),
                "uuid": uid.group(1) if uid else "",
            }
        )
    return net_ids, segs, vias


def _parse_pads(pcb_path: Path):
    """Absolute-coordinate pad list via the analyzer's PCB model.

    Returns entries ``{x, y, w, h, net, name, is_th, drill, layers}``
    with ``w``/``h`` the axis-aligned bbox dimensions (cardinal-rotation
    swap applied, matching the ``via_in_pad`` rule's model).
    """
    import math

    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    analyzer = NetStatusAnalyzer(pcb_path)
    origin_x, origin_y = analyzer.pcb.board_origin
    all_layers = frozenset({"F.Cu", "B.Cu", "In1.Cu", "In2.Cu"})
    pads = []
    for fp in analyzer.pcb.footprints:
        rot = fp.rotation or 0.0
        theta = math.radians(rot)
        for pad in fp.pads:
            px, py = pad.position
            rx = px * math.cos(theta) + py * math.sin(theta)
            ry = -px * math.sin(theta) + py * math.cos(theta)
            x = fp.position[0] + rx + origin_x
            y = fp.position[1] + ry + origin_y
            w, h = pad.size
            total = rot % 360
            if abs(total - 90) < 0.001 or abs(total - 270) < 0.001:
                w, h = h, w
            elif not (abs(total) < 0.001 or abs(total - 180) < 0.001):
                aw = abs(math.cos(theta)) * pad.size[0] + abs(math.sin(theta)) * pad.size[1]
                ah = abs(math.sin(theta)) * pad.size[0] + abs(math.cos(theta)) * pad.size[1]
                w, h = aw, ah
            is_th = any("*" in str(layer) for layer in pad.layers)
            layers = (
                all_layers if is_th else frozenset({l for l in pad.layers if l.endswith(".Cu")})
            )
            pads.append(
                {
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "net": pad.net_name or "",
                    "name": f"{fp.reference}.{pad.number}",
                    "is_th": is_th,
                    "drill": float(getattr(pad, "drill", 0.0) or 0.0),
                    "layers": layers,
                }
            )
    return pads


def _legalize_signal_vias(pcb_path: Path) -> int:
    """Repair same-net ``via_in_pad`` + sub-floor drill-pair residuals.

    Strategy per defect class (both validated with shapely against ALL
    copper; every new segment is axis/45-aligned by construction):

    * **Staple collapse** (``hole_to_hole_clearance``, same-net pair):
      find the single-layer chain of same-net segments joining the two
      vias.  Delete one via (the "victim") plus the chain, and re-draw
      the link on the victim's OTHER attached layer as a 1-2 leg
      axis/45 dogleg to the survivor.  Net effect: one fewer via, the
      drill pair gone, connectivity preserved by construction (the
      victim's other-layer copper now reaches the survivor barrel,
      which spans all layers).

    * **Via relocation** (``via_in_pad``): ring-search candidate
      positions (8 compass directions x increasing offsets) whose barrel
      clears EVERY pad, foreign copper by >= 0.15 mm and every drill by
      >= 0.5 mm edge-to-edge; bridge old -> new with connector stubs on
      every layer the via previously joined plus the pad's copper layer
      (compass directions keep connectors axis/45 by construction).

    Returns the number of defects repaired.  Unrepairable defects are
    printed and left in place (never silently dropped).
    """
    import math

    from shapely.geometry import LineString, Point, box

    CLEAR = 0.15
    MIN_H2H = 0.5
    # KiCad board-setup ``hole_clearance`` constraint: FOREIGN copper must
    # stay >= 0.25 mm from any drilled hole edge.  kct's clearance rules
    # do not model this (the #3855-adjacent engine gap: kicad-cli flagged
    # a 0.248 mm track-to-hole graze that every kct rule passed), so the
    # repair validators enforce it directly.
    HOLE_CLEAR = 0.25
    EPS = 1e-3

    fixed = 0
    pads = _parse_pads(pcb_path)

    def _pad_box(p, inflate: float = 0.0):
        return box(
            p["x"] - p["w"] / 2 - inflate,
            p["y"] - p["h"] / 2 - inflate,
            p["x"] + p["w"] / 2 + inflate,
            p["y"] + p["h"] / 2 + inflate,
        )

    min_x = generate_pcb.BOARD_ORIGIN_X + 0.5
    min_y = generate_pcb.BOARD_ORIGIN_Y + 0.5
    max_x = generate_pcb.BOARD_ORIGIN_X + generate_pcb.BOARD_WIDTH - 0.5
    max_y = generate_pcb.BOARD_ORIGIN_Y + generate_pcb.BOARD_HEIGHT - 0.5

    def _via_pos_ok(net, vx, vy, radius, drill, vias, skip_uuids=frozenset()):
        if not (min_x <= vx <= max_x and min_y <= vy <= max_y):
            return False
        vgeom = Point(vx, vy).buffer(radius)
        for p in pads:
            pb = _pad_box(p)
            if vgeom.intersects(pb):
                return False  # via-in-pad ban (same-net included)
            if p["net"] != net and vgeom.distance(pb) < CLEAR:
                return False
            if p["drill"] > 0:
                edge = math.hypot(vx - p["x"], vy - p["y"]) - drill / 2 - p["drill"] / 2
                if edge + EPS < MIN_H2H:
                    return False
        hole = Point(vx, vy).buffer(drill / 2)
        for s in segs:
            if s["net"] != net:
                sgeom = LineString([(s["x1"], s["y1"]), (s["x2"], s["y2"])]).buffer(s["w"] / 2)
                if vgeom.distance(sgeom) < CLEAR:
                    return False
                # New hole vs existing foreign copper (any layer -- the
                # drill passes through all of them).
                if hole.distance(sgeom) < HOLE_CLEAR:
                    return False
        for v in vias:
            if v["uuid"] in skip_uuids:
                continue
            edge = math.hypot(vx - v["x"], vy - v["y"]) - drill / 2 - v["drill"] / 2
            if edge + EPS < MIN_H2H:
                return False
            if v["net"] != net:
                if vgeom.distance(Point(v["x"], v["y"]).buffer(v["size"] / 2)) < CLEAR:
                    return False
                # New barrel vs existing foreign hole.
                if vgeom.distance(Point(v["x"], v["y"]).buffer(v["drill"] / 2)) < HOLE_CLEAR:
                    return False
        return True

    def _leg_ok(net, p0, p1, layer, width, vias, skip_uuids=frozenset()):
        if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) < EPS:
            return True
        path = LineString([p0, p1]).buffer(width / 2)
        for p in pads:
            if p["net"] != net:
                if layer in p["layers"] and path.distance(_pad_box(p)) < CLEAR:
                    return False
                # Track vs foreign TH pad hole (any layer).
                if p["drill"] > 0:
                    if path.distance(Point(p["x"], p["y"]).buffer(p["drill"] / 2)) < HOLE_CLEAR:
                        return False
        for s in segs:
            if s["net"] != net and s["layer"] == layer:
                sgeom = LineString([(s["x1"], s["y1"]), (s["x2"], s["y2"])]).buffer(s["w"] / 2)
                if path.distance(sgeom) < CLEAR:
                    return False
        for v in vias:
            if v["uuid"] in skip_uuids or v["net"] == net:
                continue
            if path.distance(Point(v["x"], v["y"]).buffer(v["size"] / 2)) < CLEAR:
                return False
            # Track vs foreign via hole (KiCad ``hole_clearance``).
            if path.distance(Point(v["x"], v["y"]).buffer(v["drill"] / 2)) < HOLE_CLEAR:
                return False
        return True

    def _is_45(dx: float, dy: float) -> bool:
        return abs(dx) < EPS or abs(dy) < EPS or abs(abs(dx) - abs(dy)) < EPS

    def _dogleg(p0, p1):
        """1-2 leg axis/45 decompositions of p0 -> p1 (both orders)."""
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        if _is_45(dx, dy):
            return [[(p0, p1)]]
        sx = math.copysign(1.0, dx)
        sy = math.copysign(1.0, dy)
        diag = min(abs(dx), abs(dy))
        cands = []
        if abs(dx) > abs(dy):
            mid_a = (p0[0] + sx * (abs(dx) - diag), p0[1])  # axis first
            mid_b = (p0[0] + sx * diag, p0[1] + sy * diag)  # diag first
        else:
            mid_a = (p0[0], p0[1] + sy * (abs(dy) - diag))
            mid_b = (p0[0] + sx * diag, p0[1] + sy * diag)
        cands.append([(p0, mid_a), (mid_a, p1)])
        cands.append([(p0, mid_b), (mid_b, p1)])
        return cands

    def _seg_line(net_name, p0, p1, layer, width, net_num):
        return (
            f"  (segment (start {p0[0]:.3f} {p0[1]:.3f}) (end {p1[0]:.3f} {p1[1]:.3f}) "
            f'(width {width}) (layer "{layer}") (net {net_num}) '
            f'(uuid "{_generate_uuid()}"))'
        )

    # Iterate: re-parse after every applied repair so obstacle state is
    # always current.
    for _round in range(8):
        text = pcb_path.read_text()
        net_ids, segs, vias = _parse_copper(text)
        net_num_by_name = {v: k for k, v in net_ids.items()}

        def _attached(vx, vy):
            out = []
            for s in segs:
                if (abs(s["x1"] - vx) < EPS and abs(s["y1"] - vy) < EPS) or (
                    abs(s["x2"] - vx) < EPS and abs(s["y2"] - vy) < EPS
                ):
                    out.append(s)
            return out

        repair_applied = False

        # ---- defect class 1: sub-floor same-net drill pairs ----------------
        for i in range(len(vias)):
            for j in range(i + 1, len(vias)):
                a, b = vias[i], vias[j]
                edge = (
                    math.hypot(a["x"] - b["x"], a["y"] - b["y"]) - a["drill"] / 2 - b["drill"] / 2
                )
                if edge + EPS >= MIN_H2H:
                    continue
                if a["net"] != b["net"] or not a["net"]:
                    print(
                        f"   UNREPAIRED drill pair (foreign nets "
                        f"{a['net']}/{b['net']}) at ({a['x']:.2f},{a['y']:.2f})"
                    )
                    continue
                net = a["net"]
                # Find the single-layer same-net chain joining a and b.
                chain = None
                chain_layer = None
                for lay in ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu"):
                    lsegs = [s for s in segs if s["net"] == net and s["layer"] == lay]
                    # BFS from a's position to b's position.
                    frontier = [(a["x"], a["y"])]
                    seen_pts = {(round(a["x"], 3), round(a["y"], 3))}
                    used = []
                    parent_map = {}
                    found = False
                    while frontier and not found:
                        cx, cy = frontier.pop(0)
                        for s in lsegs:
                            for near_end, far_end in (
                                ((s["x1"], s["y1"]), (s["x2"], s["y2"])),
                                ((s["x2"], s["y2"]), (s["x1"], s["y1"])),
                            ):
                                if abs(near_end[0] - cx) < EPS and abs(near_end[1] - cy) < EPS:
                                    key = (round(far_end[0], 3), round(far_end[1], 3))
                                    if key in seen_pts:
                                        continue
                                    seen_pts.add(key)
                                    parent_map[key] = (s, (round(cx, 3), round(cy, 3)))
                                    if (
                                        abs(far_end[0] - b["x"]) < EPS
                                        and abs(far_end[1] - b["y"]) < EPS
                                    ):
                                        found = True
                                        break
                                    frontier.append(far_end)
                            if found:
                                break
                    if found:
                        # Walk parents back to collect the chain.
                        used = []
                        key = (round(b["x"], 3), round(b["y"], 3))
                        start_key = (round(a["x"], 3), round(a["y"], 3))
                        while key != start_key:
                            s, prev = parent_map[key]
                            used.append(s)
                            key = prev
                        chain = used
                        chain_layer = lay
                        break
                if chain is None:
                    print(
                        f"   UNREPAIRED drill pair {net} at "
                        f"({a['x']:.2f},{a['y']:.2f}): no single-layer chain"
                    )
                    continue
                # Interior chain nodes must have no other attachments.
                interior = set()
                for s in chain:
                    for pt in ((s["x1"], s["y1"]), (s["x2"], s["y2"])):
                        k = (round(pt[0], 3), round(pt[1], 3))
                        if k not in (
                            (round(a["x"], 3), round(a["y"], 3)),
                            (round(b["x"], 3), round(b["y"], 3)),
                        ):
                            interior.add(k)
                chain_ids = {id(s) for s in chain}
                tee = False
                for k in interior:
                    for s in segs:
                        if id(s) in chain_ids:
                            continue
                        for pt in ((s["x1"], s["y1"]), (s["x2"], s["y2"])):
                            if abs(pt[0] - k[0]) < EPS and abs(pt[1] - k[1]) < EPS:
                                tee = True
                    for v in vias:
                        if abs(v["x"] - k[0]) < EPS and abs(v["y"] - k[1]) < EPS:
                            tee = True
                    for p in pads:
                        if math.hypot(p["x"] - k[0], p["y"] - k[1]) < 0.2:
                            tee = True
                if tee:
                    print(
                        f"   drill pair {net} at ({a['x']:.2f},{a['y']:.2f}): "
                        f"chain has tee (pad escape) -- collapse skipped, trying slide"
                    )
                done = False
                for victim, survivor in () if tee else ((a, b), (b, a)):
                    other = [
                        s
                        for s in _attached(victim["x"], victim["y"])
                        if id(s) not in chain_ids and s["layer"] != chain_layer
                    ]
                    other_layers = {s["layer"] for s in other}
                    if len(other_layers) != 1:
                        continue
                    lay = next(iter(other_layers))
                    width = max(s["w"] for s in other)
                    for legs in _dogleg((victim["x"], victim["y"]), (survivor["x"], survivor["y"])):
                        if all(_leg_ok(net, p0, p1, lay, width, vias) for p0, p1 in legs):
                            new_text = text
                            new_text = new_text.replace(victim["block"], "")
                            for s in chain:
                                new_text = new_text.replace(s["block"], "")
                            add = [
                                _seg_line(net, p0, p1, lay, width, net_num_by_name[net])
                                for p0, p1 in legs
                                if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) >= EPS
                            ]
                            # If the collapse leaves the SURVIVOR with
                            # copper on a single layer only (the dogleg
                            # re-landed on its own escape layer), the via
                            # no longer changes layers -- remove it too so
                            # no dangling barrel ships.
                            surv_att = [
                                s
                                for s in _attached(survivor["x"], survivor["y"])
                                if id(s) not in chain_ids
                            ]
                            surv_layers = {s["layer"] for s in surv_att} | {lay}
                            n_removed = 1
                            if len(surv_layers) == 1:
                                new_text = new_text.replace(survivor["block"], "")
                                n_removed = 2
                            new_text = new_text.rstrip().rstrip(")")
                            new_text += "\n" + "\n".join(add) + "\n)\n"
                            pcb_path.write_text(new_text)
                            print(
                                f"   collapsed {net} via staple at "
                                f"({victim['x']:.2f},{victim['y']:.2f}) -> "
                                f"({survivor['x']:.2f},{survivor['y']:.2f}) "
                                f"[{len(add)} new {lay} leg(s), {n_removed} via(s) removed]"
                            )
                            fixed += 1
                            done = True
                            break
                    if done:
                        break
                if done:
                    repair_applied = True
                    break
                # Fallback: SLIDE one via of the pair to a ring candidate
                # that clears the drill floor, bridging old -> new with
                # axis/45 connector stubs on every attached layer (the
                # staple-through-pad case: the inter-via chain carries a
                # pad escape, so it cannot be deleted -- J1.B6 here).
                directions = [
                    (math.cos(k * math.pi / 4.0), math.sin(k * math.pi / 4.0)) for k in range(8)
                ]
                for victim in (b, a):
                    # ALL attached layers need a connector -- including the
                    # chain layer: the slide keeps the chain in place, so
                    # the copper ending at the old via position on EVERY
                    # layer must be bridged to the new barrel.
                    att = _attached(victim["x"], victim["y"])
                    att_layers = sorted({s["layer"] for s in att}) or ["F.Cu"]
                    width_by_layer = {
                        lay: max(s["w"] for s in att if s["layer"] == lay) for lay in att_layers
                    }
                    radius = victim["size"] / 2
                    for off in (0.25, 0.35, 0.5, 0.7, 0.9, 1.2, 1.6):
                        for ux, uy in directions:
                            nx = victim["x"] + ux * off
                            ny = victim["y"] + uy * off
                            if not _via_pos_ok(
                                net,
                                nx,
                                ny,
                                radius,
                                victim["drill"],
                                vias,
                                skip_uuids={victim["uuid"]},
                            ):
                                continue
                            if not all(
                                _leg_ok(
                                    net,
                                    (victim["x"], victim["y"]),
                                    (nx, ny),
                                    lay,
                                    width_by_layer[lay],
                                    vias,
                                    skip_uuids={victim["uuid"]},
                                )
                                for lay in att_layers
                            ):
                                continue
                            import re as _re

                            new_block = _re.sub(
                                r"\(at [\d.-]+ [\d.-]+\)",
                                f"(at {nx:.3f} {ny:.3f})",
                                victim["block"],
                                count=1,
                            )
                            new_text = text.replace(victim["block"], new_block)
                            add = [
                                _seg_line(
                                    net,
                                    (victim["x"], victim["y"]),
                                    (nx, ny),
                                    lay,
                                    width_by_layer[lay],
                                    net_num_by_name[net],
                                )
                                for lay in att_layers
                            ]
                            new_text = new_text.rstrip().rstrip(")")
                            new_text += "\n" + "\n".join(add) + "\n)\n"
                            pcb_path.write_text(new_text)
                            print(
                                f"   slid {net} via ({victim['x']:.2f},{victim['y']:.2f})"
                                f" -> ({nx:.3f},{ny:.3f}) to clear drill floor "
                                f"[connectors on {att_layers}]"
                            )
                            fixed += 1
                            done = True
                            break
                        if done:
                            break
                    if done:
                        break
                if done:
                    repair_applied = True
                    break
                print(
                    f"   UNREPAIRED drill pair {net} at ({a['x']:.2f},{a['y']:.2f}): "
                    f"no legal dogleg or slide"
                )
            if repair_applied:
                break
        if repair_applied:
            continue

        # ---- defect class 2: via drilled inside an SMD pad -----------------
        for v in vias:
            in_pad = None
            for p in pads:
                if p["is_th"]:
                    continue
                # Drill circle fully inside the pad bbox (rule geometry).
                r = v["drill"] / 2
                if (
                    p["x"] - p["w"] / 2 <= v["x"] - r
                    and v["x"] + r <= p["x"] + p["w"] / 2
                    and p["y"] - p["h"] / 2 <= v["y"] - r
                    and v["y"] + r <= p["y"] + p["h"] / 2
                ):
                    in_pad = p
                    break
            if in_pad is None:
                continue
            net = v["net"]
            att_layers = {s["layer"] for s in _attached(v["x"], v["y"])}
            pad_layer = "F.Cu" if "F.Cu" in in_pad["layers"] else "B.Cu"
            conn_layers = sorted(att_layers | {pad_layer})
            radius = v["size"] / 2
            directions = [
                (math.cos(k * math.pi / 4.0), math.sin(k * math.pi / 4.0)) for k in range(8)
            ]
            placed = False
            for off in (0.55, 0.65, 0.75, 0.9, 1.1, 1.4, 1.8):
                for ux, uy in directions:
                    nx, ny = v["x"] + ux * off, v["y"] + uy * off
                    if not _via_pos_ok(
                        net, nx, ny, radius, v["drill"], vias, skip_uuids={v["uuid"]}
                    ):
                        continue
                    if not all(
                        _leg_ok(
                            net,
                            (v["x"], v["y"]),
                            (nx, ny),
                            lay,
                            0.15,
                            vias,
                            skip_uuids={v["uuid"]},
                        )
                        for lay in conn_layers
                    ):
                        continue
                    import re as _re

                    new_block = _re.sub(
                        r"\(at [\d.-]+ [\d.-]+\)",
                        f"(at {nx:.3f} {ny:.3f})",
                        v["block"],
                        count=1,
                    )
                    new_text = text.replace(v["block"], new_block)
                    add = [
                        _seg_line(net, (v["x"], v["y"]), (nx, ny), lay, 0.15, net_num_by_name[net])
                        for lay in conn_layers
                    ]
                    new_text = new_text.rstrip().rstrip(")")
                    new_text += "\n" + "\n".join(add) + "\n)\n"
                    pcb_path.write_text(new_text)
                    print(
                        f"   relocated {net} via out of pad {in_pad['name']}: "
                        f"({v['x']:.2f},{v['y']:.2f}) -> ({nx:.3f},{ny:.3f}) "
                        f"[connectors on {conn_layers}]"
                    )
                    fixed += 1
                    placed = True
                    break
                if placed:
                    break
            if placed:
                repair_applied = True
                break
            print(
                f"   UNREPAIRED via-in-pad {net} at ({v['x']:.2f},{v['y']:.2f}) "
                f"in {in_pad['name']}: no legal relocation"
            )
        if not repair_applied:
            break
    return fixed


def _split_offangle_chords(pcb_path: Path, baseline: set[tuple[str, ...]]) -> int:
    """Mid-split 3-leg fallback for off-angle chords the quantizer skipped.

    ``quantize_pcb_file`` offers exactly two dogleg variants per chord
    (diag-first and axis-first); when BOTH graze foreign copper the chord
    is skipped and ships off-angle (the fleet-45-census documented
    residual).  A 3-leg *mid-split* (axis leg, 45 diag, axis leg) slides
    the diagonal along the chord, which often threads corridors both
    2-leg bulges cannot (board 06: the GND pour-repair bridge between the
    USB3_RX2+/RX2- via barrels in the BGA field).

    Each candidate is validated the same way as the step-12d gate: the
    mutated copy's clearance signature must introduce nothing beyond
    ``baseline``.  Returns the number of chords replaced.
    """
    import math
    import os
    import tempfile

    from kicad_tools.router.quantize import segment_angle_census

    EPS = 1e-3
    HOLE_CLEAR = 0.25  # KiCad board-setup hole_clearance (see _legalize_signal_vias)
    _total, off_angle = segment_angle_census(pcb_path)
    if not off_angle:
        return 0

    text = pcb_path.read_text()
    _net_ids, segs, vias = _parse_copper(text)
    fixed = 0

    def _legs_clear_foreign_holes(legs, width: float, net: str) -> bool:
        from shapely.geometry import LineString as _LS
        from shapely.geometry import Point as _Pt

        for q0, q1 in legs:
            path = _LS([q0, q1]).buffer(width / 2)
            for v in vias:
                if v["net"] == net:
                    continue
                if path.distance(_Pt(v["x"], v["y"]).buffer(v["drill"] / 2)) < HOLE_CLEAR:
                    return False
        return True

    for entry in off_angle:
        uuid = entry["uuid"]
        seg = next((s for s in segs if s["uuid"] == uuid), None)
        if seg is None:
            continue
        p0 = (seg["x1"], seg["y1"])
        p1 = (seg["x2"], seg["y2"])
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        adx, ady = abs(dx), abs(dy)
        diag = min(adx, ady)
        extra = max(adx, ady) - diag
        if diag < EPS or extra < EPS:
            continue
        sx = math.copysign(1.0, dx)
        sy = math.copysign(1.0, dy)
        net_num = None
        import re as _re

        m = _re.search(r"\(net (\d+)\)", seg["block"])
        net_num = m.group(1) if m else "0"
        replaced = False
        for frac in (0.5, 0.35, 0.65, 0.25, 0.75, 0.15, 0.85):
            if replaced:
                break
            # Round the split length to the emitted 3-decimal precision so
            # the diagonal leg is EXACTLY 45 degrees after formatting (a
            # half-thousandth propagation would re-trip the census).
            a_len = round(extra * frac, 3)
            if adx > ady:
                m1 = (round(p0[0] + sx * a_len, 3), p0[1])
                m2 = (round(m1[0] + sx * diag, 3), round(p0[1] + sy * diag, 3))
                m2 = (m2[0], round(m1[1] + sy * abs(m2[0] - m1[0]), 3))
            else:
                m1 = (p0[0], round(p0[1] + sy * a_len, 3))
                m2 = (round(p0[0] + sx * diag, 3), round(m1[1] + sy * diag, 3))
                m2 = (m2[0], round(m1[1] + sy * abs(m2[0] - m1[0]), 3))
            legs = [(p0, m1), (m1, m2), (m2, p1)]
            legs = [(q0, q1) for q0, q1 in legs if math.hypot(q1[0] - q0[0], q1[1] - q0[1]) >= EPS]
            # Width fallback: the original width first; then a narrowed
            # bridge (>= the 0.1016 mm fab floor) -- corridor chords sit in
            # via-dense fields where the full-width leg can violate the
            # 0.25 mm KiCad hole-clearance even though every kct clearance
            # rule passes (the 2 um USB3_RX2 graze this pass exists for).
            for width in (seg["w"], 0.15):
                if not _legs_clear_foreign_holes(legs, width, seg["net"]):
                    continue
                add = "\n".join(
                    f"  (segment (start {q0[0]:.3f} {q0[1]:.3f}) (end {q1[0]:.3f} {q1[1]:.3f}) "
                    f'(width {width}) (layer "{seg["layer"]}") (net {net_num}) '
                    f'(uuid "{_generate_uuid()}"))'
                    for q0, q1 in legs
                )
                new_text = text.replace(seg["block"], "").rstrip().rstrip(")")
                new_text += "\n" + add + "\n)\n"
                fd, tmp = tempfile.mkstemp(suffix=".kicad_pcb")
                os.close(fd)
                try:
                    Path(tmp).write_text(new_text)
                    if _clearance_signature(Path(tmp)) - baseline:
                        continue
                finally:
                    os.unlink(tmp)
                pcb_path.write_text(new_text)
                text = new_text
                print(
                    f"   split off-angle chord {uuid[:8]} ({entry['off_deg']:.1f} deg, "
                    f"net {seg['net']}) into {len(legs)} 45-aligned leg(s) "
                    f"[mid-split frac={frac}, width={width}]"
                )
                fixed += 1
                replaced = True
                break
        if not replaced:
            print(
                f"   off-angle chord {uuid[:8]} ({entry['off_deg']:.1f} deg) "
                f"has no clean mid-split -- kept as documented residual"
            )
    return fixed


def _repair_pair_overlap_solo(router, net_map: dict[str, int]) -> list[str]:
    """Rip + solo-re-route a diff-pair side whose copper rides its partner.

    Issue #3413 phase 6 residual: the negotiated loop's banked best
    snapshot can carry a small unresolved overflow where one side of a
    declared pair physically overlaps the other side's copper (seed-42
    measurement: USB3_RX1- over USB3_RX1+ at the J1 fan-out, overflow=2,
    -> 4 ``diffpair_clearance_intra`` + 6 ``clearance_segment_via``
    errors).  Single-net ``route_net`` treats ALL committed copper as
    hard obstacles, so a successful solo re-route of one side is
    overlap-free by construction.

    Transactional per pair: the ripped side's routes are restored
    verbatim if the solo re-route fails or still overlaps.

    Returns the list of net names that were re-routed.
    """
    from shapely.geometry import LineString, Point

    def _net_geoms(net_id: int):
        """(same-layer segment geoms by layer, via geoms) for a net."""
        by_layer: dict = {}
        vias = []
        nseg = 0
        for r in router.routes:
            if r.net != net_id:
                continue
            for s in r.segments:
                by_layer.setdefault(str(s.layer), []).append(
                    LineString([(s.x1, s.y1), (s.x2, s.y2)]).buffer(s.width / 2.0)
                )
                nseg += 1
            for v in r.vias:
                vias.append(Point(v.x, v.y).buffer(v.diameter / 2.0))
        return by_layer, vias, nseg

    def _sides_overlap(a_id: int, b_id: int) -> bool:
        ag, av, _ = _net_geoms(a_id)
        bg, bv, _ = _net_geoms(b_id)
        # same-layer segment/segment overlap
        for lay, geoms in ag.items():
            for g in geoms:
                for h in bg.get(lay, []):
                    if g.intersects(h):
                        return True
        # via barrels span all layers -> check against everything
        for v in av:
            for geoms in bg.values():
                for h in geoms:
                    if v.intersects(h):
                        return True
            for w in bv:
                if v.intersects(w):
                    return True
        for v in bv:
            for geoms in ag.values():
                for h in geoms:
                    if v.intersects(h):
                        return True
        return False

    repaired: list[str] = []
    for p_name, n_name in generate_pcb.DIFFPAIRS.items():
        p_id = net_map.get(p_name)
        n_id = net_map.get(n_name)
        if p_id is None or n_id is None:
            continue
        if not _sides_overlap(p_id, n_id):
            continue
        _, _, p_segs = _net_geoms(p_id)
        _, _, n_segs = _net_geoms(n_id)
        side_id, side_name = (n_id, n_name) if n_segs <= p_segs else (p_id, p_name)
        partner_id = p_id if side_id == n_id else n_id
        print(
            f"   {p_name}/{n_name}: physically overlapping copper -- "
            f"ripping {side_name} for solo re-route"
        )
        import contextlib as _ctx

        old_routes = [r for r in router.routes if r.net == side_id]
        for r in old_routes:
            with _ctx.suppress(Exception):
                router.grid.unmark_route_usage(r)
            router.grid.unmark_route(r)
            router.routes.remove(r)

        new_routes = router.route_net(side_id)

        def _rollback() -> None:
            for r in list(new_routes or []):
                with _ctx.suppress(Exception):
                    router.grid.unmark_route(r)
                if r in router.routes:
                    router.routes.remove(r)
            for r in old_routes:
                router.routes.append(r)
                try:
                    router._mark_route(r)
                except Exception:
                    router.grid.mark_route(r)

        if not new_routes:
            print(f"   {side_name}: solo re-route FAILED -- restoring original")
            _rollback()
            continue
        if _sides_overlap(side_id, partner_id):
            print(f"   {side_name}: solo re-route still overlaps partner -- restoring original")
            _rollback()
            continue
        # Issue #3507: the optimizer/nudge passes are now grid-
        # transactional (``optimize_routes_grid_synced`` + the resync
        # inside ``drc_verify_and_nudge``), so the solo A* above ran
        # against the TRUE post-mutation copper.  Keep this geometric
        # cross-check as defense-in-depth: validate the new copper
        # against every OTHER net's CURRENT route geometry (not the
        # grid) and roll back on any cross-net contact.
        cross = False
        new_by_layer: dict = {}
        new_vias = []
        for r in new_routes:
            for s in r.segments:
                new_by_layer.setdefault(str(s.layer), []).append(
                    LineString([(s.x1, s.y1), (s.x2, s.y2)]).buffer(s.width / 2.0)
                )
            for v in r.vias:
                new_vias.append(Point(v.x, v.y).buffer(v.diameter / 2.0))
        for other in router.routes:
            if other.net == side_id or cross:
                continue
            for s in other.segments:
                og = LineString([(s.x1, s.y1), (s.x2, s.y2)]).buffer(s.width / 2.0)
                if any(g.intersects(og) for g in new_by_layer.get(str(s.layer), [])):
                    cross = True
                    break
                if any(v.intersects(og) for v in new_vias):
                    cross = True
                    break
            if cross:
                break
            for v in other.vias:
                ov = Point(v.x, v.y).buffer(v.diameter / 2.0)
                if any(g.intersects(ov) for geoms in new_by_layer.values() for g in geoms) or any(
                    w.intersects(ov) for w in new_vias
                ):
                    cross = True
                    break
        if cross:
            print(f"   {side_name}: solo re-route contacts foreign copper -- restoring original")
            _rollback()
            continue
        repaired.append(side_name)
    return repaired


# =============================================================================
# Pipeline Steps
# =============================================================================


def create_project(output_dir: Path, project_name: str) -> Path:
    """Create the .kicad_pro file."""
    print("\n" + "=" * 60)
    print("Creating Project File...")
    print("=" * 60)

    output_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{project_name}.kicad_pro"
    project_data = create_minimal_project(filename)

    project_path = output_dir / filename
    save_project(project_data, project_path)
    print(f"   Project: {project_path}")
    return project_path


def create_schematic(output_dir: Path) -> Path:
    """Generate the schematic."""
    output_path = output_dir / "diffpair_test.kicad_sch"
    generate_schematic.create_diffpair_schematic(output_path)
    return output_path


def create_pcb(output_dir: Path) -> Path:
    """Generate the unrouted PCB."""
    print("\n" + "=" * 60)
    print("Creating PCB...")
    print("=" * 60)
    output_path = output_dir / "diffpair_test.kicad_pcb"
    pcb_content = generate_pcb.generate_pcb()
    output_path.write_text(pcb_content)
    print(f"   PCB: {output_path}")
    print(f"   Nets: {len([n for n in generate_pcb.NETS.values() if n > 0])}")
    print(f"   Diff pairs: {len(generate_pcb.DIFFPAIRS)}")
    return output_path


class PostQuantizeClearanceError(RuntimeError):
    """Raised when the step-12d gate finds NEW post-quantize grazes.

    A dedicated type so the step-12 ``except Exception`` graceful-degrade
    handler (which downgrades quantizer/re-fill hiccups to a WARNING) can
    re-raise this hard clearance failure instead of swallowing it -- a
    clearance short must abort the build, not print a warning (#3913).
    """


def _clearance_signature(pcb_path: Path) -> set[tuple[str, ...]]:
    """Return the set of clearance-violation identities on ``pcb_path``.

    Issue #3913: used to diff the clearance state across the step-12
    quantize mutation so the post-quantize gate fires only on NEW
    violations, not the board's allowlisted marginal-clearance baseline.

    Each violation is reduced to a coarse, order-independent identity
    ``(rule_id, layer, sorted-items, rounded-location)`` so a segment
    endpoint that shifts by a dogleg's sub-mm bulge still matches its
    pre-quantize twin (the location is rounded to 0.1 mm buckets).  A
    genuinely new graze at a different via/segment pair produces a new
    identity that is absent from the pre-quantize set.
    """
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    pcb = PCB.load(pcb_path)
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4)
    results = checker.check_clearances()
    signature: set[tuple[str, ...]] = set()
    for v in results.violations:
        if not v.is_error:
            continue
        loc = ""
        if v.location is not None:
            loc = f"{round(v.location[0], 1):.1f},{round(v.location[1], 1):.1f}"
        identity = (
            v.rule_id,
            v.layer or "",
            "|".join(sorted(v.items)),
            loc,
        )
        signature.add(identity)
    return signature


def _check_post_quantize_clearances(pcb_path: Path, baseline: set[tuple[str, ...]]) -> bool:
    """Re-verify clearances after the step-12 quantize + re-fill.

    Issue #3913: ``kicad_tools.router.quantize`` mutates committed copper
    (each off-angle chord becomes a two-leg dogleg), and its own
    ``dogleg_points()`` docstring obliges callers to re-verify clearances
    afterward -- the dogleg's perpendicular bulge can graze a foreign via
    barrel (the #3855 mode) yet still preserve endpoints, so pour audits
    and endpoint-preserving checks miss it.  The board-06 pipeline
    previously returned from step 12 with no clearance gate, certifying a
    -0.337 mm segment-to-via short that ``kicad-cli`` flagged.

    Returns ``True`` when no NEW clearance violation was introduced by the
    quantize step (violations already present in ``baseline`` -- the
    board's allowlisted marginal-clearance floor -- are tolerated).
    Returns ``False`` and prints the offending violations otherwise.
    """
    post = _clearance_signature(pcb_path)
    new_ids = post - baseline
    if not new_ids:
        n_carried = len(post & baseline)
        print(
            f"   POST-QUANTIZE CLEARANCE: PASS "
            f"(0 new violations; {n_carried} pre-existing baseline "
            f"violation(s) unchanged)"
        )
        return True

    # Re-run the check to recover full messages for the new identities.
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    pcb = PCB.load(pcb_path)
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4)
    results = checker.check_clearances()
    print(f"   POST-QUANTIZE CLEARANCE: FAIL ({len(new_ids)} NEW violation(s)):")
    for v in results.violations:
        if not v.is_error:
            continue
        loc = ""
        if v.location is not None:
            loc = f"{round(v.location[0], 1):.1f},{round(v.location[1], 1):.1f}"
        identity = (v.rule_id, v.layer or "", "|".join(sorted(v.items)), loc)
        if identity in new_ids:
            where = f" @ ({v.location[0]:.3f}, {v.location[1]:.3f})" if v.location else ""
            print(f"     [{v.rule_id}] {v.message}{where}")
    return False


def _resolve_quantize_treatment(
    pre_quantize_path: Path,
    baseline: set[tuple[str, ...]],
    seed_skip_uuids: frozenset[str] | set[str] = frozenset(),
) -> tuple[set[str], set[str]]:
    """Compute per-segment 45-degree-quantize treatment for THIS run (#3913).

    The quantizer replaces each off-angle chord with a two-leg dogleg whose
    perpendicular bulge can graze a foreign via barrel (the #3855 mode).
    ``dogleg_points()`` offers an axis-first *flip* variant (bulge on the
    opposite side of the chord) as the escape hatch; when BOTH variants
    graze, the chord must be left off-angle (skipped) -- it is the only
    manufacturable path through that corridor.

    Historically the offending uuids were hard-coded in
    ``QUANTIZE_SKIP_UUIDS``.  That is fragile: the grazing chord here is a
    GND pour-repair *bridge* trace, and bridge-trace uuids are NOT
    reproducible across re-routes (they are minted outside the seeded
    router path, so a fresh ``--seed 42`` regeneration mints new uuids --
    the exact trap flagged in ``.claude/fresh-sweep-findings.md``).  A
    hard-coded uuid therefore silently no-ops on the CI re-route and the
    step-12d gate aborts the build (issue #3913).

    This resolver instead reads the CURRENT artifact's uuids and greedily
    assigns each grazing chord the cheapest clean treatment -- prefer a
    flip (which keeps the chord 45-aligned), fall back to a skip -- so the
    remediation tracks the geometry, not a run-specific uuid.  Returns
    ``(axis_first_uuids, skip_uuids)`` ready for ``quantize_pcb_file``.
    """
    import os
    import shutil
    import tempfile

    from kicad_tools.router.quantize import quantize_pcb_file

    def _new_grazes(axis_first: set[str], skip: set[str]) -> set[tuple[str, ...]]:
        fd, tmp = tempfile.mkstemp(suffix=".kicad_pcb")
        os.close(fd)
        try:
            shutil.copy(pre_quantize_path, tmp)
            quantize_pcb_file(
                tmp,
                axis_first_uuids=frozenset(axis_first),
                skip_uuids=frozenset(skip),
            )
            return _clearance_signature(Path(tmp)) - baseline
        finally:
            os.unlink(tmp)

    # ``dry_run`` lists every off-angle chord's uuid without mutating the file.
    off_angle = quantize_pcb_file(pre_quantize_path, dry_run=True)
    axis_first: set[str] = set()
    skip: set[str] = set(seed_skip_uuids)

    # Greedy fixpoint: each iteration commits at most one new treatment that
    # strictly reduces the NEW-graze count; bounded by the off-angle count.
    for _ in range(len(off_angle) + 1):
        remaining = _new_grazes(axis_first, skip)
        if not remaining:
            break
        chosen: tuple[str, str] | None = None
        for u in off_angle:
            if u in axis_first or u in skip:
                continue
            if len(_new_grazes(axis_first | {u}, skip)) < len(remaining):
                axis_first.add(u)
                chosen = ("flip", u)
                break
            if len(_new_grazes(axis_first, skip | {u})) < len(remaining):
                skip.add(u)
                chosen = ("skip", u)
                break
        if chosen is None:
            # No single-segment flip/skip reduces the residual -- let the
            # step-12d gate report it as a hard failure (a genuinely new,
            # non-corridor short that a re-route must resolve).
            break
        print(f"   auto-remediate ({chosen[0]}) off-angle chord {chosen[1]} (#3913)")
    return axis_first, skip


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """Route the PCB with per-protocol net-class engagement.

    Wires the protocol-specific NetClassRouting instances from
    ``build_net_class_map()`` into the autorouter so each Phase 1-3
    feature is exercised on the appropriate pair set.
    """
    import random

    # Issue #3880: reuse the CLI's deterministic-budget constants so a future
    # tune of the iteration cap is single-source (route_cmd.py), rather than
    # hardcoding 12M / 1M here.
    from kicad_tools.cli.route_cmd import (
        DETERMINISTIC_BUDGET_MAX_SEARCH_ITERATIONS,
        DETERMINISTIC_BUDGET_PER_NET_ITERATIONS,
    )
    from kicad_tools.router import DesignRules, DifferentialPairConfig, load_pcb_for_routing
    from kicad_tools.router.optimizer import (
        GridCollisionChecker,
        OptimizationConfig,
        TraceOptimizer,
        optimize_routes_grid_synced,
    )

    print("\n" + "=" * 60)
    print("Routing PCB...")
    print("=" * 60)

    # JLCPCB tier-1 design rules: 0.15mm trace / 0.15mm space / 0.3mm via.
    # Grid must be <= clearance/2 for DRC compliance (0.05 <= 0.15/2 = 0.075 OK).
    # Via diameter chosen tight (0.45mm) so escape vias fit between
    # the 1.0mm-pitch BGA pads without blocking adjacent pad access.
    # This is the same tier the Phase 3K impedance formulas were calibrated
    # against, so the router consumes the same stackup the DRC will check.
    #
    # Issue #3313 -- impedance-driven sizing + neck-down taper:
    #   * ``manufacturer="jlcpcb"`` activates the validator's JLCPCB
    #     impedance regex defaults (synthesised onto net classes that
    #     do not declare an explicit ``target_*_impedance``).
    #   * ``min_trace_width=0.1016`` (4 mil = 0.1016 mm, the exact JLCPCB
    #     4-layer 1oz minimum) enables the existing neck-down taper
    #     mechanic so the escape router can fit narrow traces through
    #     fine-pitch BGA-49 / QFN / FFC corridors while the corridor-region
    #     trace widens back to the impedance-resolved width (~0.39 mm for
    #     50 Ω SE / ~0.20 mm for 100 Ω diff, etc.).  Without this, enabling
    #     the impedance resolver would brick the BGA escapes (the 0.39 mm
    #     wide trace does not fit through the 0.5 mm pad-pitch escape
    #     channel).  Issue #3740: this was previously 0.10 mm, which
    #     clamped neck-down escapes to exactly 0.100 mm -- 1.6 µm below the
    #     real 4 mil floor -- and tripped kicad-cli's ``track_width`` rule.
    #     Using the exact 0.1016 mm keeps the escapes legal under both
    #     DRC engines.
    rules = DesignRules(
        grid_resolution=0.05,
        trace_width=0.15,
        trace_clearance=0.15,
        via_drill=0.25,
        via_diameter=0.45,
        manufacturer="jlcpcb",
        min_trace_width=0.1016,
        neck_down_distance=1.0,
        neck_down_threshold=0.8,
    )

    # Power and ground nets are handled via copper pours on the inner planes
    # (In1.Cu = GND, In2.Cu = PWR).  Skip them at the trace router so they
    # don't fight for outer-layer corridors.  POUR_NETS is module-level so
    # the CI gate's reach assertion derives the signal-net universe from
    # the same declaration (Issue #3413 phase 5).
    skip_nets = list(POUR_NETS)

    print(f"\n1. Loading PCB: {input_path}")
    print(
        f"   Grid: {rules.grid_resolution}mm  Trace: {rules.trace_width}mm  Clearance: {rules.trace_clearance}mm"
    )
    print(f"   Skipping pour nets: {skip_nets}")

    # Issue #3880: construct the Autorouter with the deterministic iteration
    # budget (the same knobs ``kct route --deterministic-budget`` sets via
    # route_cmd._normalize_deterministic_budget).  Board 06 is the one board
    # whose main pass routes IN-PROCESS via ``route_all_negotiated`` rather
    # than the ``kct route`` CLI, so it cannot inherit the CLI flag -- the
    # iteration cap must be wired through the constructor.  This bounds each
    # per-net A* by a fixed node-expansion count instead of the wall-clock
    # ``per_net_timeout`` (set to 0.0 in _negotiated_non_diffpair_strategy
    # below), so the seed-42 route is reproducible regardless of runner load
    # -- removing the load-dependence that flaked the board-06 re-route gate.
    router, net_map = load_pcb_for_routing(
        str(input_path),
        skip_nets=skip_nets,
        rules=rules,
        max_search_iterations=DETERMINISTIC_BUDGET_MAX_SEARCH_ITERATIONS,
        per_net_iterations=DETERMINISTIC_BUDGET_PER_NET_ITERATIONS,
    )

    # Install per-protocol net classes.  The router consumes:
    #   - intra_pair_clearance via effective_intra_pair_clearance()
    #     in pathfinder.py / cpp_backend.py
    #   - coupled_routing as the opt-in gate for CoupledPathfinder
    #   - target_diff_impedance via apply_impedance_driven_sizing()
    #   - skew_tolerance_mm via DiffPairLengthTracker / Phase 3I
    #     serpentine (when #2648 lands)
    #   - coupled_continuity_threshold via the DRC rule (passed to
    #     DiffPairRoutingContinuityRule.threshold_map)
    net_class_map = build_net_class_map()

    # Apply impedance-driven sizing (Phase 3K integration point).
    # ``resolve_impedance_for_net_classes`` walks the net-class map and
    # replaces each class whose ``target_diff_impedance`` or
    # ``target_single_impedance`` is set with a copy whose ``trace_width``
    # / ``intra_pair_clearance`` reflect the impedance solver's output for
    # the configured stackup.
    #
    # Issue #3313 -- impedance sizing ENABLED (was disabled prior to PR
    # closing #3313):
    #
    # The pre-fix scaffold disabled the resolver because the impedance
    # solver produces wide traces (~0.39 mm for 50 Ω single-ended on F.Cu
    # with the JLCPCB tier-1 stackup) which do not fit through the dense
    # pad-pitch corridors at the BGA-49, QFN, and FFC connectors.
    # Routing succeeded only on 3/21 nets when the resolved widths were
    # applied uniformly.  The PR #3273 / #3315 trap then bit the next
    # author: ``kct check`` WITHOUT ``--net-class-map`` made the dormant
    # path look fine (3 errors) while a sidecar check revealed ~600
    # impedance violations.
    #
    # The fix that lets this be ``True``:
    #
    #   1. ``DesignRules`` now declares ``min_trace_width=0.10`` and the
    #      escape router already uses that width for fine-pitch BGA-49 /
    #      QFN escape segments (escape.py:3303-3304, Issue #1778), so the
    #      escape geometry succeeds even when the corridor trace is
    #      ~0.39 mm wide.
    #
    #   2. ``DesignRules.get_neck_down_width`` (rules.py:498) now accepts
    #      a ``base_width`` parameter, and the pathfinder passes the
    #      per-net-class ``trace_width`` (the impedance-resolved width)
    #      as the corridor base.  Segments taper from the impedance
    #      width down to ``min_trace_width`` (0.10 mm) within
    #      ``neck_down_distance`` of fine-pitch pads (#3313).
    #
    #   3. ``manufacturer="jlcpcb"`` is set so the resolver's clamp path
    #      and validator regex defaults reference the same fab tier the
    #      stackup ``Stackup.jlcpcb_4layer()`` represents.
    #
    # Heed PR #3273 -- the strict CI gate at
    # ``scripts/ci/check_routed_drc.py`` runs WITH the net-class sidecar
    # (Issue #3151), so the impedance count IS the gate.  This recipe
    # must keep ``APPLY_IMPEDANCE_DRIVEN_SIZING = True`` and the
    # impedance-resolved widths must land on the F.Cu signal nets.
    APPLY_IMPEDANCE_DRIVEN_SIZING = True
    if APPLY_IMPEDANCE_DRIVEN_SIZING:
        try:
            from kicad_tools.manufacturers import get_profile
            from kicad_tools.physics.stackup import Stackup
            from kicad_tools.router.diffpair_impedance import (
                resolve_impedance_for_net_classes,
            )

            stackup = Stackup.jlcpcb_4layer()
            mfr_profile = get_profile("jlcpcb")
            mfr_rules = mfr_profile.get_design_rules(layers=4, copper_oz=1.0)

            resolved_map, mismatch_warnings, clamp_errors = resolve_impedance_for_net_classes(
                net_class_map,
                stackup=stackup,
                design_rules=mfr_rules,
                layer="F.Cu",
            )

            # Issue #3313 -- preserve recipe-author's tight ``intra_pair_clearance``.
            #
            # The resolver computes a (trace_width, intra_pair_clearance)
            # pair from the coupled-lines model that maintains the target
            # differential impedance.  On the JLCPCB tier-1 4-layer
            # stackup the bisection picks a "loosely coupled" geometry
            # (~8 mm gap with ~0.475 mm widths for 90/100 Ω diff).  That
            # gap is correct physics, but it disables Phase 2E coupled
            # routing on board 06 (the gap exceeds the available diff-
            # pair fabric width and the gap-coupled routing collapses to
            # independent per-net routing).
            #
            # The recipe deliberately declares ``intra_pair_clearance``
            # = 0.075-0.10 mm on USB/USB3/PCIe/MIPI for tightly-coupled
            # diff-pair routing -- an explicit author choice that takes
            # precedence over the solver's "loosely coupled" branch.
            # Re-apply the recipe's per-class ``intra_pair_clearance``
            # (and ``clearance``) on each resolved class.
            #
            # Issue #3413 (phase 6) -- re-solve the WIDTH on the
            # tightly-coupled branch instead of keeping the resolver's
            # loosely-coupled width.  The historical combination
            # ("resolver width 0.475 mm + recipe gap 0.10 mm") was
            # physically inconsistent (measured Zdiff ~62 Ω vs the 90 Ω
            # target, 31% off) AND operationally toxic: a 0.475 mm trace
            # needs 0.775 mm of fabric (width + 2x0.15 clearance), which
            # exceeds J1's 0.7 mm inter-pad channel -- the USB-C fan-out
            # was geometrically SEALED for any net that had to thread
            # between J1 pads, and the A* search at that width was so
            # constrained that USB3_RX1- needed 27 s on an EMPTY board
            # (vs the 30 s per-net budget).  This is the root cause of
            # the USB3_RX1- residual diagnosed in PR #3500 ("hard
            # obstacles at the J1 fan-out").
            #
            # Given the author-chosen tight gap, solve
            # ``Zdiff(width, gap) = target`` for width via bisection on
            # the same ``CoupledLines.edge_coupled_microstrip`` model the
            # resolver uses.  Measured solutions on jlcpcb_4layer F.Cu:
            #   USB2  90 Ω @ 0.075 mm gap -> 0.250 mm (Zdiff 89.6, 0.4% off)
            #   USB3  90 Ω @ 0.100 mm gap -> 0.275 mm (Zdiff 90.4, 0.4% off)
            #   PCIe 100 Ω @ 0.100 mm gap -> 0.225 mm (Zdiff 102.3, 2.3% off)
            #   MIPI 100 Ω @ 0.100 mm gap -> 0.225 mm (Zdiff 102.3, 2.3% off)
            # All are within the classes' 10% tolerance; 0.275 + 0.3 mm
            # clearance = 0.575 mm fits J1's 0.7 mm channels and the
            # BGA-49's 0.82 mm channels with margin.  Single-ended
            # classes (Sideband) keep the resolver's width -- the
            # sidecar DRC gate validates SE impedance against it.
            import dataclasses as _dc

            from kicad_tools.physics import CoupledLines as _CoupledLines

            _cl = _CoupledLines(stackup)

            def _tightly_coupled_width(
                target_zdiff: float, gap_mm: float, fallback: float
            ) -> float:
                """Bisect Zdiff(width, gap)=target on F.Cu; round to 0.025mm."""
                try:
                    lo, hi = 0.1, 1.0
                    for _ in range(40):
                        mid = (lo + hi) / 2
                        z = _cl.edge_coupled_microstrip(mid, gap_mm, "F.Cu").zdiff
                        if z > target_zdiff:
                            lo = mid
                        else:
                            hi = mid
                    w = round(round(((lo + hi) / 2) / 0.025) * 0.025, 4)
                    return max(w, rules.min_trace_width or w)
                except (ValueError, AttributeError):
                    return fallback

            for _name, _resolved_nc in list(resolved_map.items()):
                _original_nc = net_class_map.get(_name)
                if _original_nc is None:
                    continue
                _width = _resolved_nc.trace_width
                if (
                    _original_nc.target_diff_impedance is not None
                    and _original_nc.intra_pair_clearance is not None
                ):
                    _width = _tightly_coupled_width(
                        _original_nc.target_diff_impedance,
                        _original_nc.intra_pair_clearance,
                        _resolved_nc.trace_width,
                    )
                resolved_map[_name] = _dc.replace(
                    _resolved_nc,
                    trace_width=_width,
                    intra_pair_clearance=_original_nc.intra_pair_clearance,
                    clearance=_original_nc.clearance,
                )

            net_class_map = resolved_map
            print("   Impedance sizing applied (stackup: jlcpcb_4layer)")
            print("   Recipe intra_pair_clearance preserved; diff widths re-solved tightly-coupled")
            for _name in ("USB2_D+", "USB3_RX1-", "PCIE_TX+", "MIPI_CLK+", "MIPI_RST"):
                _nc = net_class_map.get(_name)
                if _nc is not None:
                    print(
                        f"     {_name}: width={_nc.trace_width:.3f}mm "
                        f"gap={_nc.intra_pair_clearance}"
                    )
            if mismatch_warnings:
                print(f"   Stackup mismatch warnings: {len(mismatch_warnings)}")
            if clamp_errors:
                print(f"   Impedance clamp diagnostics: {len(clamp_errors)}")
        except Exception as exc:  # pragma: no cover - degrade gracefully
            print(f"   Impedance sizing skipped: {exc}")
    else:
        print("   Impedance sizing: declared on net classes but not applied to trace widths")
        print("   (resolved widths exceed pad-pitch corridors; see generate_design.py for details)")

    router.net_class_map.update(net_class_map)

    # Engaged pairs --- the diff pair detector (#2558) uses this list as
    # the AUTHORITATIVE pair declarations (overrides suffix inference
    # and KiCad DiffPair group annotations).
    print(f"\n2. Net classes installed: {len(net_class_map)} entries")
    print(f"   Diff pairs declared: {len(generate_pcb.DIFFPAIRS)}")

    print(f"\n3. Board: {router.grid.width}mm x {router.grid.height}mm")
    print(f"   Nets loaded: {len(net_map)}")

    print("\n4. Routing nets...")
    # Issue #3071 (follow-up to #3040 / PR #3069 board-03 migration):
    # route the 9 declared pairs through the diff-pair-aware entry
    # point so Phase A (``CoupledPathfinder``) populates the intra-pair
    # clearance buffer and Phase B
    # (``repair_intra_clearance_violations``) can widen
    # ``min_spacing_cells`` on any pair whose coupled route quantises
    # to a clearance violation.  Previously this board called the
    # per-net ``router.route_all()`` directly, leaving
    # ``CoupledPathfinder`` unrun -- so the entire Phase B repair pass
    # was unreachable on this in-tree board even though the underlying
    # mechanism was sound.
    #
    # An earlier attempt at this migration (see the closed comment on
    # #3071) regressed catastrophically (32 -> 36,236 DRC errors) because
    # ``CoupledPathfinder``'s A* state was keyed only on the endpoint
    # ``(p_pos, n_pos)`` pair; the asymmetric P-advance / N-advance
    # moves added in #2490 let one trace loop around its partner and
    # re-converge from the opposite side at full spacing.  That
    # underlying defect is fixed by PR #3083 / issue #3078, which
    # threads ``p_visited`` / ``n_visited`` path-history sets through
    # all three coupled-move branches and rejects cross-trail and
    # self-loop landings.  With that guard in place this migration is
    # safe to land.
    #
    # Seed=42 makes the resulting routed PCB deterministic so the
    # per-board DRC floor in ``.github/routed-drc-tolerance.yml``
    # reflects a reproducible artifact rather than a lucky one-shot.
    # This mirrors the seed plumbing PR #3065 added to
    # ``route_all_negotiated``; ``route_all_with_diffpairs`` does not
    # (yet) accept a seed kwarg directly, so we pre-seed the global RNG
    # which is what the diff-pair pre-pass and the inner per-net A*
    # loop both consult.
    #
    # Issue #3089: per-pair wall-clock budget for the inner
    # ``CoupledPathfinder.route_coupled`` A*.  Two prior seed=42
    # attempts (96 min and 25 min wall-clock) stalled inside the
    # coupled pathfinder on the USB3_RX2+/USB3_RX2- BGA-49 escape
    # at J3 / J4 -- 6 of 9 pairs routed in the first ~5 min, then the
    # 7th pair consumed the entire remaining budget without
    # converging.  With the per-pair budget below, each pair gets at
    # most ``per_pair_timeout`` seconds of coupled A*; pairs that
    # exceed the budget fall through to the independent per-net
    # router (which the per-net A* C++ backend accelerates 10-100x).
    # The prior open-ended runs reported 6 of 9 pairs converging
    # in coupled mode in the first ~5 min of wall-clock; the failing
    # pairs (USB3 SS BGA-49 escapes at J3/J4 and several MIPI nets)
    # consume the entire remaining budget without converging.  We
    # set a tight per-pair budget so the diff-pair phase finishes
    # quickly and the C++-backed per-net A* in the main strategy
    # picks up the deferred pairs.  Pairs that hit the budget are
    # surfaced via a logger.warning + ``diffpair coupled-routing
    # budget exceeded`` diagnostic and their nets are dropped from
    # ``diff_net_ids`` so the main strategy routes them as ordinary
    # nets.  Empirical observation: pairs that converge coupled do
    # so in 0.05 -- 30 s; budget exits run for the full budget then
    # defer.  A 30s budget bounds the diff-pair phase at 9 x 30 = 270s,
    # leaving 330s for the per-net pass + optimisation + DRC nudge
    # under the 600s ``timeout`` AC criterion of #3089.
    random.seed(42)

    # Issue #3089: per-pair wall-clock budget for the inner
    # ``CoupledPathfinder.route_coupled`` A*.  Two prior seed=42
    # attempts (96 min and 25 min wall-clock) stalled inside the
    # coupled pathfinder on the USB3 SS BGA-49 escape at J3/J4 --
    # 6 of 9 pairs route in the first ~5 min, then the failing
    # pairs consume the entire remaining budget without converging.
    # The 30s per-pair budget bounds the diff-pair phase at
    # 9 x 30 = 270s.  Pairs that exceed the budget surface a
    # ``diffpair coupled-routing budget exceeded`` diagnostic, are
    # excluded from ``diff_net_ids``, and are picked up by the
    # non-diff-pair main strategy as ordinary nets.  When all 9
    # converge coupled (the happy path), the diff-pair phase is
    # observed at well under the budget.
    # Issue #3144: also set a per-pair iteration budget alongside the
    # wall-clock budget.  The iteration budget is the deterministic
    # classifier -- it fires at the same iteration count regardless of
    # CPU speed -- while the wall-clock budget remains as a safety net
    # against pathological cases where memory pressure or grid layout
    # makes an iteration extremely slow.
    #
    # Empirical calibration (commit 0bbe29a7, local 8-core M-series):
    # the 9 board 06 pairs that exit the per-pair budget at the 30s
    # wall-clock currently reach 3456-19968 iterations.  A pair that
    # WAS going to converge inside the budget consumes <2000
    # iterations (the historical "6 of 9 succeed in the first 5 min"
    # case from Issue #3089).  Picking ``per_pair_max_iterations=4000``
    # therefore preserves the budget-classification intent (slow pairs
    # defer to the main strategy) while making that classification
    # reproducible across CPU speeds: a 2-core CI runner reaches the
    # same iteration ceiling as a 16-core dev machine, just slower
    # in wall-clock terms.
    #
    # ``per_pair_timeout=60.0`` is the safety net.  Doubled from the
    # historical 30.0s value because on a 2-core CI runner the
    # iteration budget can take ~45s to reach -- the wall-clock budget
    # must be > that or it would fire first and re-introduce the
    # timing-dependent classification we are eliminating.  When the
    # iteration budget fires (the deterministic path), wall-clock is
    # always well under 60s so this is a safety net only.
    # Issue #3508: per_pair_timeout 60 -> 120 and per_pair_max_iterations
    # 4000 -> 2000.  The coupled pre-phase is no longer search-dominated:
    # the geometric shadow constructor (guide + validated parallel offset,
    # see diffpair_routing._shadow_route_pair) converges 6-7/9 pairs in
    # 0.3-75s each, and the joint-state A* is only a last-resort fallback
    # -- so its iteration budget SHRINKS (it essentially never converges
    # on this board; 2000+2000 iterations bound the fallback at ~10-30s).
    # The wall-clock budget GROWS because the corridor/shadow guide probe
    # needs up to ~45s for the USB3 J1 fan-out (the C++ validation falls
    # back to the Python pathfinder there) and the budget must cover
    # probe + shadow + swapped-probe + bounded fallback searches.
    # Issue #3508 (decomposition): the geometric shadow constructor is
    # kept OFF for this recipe.  Set ``KCT_BOARD06_SHADOW=1`` to reproduce
    # the shadow-ON run (the constant defaults False so the committed
    # artifact and CI stay on the pre-#3508 budget-exit behaviour).
    #
    # Issue #3921 (2026-07-08 re-scope + re-measure): two of the three
    # original artifact-quality defects are now FIXED on main -- stranded
    # shadow tails (#3665 transactional connectivity rollback) and shadow
    # vias intersecting the partner (#3667 full-polyline via validation).
    # The re-scoped corridor-competition defect did NOT reproduce on the
    # current (#3413-phase-6, tightened-width) geometry: the shadow-ON
    # seed-42 re-run reached 15/15 single-ended nets at its best
    # negotiated snapshot (MIPI_D0-/USB_CC1 not stranded).  But shadow-ON
    # is still not shippable here for two deeper reasons:
    #   * Convergence collapsed 6-7/9 -> 3/9.  The 0.225-0.275 mm coupled
    #     widths make the geometric parallel offset infeasible for 6/9
    #     pairs (self-check overlap -0.165..-0.275 mm; mid-route blockage).
    #   * The surviving shadow segments are 3.7-11.9 deg off-angle
    #     (#3975 OffAngleSegmentWarning -> would fail the 45-census if
    #     committed) and the 6 fallbacks blow the wall-clock (>1200 s vs
    #     ~150 s / 21-21 shadow-OFF; the negotiated 360 s backstop fires).
    # Enabling by default needs a shadow-aware by-construction dogleg
    # (#3907/#3975) plus a parallel-offset feasibility fix at the tight
    # widths.  When BOTH land, flip the env default and the optimizer/nudge
    # diff-pair protections below together.  See #3921 for the full data.
    import os as _os

    ENABLE_COUPLED_SHADOW = _os.environ.get("KCT_BOARD06_SHADOW", "0") == "1"

    diffpair_config = DifferentialPairConfig(
        enabled=True,
        per_pair_timeout=120.0,
        per_pair_max_iterations=2000,
        enable_shadow_construction=ENABLE_COUPLED_SHADOW,
    )

    # Issue #3089: route the non-diff-pair tail (including any
    # budget-exit-deferred pairs) via ``Autorouter.route_all_negotiated``
    # so the per-net A* honours the wall-clock ``per_net_timeout``
    # bound.  Bare ``route_all`` only treats ``per_net_timeout`` as
    # advisory and the BGA-49-escape nets on board 06 can monopolise
    # the run for 5+ minutes per net (see #2794).  ``route_all_negotiated``
    # is the strategy ``route_all`` recommends for dense boards and
    # it composes with the diff-pair pre-pass via the
    # ``non_diffpair_strategy`` callable hook (Issue #2464).
    # Issue #3413 (phase 3): the 240s budget was A/B-measured against
    # 360s and KEPT -- under the OLD loosely-coupled 0.475mm widths.  At
    # those widths 360s banked a routed=21/connected=20 snapshot whose
    # extra copper was a PARTIAL USB3_RX1- route carrying a heavy
    # violation load (DRC 36 vs the 9-13 band at 240s).
    #
    # Issue #3413 (phase 6) RE-MEASURED after the tightly-coupled width
    # re-solve (0.225-0.275mm): the premise of the 240s choice no longer
    # holds.  USB3_RX1- now connects FULLY inside iteration 1-2 (21/21
    # connected at ~40s wall), so there is no partial-route snapshot for
    # the lex tuple to bank.  The loop's remaining work is overflow
    # relief (a 2-cell USB_CC1 residual at the J1 fan-out): at 240s the
    # timeout fires mid-iteration-4 right after the oscillation-escape
    # finds an overflow 9 -> 0 strategy, restoring the iter-2
    # overflow=2 snapshot.  360s gives the loop the room to BANK the
    # overflow-0 state instead of just discovering it.
    def _negotiated_non_diffpair_strategy() -> list:
        # Issue #3880: disable the per-net WALL-CLOCK cutoff (was 30.0s).  The
        # per-net A* search is now bounded by the deterministic ITERATION
        # budget wired into the Autorouter above (per_net_iterations=1M), which
        # the CLI's --deterministic-budget path sets identically -- so this
        # in-process route matches the CLI boards (02/03/04/05/07) and is
        # reproducible regardless of runner load.
        #
        # ``per_net_timeout=None`` (NOT 0.0): the negotiated/MST routers treat
        # per_net_timeout as a cumulative wall-clock DEADLINE
        # (``time.monotonic() + per_net_timeout``); a literal 0.0 would make
        # every edge's remaining budget <= 0 and record all nets as failures.
        # ``None`` disables the deadline entirely.  This mirrors the CLI, which
        # sets args.per_net_timeout = 0.0 but forwards
        # ``getattr(args, "per_net_timeout", None) or None`` -> None
        # (route_cmd.py:3272 etc.).
        #
        # ``timeout=360.0`` is retained as an outer SAFETY BACKSTOP only -- it
        # must NOT fire, or it re-introduces the load-dependent cutoff the
        # iteration budget removes.  The negotiated loop's banking work
        # completes well under 360s on this board (see the #3413 phase-6 note
        # above).
        return router.route_all_negotiated(
            per_net_timeout=None,
            timeout=360.0,
            seed=42,
        )

    # Issue #3508: an A/B run that PRE-ROUTED the two chronically-
    # stranded singles (USB_CC1, MIPI_D0-) before the coupled pre-phase
    # was measured and REJECTED: their single-ended A* claims the prime
    # J1/FFC corridors and coupled convergence collapses 6/9 -> 2/9
    # (the shadow constructor needs those corridors).  The coupled
    # phase must claim corridors first; stranded-single residuals are
    # handled by the stub-edge deferral and the post-routing repair
    # passes instead.
    router.route_all_with_diffpairs(
        diffpair_config=diffpair_config,
        non_diffpair_strategy=_negotiated_non_diffpair_strategy,
    )

    stats_raw = router.get_statistics()
    print(
        f"   Raw: {stats_raw['routes']} routes / {stats_raw['segments']} segments / {stats_raw['vias']} vias"
    )

    print("\n5. Optimizing traces...")
    opt_config = OptimizationConfig(
        merge_collinear=True,
        eliminate_zigzags=True,
        compress_staircase=True,
        convert_45_corners=True,
        minimize_vias=True,
    )
    collision_checker = GridCollisionChecker(router.grid)
    optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

    # Issue #3507: grid-transactional optimize -- each mutated route's old
    # copper is unmarked and the new copper marked, so the optimizer's own
    # collision checking and every downstream grid consumer (the nudge
    # pass, step 6b's transactional solo re-route repair) see the TRUE
    # copper state instead of the pre-optimization snapshot.
    #
    # Issue #3508: when the coupled shadow constructor is ON, diff-pair
    # nets are EXCLUDED from optimization.  The coupled pre-phase's
    # geometry is intentional: length-matching serpentines are exactly
    # the "zigzags" ``eliminate_zigzags`` removes (measured: PCIE_RX
    # skew 0.097mm post-serpentine -> 1.652mm in the optimized
    # artifact), and straightening one side of a coupled pair breaks
    # the constant-gap geometry the skew/continuity rules measure.
    # With the shadow OFF (current state, see ENABLE_COUPLED_SHADOW)
    # all pairs are single-ended fallback routes with no intentional
    # coupled geometry, so they stay in the optimizer/nudge scope and
    # the committed artifact is unchanged.
    diffpair_net_ids: set[int] = (
        {
            r.net
            for r in router.routes
            if r.net_name and (r.net_name.endswith("+") or r.net_name.endswith("-"))
        }
        if ENABLE_COUPLED_SHADOW
        else set()
    )
    optimize_routes_grid_synced(
        router, optimizer, skip_nets=diffpair_net_ids if diffpair_net_ids else None
    )

    # Issue #2757: Run the DRC verify-and-nudge pass after trace optimisation.
    # The optimiser can produce chamfered diagonals that graze BGA / QFN /
    # USB-C pads on skipped pour nets (GND, +3V3, +1V2); the in-memory
    # ``drc_verify_and_nudge`` pass surfaces those as ``clearance_pad_segment``
    # candidates and nudges segments perpendicular to repair them.  Without
    # this call the post-route ``kct check`` is the first thing that sees
    # the violations -- by which point the routed PCB is already serialised.
    # See also the equivalent invocations in ``kct route`` (route_cmd.py:1985
    # and 2511) and ``kct optimize`` (route_cmd.py:5184).
    from kicad_tools.router.drc_nudge import drc_verify_and_nudge

    print("\n6. DRC verify-and-nudge pass...")
    # Issue #3508: when the coupled shadow constructor is ON, diff-pair
    # nets are protected from the nudge pass for the same reason they
    # skip the optimizer above -- the nudge helpers are not
    # partner-aware, and a 0.2mm displacement at the pairs'
    # 0.075-0.1mm intra gap lands copper ON the partner (measured:
    # USB3_RX1/RX2 sides physically overlapping post-nudge, forcing the
    # 6b solo rip which then destroys the coupled geometry).  With the
    # shadow OFF, ``diffpair_net_ids`` is empty (see above) and the
    # nudge pass keeps its full scope.
    nudge_result = drc_verify_and_nudge(
        router, skip_nets=diffpair_net_ids if diffpair_net_ids else None
    )
    if nudge_result.initial_violations:
        print(f"   {nudge_result.summary()}")
    else:
        print("   No in-router DRC violations detected")

    # Issue #3413 phase 6 (residual cleanup): surgical intra-pair
    # overlap repair -- AFTER the optimizer + nudge passes, which is
    # where the overlap is actually introduced (measured on seed 42: at
    # the post-route stage the pair copper is overlap-free; after
    # optimization/nudge USB3_RX1- copper rides USB3_RX1+ segments + a
    # via at the J1 fan-out, producing 4 diffpair_clearance_intra + 4-6
    # clearance_segment_via errors at identical coordinates run-to-run).
    # For each declared pair whose two sides have physically overlapping
    # copper, rip the side with fewer segments and re-route it SOLO --
    # single-net A* treats all committed copper as hard obstacles, so a
    # successful re-route is overlap-free by construction.
    # Transactional: if the solo route fails or still overlaps, the
    # original routes are restored.
    print("\n6b. Surgical intra-pair overlap repair...")
    try:
        repaired_pairs = _repair_pair_overlap_solo(router, net_map)
        if repaired_pairs:
            print(f"   Re-routed {len(repaired_pairs)} pair side(s): {repaired_pairs}")
        else:
            print("   No physically-overlapping pair sides detected")
    except Exception as exc:  # pragma: no cover - degrade gracefully
        print(f"   WARNING: pair-overlap repair skipped: {exc}")

    stats = router.get_statistics()
    print(
        f"\n7. Final: {stats['routes']} routes / {stats['segments']} segments / {stats['vias']} vias"
    )
    print(f"   Total length: {stats['total_length_mm']:.2f}mm")
    print(f"   Nets routed: {stats['nets_routed']}")

    # Stitch routes back into the unrouted PCB.
    original_content = input_path.read_text()
    route_sexp = router.to_sexp()

    if route_sexp:
        output_content = original_content.rstrip().rstrip(")")
        output_content += "\n"
        output_content += f"  {route_sexp}\n"
        output_content += ")\n"
    else:
        output_content = original_content
        print("   Warning: No routes generated!")

    output_path.write_text(output_content)
    print(f"\n8. Routed PCB: {output_path}")

    # Issue #3828: emit the net-class-map sidecar next to the routed PCB so
    # the recipe's own DRC gate (run_drc) -- and the CI coverage gate -- can
    # pass ``--net-class-map`` to ``kct check``.  Without the sidecar the
    # ``diffpair_length_skew``, ``diffpair_routing_continuity`` and
    # ``match_group_length_skew`` rules silently degrade to no-ops (kct warns
    # on stderr) and never validate length-match skew / coupled continuity.
    # Mirrors the board-03 pattern (boards/03-usb-joystick/generate_design.py
    # ~L498-516).  ``net_class_map`` here is the SAME object the router
    # consumed above, so the sidecar reflects the exact engagement state the
    # board was routed under.
    import json as _json

    from kicad_tools.router.rules import net_class_map_to_dict

    sidecar_path = output_path.parent / "net_class_map.json"
    sidecar_path.write_text(_json.dumps(net_class_map_to_dict(net_class_map), indent=2))
    print(f"   Wrote net-class-map sidecar: {sidecar_path}")

    # Issue #2835: emit copper-pour zones for GND + power nets so the
    # net-status report doesn't flag pour-net pads as "incomplete".
    #
    # Issue #3413 phase 4: layer assignment is now RECIPE-LOCAL instead
    # of ``auto_create_zones_for_pour_nets``'s allocator.  The allocator
    # puts the 2nd..Nth power nets on F.Cu -- but board 06's F.Cu
    # carries ~270 signal segments (the entire diff-pair fabric), so the
    # F.Cu pours fragmented into pad-island slivers and the +3V3/+1V8/
    # +1V2 "planes" were dead copper (the #3482 analyzer gap hid this:
    # a pad inside a zone *boundary* counts as connected even when the
    # zone has zero/islanded fill).  B.Cu carries ~13 segments and
    # In2.Cu ~16 on this board, so the power rails pour there:
    #   GND      -> In1.Cu p1 (full board outline -- the return plane)
    #   VBUS_USB -> In2.Cu p1 (pad bbox: its 5 pads are all top-left)
    #   +1V2     -> In2.Cu p2 (pad bbox -- the split-PWR-plane intent
    #               from generate_pcb's stackup comment).  Keeping +1V2
    #               OFF B.Cu is deliberate: its bbox covers the whole
    #               BGA-49 field, and as a B.Cu zone it carved the +3V3
    #               pour away from the BGA's +3V3 corner pads (C2/C6/
    #               E2/E6), leaving them enclaved behind a via-crowded
    #               corridor that no straight repair bridge could cross
    #               (measured: 3 unrepairable pads).  On In2.Cu the
    #               +1V2 region coexists with VBUS instead, and the
    #               BGA-area B.Cu stays +3V3-contiguous.
    #   +1V8     -> B.Cu  p2 (pad bbox)
    #   +3V3     -> B.Cu  p1 (pad bbox minus the +1V8 strip)
    # ``_compute_pour_outlines`` (#2771/#3043/#3240) carves shared-layer
    # outlines so they are geometrically disjoint.
    print("\n9. Generating copper-pour zones (recipe-local layer plan)...")
    try:
        from kicad_tools.zones.generator import (
            ZoneGenerator,
            _compute_pour_outlines,
        )

        zone_assignments: list[tuple[str, str, int]] = [
            ("GND", "In1.Cu", 1),
            ("VBUS_USB", "In2.Cu", 1),
            ("+1V2", "In2.Cu", 2),
            ("+1V8", "B.Cu", 2),
            ("+3V3", "B.Cu", 1),
        ]
        # JLCPCB minimum mask-to-copper clearance is ~0.2mm; inset by
        # 0.5mm for a conservative margin.
        zone_gen = ZoneGenerator.from_pcb(output_path, edge_clearance=0.5)
        pour_outlines = _compute_pour_outlines(
            zone_gen.pcb, zone_assignments, zone_gen.board_outline
        )
        for zone_net, zone_layer, zone_priority in zone_assignments:
            zone_gen.add_zone(
                net=zone_net,
                layer=zone_layer,
                priority=zone_priority,
                boundary=pour_outlines.get(zone_net),
            )
        zone_gen.save(output_path)
        print(f"   Created {len(zone_assignments)} zone(s): {zone_assignments}")
    except Exception as exc:  # pragma: no cover - degrade gracefully
        print(f"   Zone generation skipped: {exc}")

    # Issue #3413 phase 4: FILL the zones.  The historical recipe never
    # invoked the filler, so the committed artifact had zone boundaries
    # with zero ``filled_polygon`` copper -- every pour net's
    # connectivity was a boundary-test illusion (#3482).  The stitcher
    # and the copper-union audit below need real fill geometry.
    print("\n9b. Filling zones (first pass)...")
    fill_argv = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "zones",
        "fill",
        str(output_path),
    ]
    fill_result = subprocess.run(fill_argv, capture_output=True, text=True)
    first_fill_ok = fill_result.returncode == 0
    if first_fill_ok:
        for line in fill_result.stdout.strip().split("\n")[-4:]:
            print(f"   {line}")
    else:
        print(f"   Zone fill failed (rc={fill_result.returncode}):")
        if fill_result.stderr:
            print(f"   stderr: {fill_result.stderr.strip()}")

    # Issue #3271: pad-aware post-route stitching for plane-net pad
    # connectivity.  GND and VBUS_USB are pour nets -- the router skips
    # them on the signal layer and they rely on the In1.Cu / In2.Cu
    # pours for power-plane connectivity.  Without per-pad stitching
    # vias, SMD pads on those nets stay stranded (no copper between
    # the pad and the pour) and the connectivity DRC rule flags them.
    #
    # A NAIVE ``kct stitch --net GND --net VBUS_USB`` invocation placed
    # 27 vias on top of neighbouring same-net QFN / BGA pads (the
    # standard placement offset ``pad_radius + offset`` lands inside
    # the next pad on dense fine-pitch fields).  Those would be
    # ``via_in_pad`` DRC errors under JLCPCB standard tier (which does
    # not support plated-over via-in-pad processing).  The
    # ``--avoid-pad-overlap`` flag (issue #3271) post-filters such
    # placements so the stitched PCB stays manufacturable under the
    # ``jlcpcb`` profile this board targets.  Pads whose ideal via
    # would land in a same-net pad keep their pour-side connection
    # via the zone fill's thermal relief.
    print("\n10. Pad-aware post-route stitching for plane nets (issue #3271)...")
    try:
        # Issue #3413 phase 4: the stitcher covers ONLY GND + VBUS_USB.
        # An earlier iteration stitched all 5 pour nets; the stitcher's
        # stub placement does not validate stub-vs-stub / stub-vs-pad
        # conflicts in dense mixed-net pad fields (measured: +3V3/+1V2
        # stub overlaps inside the BGA-49 inner field and at J1's A-row,
        # ~10 clearance errors).  The +3V3/+1V8/+1V2 pads are instead
        # connected by ``_repair_pour_connectivity`` below, which
        # validates every via + stub against ALL existing copper with
        # shapely before placing it.
        stitch_nets = ["GND", "VBUS_USB"]
        stitch_argv = [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "stitch",
            str(output_path),
            "--mfr",
            "jlcpcb",
            "--avoid-pad-overlap",
        ]
        for net_name in stitch_nets:
            stitch_argv.extend(["--net", net_name])
        stitch_result = subprocess.run(stitch_argv, capture_output=True, text=True)
        if stitch_result.returncode == 0:
            # Surface the last few lines of the stitch summary so the
            # build log records how many vias were added and how many
            # were filtered as would-be via-in-pad placements.
            for line in stitch_result.stdout.strip().split("\n")[-12:]:
                print(f"   {line}")
        else:
            print(f"   Stitch failed (rc={stitch_result.returncode}):")
            if stitch_result.stderr:
                print(f"   stderr: {stitch_result.stderr.strip()}")
    except Exception as exc:  # pragma: no cover - degrade gracefully
        print(f"   Stitch step skipped: {exc}")

    # Issue #3413 phase 4: repair the stitcher's residual.  On this board
    # ``--avoid-pad-overlap`` historically left 27 via-in-pad filters +
    # 27 "manual placement needed" skips = the GND 48/122 stranded
    # residual.  ``_repair_pour_connectivity`` audits geometrically
    # (shapely copper union -- immune to the #3482 boundary-test false
    # positive) and places offset vias + F.Cu stubs + island bridges that
    # stay legal at the jlcpcb standard tier (no via-in-pad).
    #
    # Repair and re-fill ITERATE (max ``MAX_POUR_REPAIR_ROUNDS``): each
    # re-fill recomputes the pours with the new copper carved in, which
    # can shift fill edges away from a previous round's bridge endpoint
    # (measured: a +1V8 bridge connected against the round-1 fill, then
    # the round-2 fill quantised away from it).  The loop converges when
    # the copper-union audit reports every pour net in one component,
    # and short-circuits when the zone filler is unavailable (Issue
    # #3509: every fill failing with "kicad-cli not found" means no
    # round can ever clear the zero-fill-zone audit term, so iterating
    # just grinds repair geometry against an unfillable board).
    def _run_pour_audit(tag: str) -> bool:
        ok = True
        try:
            audit = _audit_pour_nets(output_path, skip_nets)
            for net in skip_nets:
                info = audit[net]
                n_pads = sum(len(g) for g in info["pad_groups"])
                problems = []
                if not info["connected"]:
                    problems.append(
                        f"{len(info['pad_groups'])} disjoint pad groups "
                        f"(largest "
                        f"{len(info['pad_groups'][0]) if info['pad_groups'] else 0}"
                        f"/{n_pads})"
                    )
                if info["zero_fill_zones"]:
                    problems.append(f"{info['zero_fill_zones']} zero-fill zone(s)")
                if problems:
                    ok = False
                    print(f"   {tag} FAIL {net}: {'; '.join(problems)}")
                    for group in info["pad_groups"][1:][:5]:
                        print(f"        stranded: {[p for p, _ in group]}")
                else:
                    print(f"   {tag} OK   {net}: {n_pads} pads in one copper component")
        except ImportError as exc:
            ok = False
            print(f"   {tag} FAIL: audit unavailable ({exc}) -- unverifiable artifact")
        except Exception as exc:
            ok = False
            print(f"   {tag} FAIL: audit crashed ({exc})")
        return ok

    pour_ok = False
    for repair_round in range(1, MAX_POUR_REPAIR_ROUNDS + 1):
        print(
            f"\n10b. Pour-connectivity repair round {repair_round} "
            f"(offset vias + stubs + island bridges)..."
        )
        try:
            rep_vias, rep_bridges = _repair_pour_connectivity(output_path, skip_nets)
            print(f"   Placed {rep_vias} repair via(s) + {rep_bridges} bridge trace(s)")
        except Exception as exc:
            print(f"   WARNING: pour-connectivity repair failed: {exc}")

        # Re-fill after repair: the new via barrels pass through the
        # OTHER nets' planes, so the fills must be recomputed to carve
        # clearance around them (and so the exported copper is final).
        print(f"10c. Re-filling zones (round {repair_round})...")
        fill_result = subprocess.run(fill_argv, capture_output=True, text=True)
        refill_ok = fill_result.returncode == 0
        if not refill_ok:
            print(f"   Zone re-fill failed (rc={fill_result.returncode}):")
            if fill_result.stderr:
                print(f"   stderr: {fill_result.stderr.strip()}")

        # Issue #3413 phase 4 acceptance gate: every pour net must be
        # GEOMETRICALLY continuous (all pads in one copper component) and
        # no fill-enabled pour zone may have zero filled polygons.  This
        # is the copper-union audit, NOT the analyzer's boundary test
        # (#3482).
        print(f"11. Copper-union pour-connectivity audit (round {repair_round})...")
        pour_ok = _run_pour_audit(f"[r{repair_round}]")
        if pour_ok:
            break
        # Issue #3509: when the filler is structurally unavailable (the
        # first pass AND this round's re-fill both failed -- e.g.
        # kicad-cli missing from the environment), every pour zone stays
        # zero-fill and no number of repair rounds can converge.  Stop
        # iterating; the FAIL verdict below (and the CI gate's asserted
        # pour audit) surfaces the environment defect attributably.
        if not first_fill_ok and not refill_ok:
            print(
                "   Zone filler unavailable (every fill invocation failed) -- "
                "aborting repair loop; pours CANNOT converge without fills "
                "(Issue #3509)."
            )
            break
    if not pour_ok:
        print("   POUR CONNECTIVITY: FAIL (see above)")
    else:
        print("   POUR CONNECTIVITY: PASS")

    # Issue #3855 / #3532 / #3617: the diff-pair crossover doglegs and the
    # pour-repair emitter (``_repair_pour_connectivity``) connect copper with
    # single straight segments to raw geometry-derived endpoints, so they ship
    # arbitrary-angle copper (~18-22 degrees off the 0/45/90/135 set) that
    # bypasses the router's on-grid A* output and fails the fleet 45-census
    # gate (``tests/test_fleet_45_census.py``).  Board 07 already pipes its
    # pour-repair copper through the shared #3532 quantizer; board 06 was
    # missing this step, so a fresh ``--step all --seed 42`` re-route emitted
    # 13 un-quantized chords.  Quantize the artifact through
    # ``kicad_tools.router.quantize.quantize_pcb_file``, which replaces each
    # off-angle segment with an EXACT two-leg dogleg (45-degree leg +
    # axis-aligned leg) that preserves the original endpoints bit-for-bit --
    # so pour connectivity (and every net's reach) is unchanged.  Mirror the
    # board-07 / softstart quantize -> re-fill fixpoint: a dogleg's small
    # perpendicular bulge can graze a foreign via barrel, so re-fill carves
    # clearance around the converged geometry before returning.  Any chord
    # whose BOTH dogleg variants clip a via is held off-angle -- the skip
    # assignment is computed per-run by ``_resolve_quantize_treatment`` (#3913)
    # from the current artifact's uuids, robust to the pour-repair bridge
    # uuids that change every re-route.
    print("\n12. 45-degree quantization of off-angle copper (#3855 / #3532)...")
    try:
        from kicad_tools.router.quantize import quantize_pcb_file

        # Issue #3913: snapshot the clearance-violation signature BEFORE the
        # quantizer mutates committed copper.  The board ships a documented,
        # allowlisted marginal-clearance baseline (the pinned 24-error strict
        # gate); the post-quantize gate below must fire ONLY on violations the
        # dogleg mutation NEWLY introduces (the #3855 "dogleg bulge grazes a
        # foreign via barrel" mode), not re-flag pre-existing baseline copper.
        pre_quantize_clearances = _clearance_signature(output_path)
        # Issue #3913: resolve per-segment treatment from THIS run's artifact
        # (flip where it clears, skip where BOTH dogleg variants graze a via
        # barrel).  Hard-coded uuids are unreliable here -- the grazing chord
        # is a GND pour-repair bridge whose uuid is re-minted every re-route --
        # so ``_resolve_quantize_treatment`` reads the current uuids instead of
        # trusting the seed set.  ``QUANTIZE_SKIP_UUIDS`` is passed only as a
        # documented seed for the committed artifact's known crossover chord.
        axis_first_uuids, skip_uuids = _resolve_quantize_treatment(
            output_path, pre_quantize_clearances, seed_skip_uuids=QUANTIZE_SKIP_UUIDS
        )
        quantized = quantize_pcb_file(
            output_path,
            axis_first_uuids=frozenset(axis_first_uuids),
            skip_uuids=frozenset(skip_uuids),
        )
        if quantized:
            print(f"   Quantized {len(quantized)} off-angle segment(s)")
            print("12b. Re-filling zones after quantization...")
            fill_result = subprocess.run(fill_argv, capture_output=True, text=True)
            if fill_result.returncode == 0:
                print("12c. Copper-union pour-connectivity audit (post-quantize)...")
                pour_ok = _run_pour_audit("[quant]")
                print(
                    "   POUR CONNECTIVITY (post-quantize): "
                    + ("PASS" if pour_ok else "FAIL (see above)")
                )
                print("12d. Post-quantize clearance re-validation (#3913)...")
                if not _check_post_quantize_clearances(output_path, pre_quantize_clearances):
                    raise PostQuantizeClearanceError(
                        "Post-quantize clearance violation(s) detected -- "
                        "aborting build. The 45-degree quantizer's dogleg "
                        "bulge grazed foreign copper (the #3855 mode) and "
                        "``_resolve_quantize_treatment`` could not clear it "
                        "with a single-segment flip/skip. See the clearance "
                        "report above; the offending chord needs a re-route so "
                        "its corridor is no longer shared with the via barrel."
                    )
            else:
                print(
                    f"   Zone re-fill after quantization failed "
                    f"(rc={fill_result.returncode}); committed copper still "
                    f"connectivity-correct (dogleg preserves endpoints)."
                )
        else:
            print("   No off-angle segments: routed copper already 45-aligned")
    except PostQuantizeClearanceError:
        # A NEW post-quantize graze is a hard build failure -- re-raise past
        # the graceful-degrade handler below so it is NOT downgraded to a
        # warning (#3913).
        raise
    except Exception as exc:  # pragma: no cover - degrade gracefully
        print(f"   WARNING: 45-degree quantization step skipped: {exc}")

    # Step 13 (gallery-ready, 2026-07-08): legalize the same-net via
    # residuals (sub-floor drill pairs + via-in-pad) and mid-split any
    # off-angle chord the step-12 flip/skip resolver had to skip.  Both
    # passes are transactional per defect and validated with shapely
    # against ALL copper; a failed repair is reported and left in place.
    # Gate exactly like step 12d: the combined mutation must introduce
    # ZERO new clearance violations or the whole step is rolled back.
    print("\n13. Same-net via legalization + off-angle mid-split (gallery-ready)...")
    try:
        import shutil as _shutil
        import tempfile as _tempfile

        pre_legalize_clearances = _clearance_signature(output_path)
        snapshot = _tempfile.mkstemp(suffix=".kicad_pcb")[1]
        _shutil.copy(output_path, snapshot)
        n_split = _split_offangle_chords(output_path, pre_legalize_clearances)
        n_vias_fixed = _legalize_signal_vias(output_path)
        if n_split or n_vias_fixed:
            print(f"   {n_vias_fixed} via defect(s) repaired, {n_split} chord(s) split")
            print("13b. Re-filling zones after legalization...")
            fill_result = subprocess.run(fill_argv, capture_output=True, text=True)
            if fill_result.returncode != 0:
                print(f"   Zone re-fill failed (rc={fill_result.returncode})")
            print("13c. Copper-union pour audit + clearance gate (post-legalize)...")
            pour_ok_13 = _run_pour_audit("[legal]")
            new_clearances = _clearance_signature(output_path) - pre_legalize_clearances
            if not pour_ok_13 or new_clearances:
                print(
                    f"   POST-LEGALIZE GATE: FAIL "
                    f"(pours={'OK' if pour_ok_13 else 'BROKEN'}, "
                    f"{len(new_clearances)} new clearance violation(s)) -- rolling back"
                )
                _shutil.copy(snapshot, output_path)
                subprocess.run(fill_argv, capture_output=True, text=True)
            else:
                print("   POST-LEGALIZE GATE: PASS (0 new clearance violations)")
        else:
            print("   No repairable via/chord residuals detected")
        Path(snapshot).unlink(missing_ok=True)
    except Exception as exc:  # pragma: no cover - degrade gracefully
        print(f"   WARNING: legalization step skipped: {exc}")

    total_signal_nets = len([n for n in router.nets if n > 0])
    success = stats["nets_routed"] == total_signal_nets
    if success:
        print(f"   SUCCESS: all {total_signal_nets} signal nets routed")
    else:
        print(f"   PARTIAL: {stats['nets_routed']}/{total_signal_nets} signal nets routed")

    return success


def run_drc(pcb_path: Path, net_class_map_path: Path | None = None) -> bool:
    """Run kct check --mfr jlcpcb on the routed PCB.

    Issue #3828: when a ``net_class_map.json`` sidecar exists next to the
    routed PCB (written by ``route_pcb``), pass it via ``--net-class-map`` so
    the diff-pair length-skew / routing-continuity rules are ACTIVE in the
    recipe's own gate.  Without it ``kct check`` warns on stderr that those
    rules are inactive and silently passes them.  ``net_class_map_path``
    defaults to ``<pcb_dir>/net_class_map.json`` when not supplied.
    """
    print("\n" + "=" * 60)
    print("Running DRC (kct check --mfr jlcpcb)...")
    print("=" * 60)

    if net_class_map_path is None:
        candidate = pcb_path.parent / "net_class_map.json"
        net_class_map_path = candidate if candidate.is_file() else None

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "check",
        str(pcb_path),
        "--mfr",
        "jlcpcb",
        "--errors-only",
    ]
    if net_class_map_path is not None:
        cmd.extend(["--net-class-map", str(net_class_map_path)])
        print(f"   Using net-class-map sidecar: {net_class_map_path}")
    else:
        print(
            "   WARNING: no net_class_map.json sidecar found; diff-pair "
            "length-skew / routing-continuity rules will be INACTIVE."
        )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                print(f"   {line}")
        if result.returncode != 0 and result.stderr:
            print(f"\n   stderr: {result.stderr}")
        return result.returncode == 0
    except Exception as e:
        print(f"\n   Error running DRC: {e}")
        return False


def export_manufacturing_bundle(routed_path: Path, output_dir: Path) -> bool:
    """Export the manufacturing bundle (gerbers, BOM, CPL, report, manifest).

    Issue #3147: ``kct fleet status`` flags a board ``ship_ready=false``
    with the ``"artifacts stale"`` blocker whenever the routed PCB is
    newer than ``output/manufacturing/manifest.json``.  Re-running this
    recipe always rewrites the routed PCB, so the recipe must also
    regenerate the manufacturing bundle to keep the manifest current.

    ``kct export`` runs the standard JLCPCB recipe (gerbers + drill + BOM
    + CPL + report.{md,pdf} + manifest.json).  ``--skip-preflight`` skips
    the strict pre-flight DRC/ERC gate so the bundle is produced even for
    boards that ship with allowlisted tolerances (mirrors boards
    03/04/05); for clean boards it is harmless.
    """
    print("\n" + "=" * 60)
    print("Exporting manufacturing bundle...")
    print("=" * 60)

    mfg_dir = output_dir / "manufacturing"
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "export",
        str(routed_path),
        "--output",
        str(mfg_dir),
        "--mfr",
        "jlcpcb",
        "--skip-preflight",
    ]
    print(f"\n   Command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.strip().split("\n")[-15:]:
            print(f"   {line}")
    if result.returncode != 0:
        if result.stderr:
            print(f"\n   Error: {result.stderr}")
        return False
    manifest = mfg_dir / "manifest.json"
    if manifest.exists():
        print(f"\n   Manifest: {manifest}")
        return True
    print("\n   WARNING: manifest.json not produced")
    return False


def main() -> int:
    """Entry point.

    Supports the following invocations:

    .. code-block:: bash

        # Default: run all steps (schematic + PCB + route + DRC) into ./output/
        python generate_design.py

        # Custom output dir (positional, backwards compatible)
        python generate_design.py /tmp/my-output

        # Phase 4N (#2660): re-route only for the CI regression gate.
        # ``--step route`` skips schematic + PCB regeneration and re-routes
        # the existing committed unrouted PCB into a new ``*_routed.kicad_pcb``.
        # ``--seed`` is forwarded to ``random.seed()`` before routing for
        # deterministic CI runs (Issue #2589 / Phase 3X.2).
        python generate_design.py --step route --seed 42
    """
    import argparse
    import random

    parser = argparse.ArgumentParser(
        prog="generate_design",
        description="Board 06 (diffpair-test) design generator + Phase 4N CI re-route hook.",
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=None,
        help=(
            "Output directory (default: ./output relative to this script).  "
            "Positional for backwards compatibility with pre-#2660 callers."
        ),
    )
    parser.add_argument(
        "--step",
        choices=["all", "schematic", "pcb", "route"],
        default="all",
        help=(
            "Run only the specified step.  ``route`` re-routes the existing "
            "committed unrouted PCB into ``output/diffpair_test_routed.kicad_pcb``  "
            "without regenerating the schematic or unrouted PCB; used by the "
            "Phase 4N (#2660) CI gate to detect routing-algorithm regressions."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Seed the global ``random`` module with N before routing for "
            "reproducible output (Issue #2589 / Phase 3X.2).  Required by "
            "the Phase 4N CI gate so re-routes are deterministic across "
            "PRs."
        ),
    )
    args = parser.parse_args()

    if args.output_dir is not None:
        output_dir = Path(args.output_dir)
    else:
        output_dir = Path(__file__).parent / "output"

    output_dir = output_dir.resolve()

    # Apply seed before any router call so all downstream ``random.shuffle``
    # / ``random.sample`` consumers (escape strategies, MST trial ordering)
    # are deterministic.  See ``kct route --seed`` (#2589) for the same
    # pattern.
    if args.seed is not None:
        random.seed(args.seed)
        print(f"[seed] Seeded global random with --seed {args.seed}")

    try:
        if args.step == "all":
            project_path = create_project(output_dir, "diffpair_test")
            sch_path = create_schematic(output_dir)
            pcb_path = create_pcb(output_dir)
            routed_path = output_dir / "diffpair_test_routed.kicad_pcb"
            route_success = route_pcb(pcb_path, routed_path)

            # route_success fast-fail gate (#4066, mirrors board 03's gate at
            # boards/03-usb-joystick/generate_design.py:838).  route_pcb runs
            # under a wall-clock ``--timeout`` SAFETY backstop layered above
            # the load-independent per-net ``--deterministic-budget`` iteration
            # cap, so on a loaded machine that outer deadline can fire before
            # every signal net lands and ``route_pcb`` returns ``False``.  If
            # we fall through, the ``write_lvs_report(require_clean=True)`` call
            # below sees a genuinely unrouted signal net as a copper OPEN and
            # raises ``BoardNetlistMismatch``, which the broad ``except`` below
            # reports as exit 1 -- misdirecting the reviewer to the LVS
            # subsystem when the true cause is upstream route truncation.
            # SCOPE: this gate is deliberately confined to the ``--step all``
            # branch.  The separate ``--step route`` branch below is the
            # Phase 4N CI re-route path (#2677) that TOLERATES a partial route
            # by design (USB3_TX1+/- is blocked by the BGA partner-via escape)
            # and does not call LVS -- it must NOT gain this gate.  The
            # "PARTIAL: Routed N/M signal nets" line is already printed above
            # by ``route_pcb``.
            if not route_success:
                raise RuntimeError(
                    "partial route -- likely wall-clock budget exhaustion "
                    "under load (the --timeout safety backstop fired before "
                    "every signal net landed; see the 'PARTIAL: Routed N/M "
                    "signal nets' line above for the exact count). This is NOT "
                    "a copper-LVS / GND-stitching failure -- the pipeline "
                    "stopped before the LVS gate. Re-run "
                    "boards/06-diffpair-test/generate_design.py --step all in "
                    "isolation on a quiet machine, or raise the --timeout in "
                    "route_pcb() if this recurs on an unloaded host."
                )

            drc_ok = run_drc(routed_path)

            # LVS (#3779, re-enabled by #4012): the fixture schematic is
            # now FULLY WIRED (generate_schematic.py emits a wire stub +
            # global label for all 198 pins, PIN_NETS mirroring
            # generate_pcb.py pad-for-pad), so both comparators carry real
            # evidence: the copper comparator binds 198/198 pads and the
            # label comparator agrees pin-for-pin.  Gate on BOTH
            # comparators, boards 00-02 style (see the gate-policy matrix
            # in kicad_tools/lvs/recipe.py): ``require_clean=True`` raises
            # BoardNetlistMismatch on any short/open/label divergence so
            # the recipe exit gate trips before the manufacturing export.
            # (History: the pre-#4012 schematic had ZERO wires, the copper
            # comparator bound 0 pins, and the #4006 vacuity guard rightly
            # refused to call that clean -- the LVS step was skipped
            # entirely.  #4012 wired the schematic, restoring the hard
            # gate #4004 originally intended.)
            # write_lvs_report(require_clean=True) RAISES on any short /
            # open / label divergence, so reaching the next line means the
            # copper-LVS verdict is clean.
            write_lvs_report(
                sch_path,
                routed_path,
                output_dir,
                require_clean=True,
                run_copper=True,
                run_label=True,
            )
            lvs_ok = True

            # Export manufacturing bundle (#3147) so ``kct fleet status``
            # reports ``ship_ready=true`` (the bundle's manifest mtime must
            # be newer than the freshly routed PCB).  Run unconditionally:
            # routing is allowed to be PARTIAL on this board, and a current
            # bundle still clears the ``"artifacts stale"`` blocker.
            mfg_ok = export_manufacturing_bundle(routed_path, output_dir)

            # Issue #3912: the exit code and the SUMMARY are now BOTH derived
            # from one shared PipelineGateResult so they cannot diverge.  The
            # previous ``return 0 if route_success else 1`` dropped the DRC
            # leg entirely -- 18 differential-pair errors printed ``DRC: FAIL``
            # while the process exited 0.  ``run_drc`` here uses
            # ``kct check --net-class-map``; that engine is the ONLY one that
            # can see the diffpair / match-group length-skew rule families
            # (kicad-cli has no native expression for them), so its verdict is
            # threaded in as ``supplemental_drc_ok``.  The gate's authoritative
            # geometric leg (``kicad-cli pcb drc --refill-zones``) catches the
            # shorts / clearance defects kicad-cli owns; the union of the two
            # is the real DRC verdict.  Board-06 routes fully by design, so
            # ``route_allowance=0``.
            gate = evaluate_pipeline_gate(
                routed_path,
                route_ok=route_success,
                route_allowance=0,
                lvs_ok=lvs_ok,
                supplemental_drc_ok=drc_ok,
                supplemental_reason=(
                    "kct check --net-class-map reported diffpair / match-group "
                    "errors (see DRC output above)"
                ),
            )

            print("\n" + "=" * 60)
            print("SUMMARY")
            print("=" * 60)
            print(f"\nOutput dir: {output_dir}")
            print(f"  Project:   {project_path.name}")
            print(f"  Schematic: {sch_path.name}")
            print(f"  PCB:       {pcb_path.name}")
            print(f"  Routed:    {routed_path.name}")
            print("\nResults:")
            for line in gate.summary_lines():
                print(line)
            # ``MFG`` reports whether the bundle was WRITTEN (exit 0 of the
            # export), NOT whether the board is DRC-clean -- board cleanliness
            # is the ``DRC``/``Overall`` verdict above (issue #3912 wording).
            print(f"  MFG bundle: {'WRITTEN' if mfg_ok else 'FAILED'}")

            return gate.exit_code()

        if args.step == "schematic":
            create_schematic(output_dir)
            return 0

        if args.step == "pcb":
            create_pcb(output_dir)
            return 0

        if args.step == "route":
            # Phase 4N (#2660): the CI gate calls this path to re-route the
            # *committed* unrouted PCB.  Do NOT regenerate the unrouted PCB
            # here -- if the unrouted PCB has drifted from the committed
            # one, that's a separate issue (board scaffolding bug, caught
            # by tests/test_board_06_diffpair_test.py).
            pcb_path = output_dir / "diffpair_test.kicad_pcb"
            if not pcb_path.exists():
                print(
                    f"Error: unrouted PCB not found at {pcb_path}.  Run "
                    "``python generate_design.py --step pcb`` first or "
                    "use ``--step all``.",
                    file=sys.stderr,
                )
                return 1
            routed_path = output_dir / "diffpair_test_routed.kicad_pcb"
            # PARTIAL is the expected outcome today (USB3_TX1+/- blocked by
            # the BGA partner-via escape, tracked in #2677).  As long as the
            # routed PCB was written, the CI gate's DRC check determines
            # pass/fail -- not the route_pcb() "all-or-nothing" boolean.
            # Verify routed_path exists to confirm route_pcb() didn't crash.
            route_pcb(pcb_path, routed_path)
            if not routed_path.exists():
                print(
                    f"Error: routed PCB not written to {routed_path}.",
                    file=sys.stderr,
                )
                return 1
            return 0

        # argparse choices already constrains this, but be explicit.
        print(f"Error: unknown step {args.step!r}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
