#!/usr/bin/env python3
"""
Match-Group Test Board - Complete Design Generation

Epic #2661 Phase 3L (issue #2724) regression testbench.

This script orchestrates the full pipeline for board 07:
    1. Create the project file (.kicad_pro)
    2. Generate the schematic (.kicad_sch)
    3. Generate the unrouted PCB (.kicad_pcb)
    4. Route the PCB (...routed.kicad_pcb)
    5. Emit ``output/net_class_map.json`` sidecar (Phase 3M pattern)
    6. Run DRC via ``kct check --mfr jlcpcb``

The board is a 4-layer JLCPCB tier-1 stackup
(F.Cu / In1.Cu GND / In2.Cu PWR / B.Cu) carrying 4 length-matched
groups across 4 protocol families:

    - DDR data byte (10 nets: DQ0-7 + DM0 + DQS_P/N pair)
    - MIPI CSI lanes (3 pairs = 6 nets)
    - HDMI TMDS lanes (3 pairs = 6 nets)
    - Address bus A0-A7 (single-ended N-trace group)

The router is configured with custom ``NetClassRouting`` instances
per group that opt into each Phase 1A field (Epic #2661):

    - length_match_group (Phase 1A #2687) -- group declaration
    - length_match_reference (Phase 1A #2687) -- pace-car semantic
    - length_match_tolerance_mm (Phase 1A #2687) -- per-group tolerance
    - skew_tolerance_mm (Phase 3H #2647) -- diff-pair sub-skew (DQS,
      MIPI, HDMI lanes)

Dependency note (Phase 3H, #2723):
    The ``--length-match-groups`` CLI flag and the
    ``apply_match_group_tuning`` orchestrator do NOT yet exist in
    main.  Until #2723 lands, the route step exercises the *detection*
    + *tracker* + *DRC rule* paths (Phases 1A/1B/1C/1D + 2.5G) but
    does NOT perform group-level meander insertion -- that is what
    #2723 will wire.  Acceptance criterion #7 (post-pass skew strictly
    less than pre-pass skew) is therefore deferred until #2723; the
    tracker query *is* exercised today.

Usage:
    python generate_design.py [output_dir]

If no output directory is specified, files are written to ./output/.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.dev import warn_if_stale
from kicad_tools.lvs import write_lvs_report
from kicad_tools.router.rules import (
    NET_CLASS_POWER,
    NetClassRouting,
    net_class_map_to_dict,
)

# Re-export net definitions and footprint generators from generate_pcb.
sys.path.insert(0, str(Path(__file__).parent))
import generate_pcb  # noqa: E402
import generate_schematic  # noqa: E402

warn_if_stale()

# Route-step discovery sentinel (Tier 2).  ``kct build``'s _run_step_route
# greps for this literal to decide whether to invoke ``generate_design.py
# --step route`` instead of falling back to the generic ``kct route``
# autorouter (which drops this board's --length-match-groups / seed / 4-layer
# flags).  project.kct's ``build.route_recipe`` is the primary (Tier 1)
# signal; this sentinel is the heuristic fallback for callers without the
# spec key.
SUPPORTS_STEP_ROUTE = True


# =============================================================================
# Per-Group Net Class Declarations
# =============================================================================
# These NetClassRouting instances are the authoritative "scenario" data the
# board exercises.  ``build_net_class_map()`` below assembles them into a
# net-name -> NetClassRouting dict that ``route_pcb`` consumes during
# routing.
#
# Each group class declares ``length_match_group`` (Phase 1A #2687).
# Pair members within a group additionally declare
# ``skew_tolerance_mm`` (Phase 3H #2647) so the diff-pair-level DRC
# rule fires alongside the group-level rule.
#
# AC#6 of issue #2724 asserts that each Phase 1-2 feature is engaged;
# this dict is the single source of truth for that audit.
# =============================================================================


def ddr_data_byte_0_net_class() -> NetClassRouting:
    """DDR data byte 0 net class (10 nets: DQ0-7 + DM0 + DQS pair).

    Phase 2E cascade-safety threshold: groups with N>=5 members
    receive ``MAX_INSERTS_PER_GROUP_MEMBER_LARGE=2`` insertions per
    member (vs the small-group default of 4).  This class has N=10
    so the large-group budget applies.

    The ``length_match_reference=None`` policy means "use longest in
    group" -- the legacy ``tune_match_group`` semantic.  For DDR a
    real design typically pins DQS_P as the reference (pace-car); we
    leave it None here so the longest-of-group path is exercised.
    """
    return NetClassRouting(
        name="DDR_DATA_BYTE_0",
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        length_critical=True,
        length_match_group="DDR_DATA_BYTE_0",  # Phase 1A #2687
        length_match_reference=None,  # Phase 1A: None -> longest-in-group
        length_match_tolerance_mm=0.1,  # Phase 1A: tight DDR tolerance
    )


def ddr_dqs_pair_net_class() -> NetClassRouting:
    """DDR strobe pair (DQS_P/DQS_N).

    Member of the DDR_DATA_BYTE_0 match group via shared
    ``length_match_group``, but additionally declares
    ``coupled_routing`` and ``skew_tolerance_mm`` (Phase 3H) so the
    within-pair DRC rule fires.  This is the Phase 2F "group-of-pairs"
    composition exercise: a pair that is also a member of an N-trace
    group.

    Per the issue's curator notes: "the test asserts within-pair skew
    on DQS stays under effective_skew_tolerance after group-level
    tuning (mirrors Phase 2F's own test)".
    """
    return NetClassRouting(
        name="DDR_DQS",
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        intra_pair_clearance=0.10,
        coupled_routing=True,  # Phase 2E
        coupled_continuity_threshold=0.85,  # Phase 2G
        skew_tolerance_mm=0.05,  # Phase 3H: tight DDR strobe budget
        length_critical=True,
        length_match_group="DDR_DATA_BYTE_0",  # Group membership
        length_match_tolerance_mm=0.1,
    )


def mipi_csi_net_class() -> NetClassRouting:
    """MIPI CSI lane net class (3 pairs = 6 nets).

    Phase 2F group-of-pairs symmetric serpentine target, ±0.05mm
    tolerance.  Pair members all share ``length_match_group``;
    detection (Phase 1C #2689) groups them at routing time.
    """
    return NetClassRouting(
        name="MIPI_CSI_LANES",
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        intra_pair_clearance=0.10,
        coupled_routing=True,  # Phase 2E
        coupled_continuity_threshold=0.85,  # Phase 2G
        target_diff_impedance=100.0,  # Phase 3K (Epic #2556)
        impedance_tolerance_percent=10.0,
        skew_tolerance_mm=0.05,  # Phase 3H: tight MIPI lane skew
        length_critical=True,
        length_match_group="MIPI_CSI_LANES",  # Phase 1A #2687
        length_match_tolerance_mm=0.05,  # Phase 1A: tight MIPI tolerance
    )


def hdmi_tmds_net_class() -> NetClassRouting:
    """HDMI TMDS lane net class (3 pairs = 6 nets).

    Phase 2F composition, ±0.075mm tolerance.  In real designs lanes
    match to the clock pair externally; this testbench has all 3
    lanes match to each other (no clock pair member).
    """
    return NetClassRouting(
        name="HDMI_TMDS_LANES",
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        intra_pair_clearance=0.10,
        coupled_routing=True,  # Phase 2E
        coupled_continuity_threshold=0.85,  # Phase 2G
        target_diff_impedance=100.0,  # Phase 3K
        impedance_tolerance_percent=10.0,
        skew_tolerance_mm=0.075,  # Phase 3H: HDMI TMDS budget
        length_critical=True,
        length_match_group="HDMI_TMDS_LANES",  # Phase 1A
        length_match_tolerance_mm=0.075,  # Phase 1A
    )


def addr_bus_net_class() -> NetClassRouting:
    """Generic address bus net class (8 nets: A0-A7).

    Phase 1A declaration with looser ±0.5mm tolerance (parallel-bus
    commodity tier).  Phase 1C suffix-inference fallback would pick
    these up via ``A[0..7]`` even without an explicit declaration,
    but we declare explicitly to exercise the AUTHORITATIVE path.
    """
    return NetClassRouting(
        name="ADDR_BUS",
        priority=2,
        trace_width=0.20,
        clearance=0.15,
        length_critical=True,
        length_match_group="ADDR_BUS",  # Phase 1A
        length_match_reference="A0",  # Phase 1A: pace-car semantic
        length_match_tolerance_mm=0.5,  # Looser commodity-bus tolerance
    )


def build_net_class_map() -> dict[str, NetClassRouting]:
    """Build the canonical net-name -> NetClassRouting mapping.

    This is the single source of truth for both the router (consumed
    in ``route_pcb`` below), the JSON sidecar (``net_class_map.json``,
    Phase 3M pattern), and the regression test
    (``tests/test_board_07_matchgroup_test.py::test_phase_features_exercised``).
    Importing this function from the test guarantees test/implementation
    parity --- the test cannot drift from the routing config.
    """
    ddr = ddr_data_byte_0_net_class()
    dqs = ddr_dqs_pair_net_class()
    mipi = mipi_csi_net_class()
    hdmi = hdmi_tmds_net_class()
    addr = addr_bus_net_class()

    return {
        # DDR data byte 0: 9 single-ended members + DQS diff pair
        "DQ0": ddr,
        "DQ1": ddr,
        "DQ2": ddr,
        "DQ3": ddr,
        "DQ4": ddr,
        "DQ5": ddr,
        "DQ6": ddr,
        "DQ7": ddr,
        "DM0": ddr,
        "DQS_P": dqs,
        "DQS_N": dqs,
        # MIPI CSI lanes (3 pairs)
        "MIPI_CLK_P": mipi,
        "MIPI_CLK_N": mipi,
        "MIPI_DAT0_P": mipi,
        "MIPI_DAT0_N": mipi,
        "MIPI_DAT1_P": mipi,
        "MIPI_DAT1_N": mipi,
        # HDMI TMDS lanes (3 pairs)
        "TMDS_D0_P": hdmi,
        "TMDS_D0_N": hdmi,
        "TMDS_D1_P": hdmi,
        "TMDS_D1_N": hdmi,
        "TMDS_D2_P": hdmi,
        "TMDS_D2_N": hdmi,
        # Address bus
        "A0": addr,
        "A1": addr,
        "A2": addr,
        "A3": addr,
        "A4": addr,
        "A5": addr,
        "A6": addr,
        "A7": addr,
        # Power
        "+1V2": NET_CLASS_POWER,
        "+1V8": NET_CLASS_POWER,
        "GND": NET_CLASS_POWER,
    }


# =============================================================================
# Board pour contract (Issue #3617 — sibling of board 06's #3509)
# =============================================================================
# POUR_NETS is the authoritative plane-net declaration: these nets are
# excluded from the trace router (``route_pcb``'s ``skip_nets`` list) and
# carried by copper pours + stitching vias instead.  The CI gate
# (``scripts/ci/check_matchgroup_coverage.py``) reads ``POUR_NETS`` +
# ``REQUIRE_POUR_CONNECTIVITY`` to assert the re-routed artifact's pour
# connectivity via the shapely copper-union audit (``_audit_pour_nets``).
# This mirrors board 06's POUR_NETS contract (Issue #3413 phase 5 / #3509)
# so both boards declare their plane nets in one place.
POUR_NETS: list[str] = ["GND", "+1V2", "+1V8"]

# Issue #3617: pour-connectivity contract.  When True, the CI gate runs
# this recipe's shapely copper-union audit (``_audit_pour_nets``) against
# the re-routed artifact and FAILS the job if any pour net is disjoint or
# any fill-enabled zone has zero filled polygons.  Before this contract
# board 07's recipe created pour zones (step 4 ``auto_create_zones_for_pour_nets``)
# but never invoked ``kct zones fill`` — the routed artifact shipped zone
# outlines with ZERO fill geometry and there was no pour-connectivity gate
# (the "dead pour" class board 06 closed in PR #3615).  A board that cannot
# yet pass must set this to False WITH a tracking-issue comment (the
# explicit exit clause; mirrors the .github/routed-drc-tolerance.yml
# grandfathering convention) — the verdict must never be silently ignored.
REQUIRE_POUR_CONNECTIVITY: bool = True

# Issue #3617: pour repair <-> re-fill iteration budget.  Mirrors board
# 06's #3509 value: each re-fill recomputes the pours with the new repair
# copper carved in, which can shift fill edges away from a previous round's
# bridge endpoint.  The loop breaks early on audit PASS, so a higher cap
# only costs wall time in failure scenarios.
MAX_POUR_REPAIR_ROUNDS: int = 6


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

    # Vias: (center_point, net, radius, drill_radius)
    via_index: list[tuple] = []
    for via in _find_sexp_blocks(text, "\n\t(via") + _find_sexp_blocks(text, "\n  (via"):
        at = re.search(r"\(at ([\d.-]+) ([\d.-]+)\)", via)
        sz = re.search(r"\(size ([\d.]+)\)", via)
        dr = re.search(r"\(drill ([\d.]+)\)", via)
        nid = re.search(r"\(net (\d+)\)", via).group(1)
        radius = (float(sz.group(1)) if sz else 0.6) / 2.0
        drill_radius = (float(dr.group(1)) if dr else 0.3) / 2.0
        via_index.append(
            (
                Point(float(at.group(1)), float(at.group(2))),
                id_to_name.get(nid, ""),
                radius,
                drill_radius,
            )
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
    VIA_DRILL_R = 0.125  # 0.25 mm drill on every repair via
    CLEAR = 0.15
    STUB_W = 0.15
    BRIDGE_W = 0.2
    # Fab hole-to-hole floor, EDGE-to-edge (mirrors the jlcpcb
    # ``min_hole_to_hole_mm`` = 0.5 that ``kct check`` enforces as
    # ``dimension_drill_clearance``).  The old constant here was a bare
    # 0.45 mm CENTER-to-center -- for a 0.25 mm repair drill next to a
    # 0.20 mm stitch drill that allowed an edge gap of just 0.225 mm and
    # shipped two ~0.39 mm violations on the re-routed artifact.
    MIN_HOLE_TO_HOLE = 0.5

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
            if drill_r > 0 and math.hypot(vx - px, vy - py) < (
                drill_r + VIA_DRILL_R + MIN_HOLE_TO_HOLE
            ):
                return False
        for geom, snet, _lay in seg_index:
            if snet != net and vgeom.distance(geom) < CLEAR:
                return False
        for pt, vnet, radius, drill_r in via_index:
            if pt.distance(vpt) < MIN_HOLE_TO_HOLE + VIA_DRILL_R + drill_r:
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
        for pt, vnet, radius, _drill_r in via_index:
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
        via_index.append((Point(vx, vy), net, VIA_R, VIA_DRILL_R))
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

    def _emit_seg_45(
        net: str,
        p0: tuple[float, float],
        p1: tuple[float, float],
        layer: str,
        width: float,
    ) -> bool:
        """Validate and emit a connectivity stub/bridge as 45-aligned copper.

        Issue #3617 / #3532: ``_emit_seg`` writes a single straight chord, so
        an off-axis pad->via stub or cross-component bridge ships
        arbitrary-angle copper that fails the fleet 45-census gate
        (``tests/test_fleet_45_census.py``).  Rather than post-processing the
        committed artifact (where a dogleg's ``min(|dx|, |dy|)`` perpendicular
        bulge can be >1 mm and clip the congested BGA/diff-pair copper), the
        emitter builds the 45-aligned path itself and validates EVERY leg
        against foreign copper with the same ``_path_ok`` contract -- so the
        repair copper is 45-only AND clearance-clean by construction, and
        future regens stay census-clean without a separate quantize step.

        Tries the straight chord first (already 45-aligned bridges stay one
        segment), then both dogleg variants (diagonal-first / axis-first --
        the bulge falls on opposite sides of the chord).  Emits the first
        candidate whose legs all clear; returns False (caller skips, exactly
        as a bare ``_path_ok`` failure did) when none clears.
        """
        from kicad_tools.router.quantize import dogleg_points, is_45_aligned

        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        if is_45_aligned(dx, dy):
            candidates = [[p0, p1]]
        else:
            candidates = [
                dogleg_points(p0[0], p0[1], p1[0], p1[1], axis_first=False),
                dogleg_points(p0[0], p0[1], p1[0], p1[1], axis_first=True),
            ]
        for verts in candidates:
            legs = list(zip(verts[:-1], verts[1:], strict=True))
            if all(_path_ok(net, a, b, layer, width) for a, b in legs):
                for a, b in legs:
                    _emit_seg(net, a, b, layer, width)
                return True
        return False

    # --- per-net connectivity loop ------------------------------------------
    for net in net_names:
        # Own elements: (geom, layerset, label).  Pads carry their name.
        own: list[tuple] = []
        for poly, lay in fills_by_net.get(net, []):
            own.append((poly, frozenset({lay}), "fill"))
        for geom, snet, lay in seg_index:
            if snet == net:
                own.append((geom, frozenset({lay}), "seg"))
        for pt, vnet, radius, _drill_r in via_index:
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
                    _emit_seg_45(net, (x0, y0), (vx, vy), "F.Cu", STUB_W)
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
                    if not _emit_seg_45(net, p0, p1, lay, BRIDGE_W):
                        continue
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
                        if not _emit_seg_45(net, p0, p1, best_lay, BRIDGE_W):
                            continue
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
                                # Straight chord cleared above, so a
                                # 45-aligned chord always emits; an off-angle
                                # chord doglegs (both via barrels span all
                                # layers, the bulge clears the same copper the
                                # straight chord did at this short length).
                                _emit_seg_45(net, va, vb, lay, BRIDGE_W)
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
    output_path = output_dir / "matchgroup_test.kicad_sch"
    generate_schematic.create_matchgroup_schematic(output_path)
    return output_path


def create_pcb(output_dir: Path) -> Path:
    """Generate the unrouted PCB."""
    print("\n" + "=" * 60)
    print("Creating PCB...")
    print("=" * 60)
    output_path = output_dir / "matchgroup_test.kicad_pcb"
    pcb_content = generate_pcb.generate_pcb()
    output_path.write_text(pcb_content)
    print(f"   PCB: {output_path}")
    print(f"   Nets: {len([n for n in generate_pcb.NETS.values() if n > 0])}")
    print(f"   Diff pairs: {len(generate_pcb.DIFFPAIRS)}")
    print("   Match groups: 4 (DDR_DATA_BYTE_0, MIPI_CSI_LANES, HDMI_TMDS_LANES, ADDR_BUS)")
    return output_path


def write_sidecar(net_class_map: dict, output_dir: Path) -> Path:
    """Emit the ``net_class_map.json`` sidecar (Phase 3M pattern).

    Without this sidecar, ``kct check --net-class-map <path>`` cannot
    re-derive match-group / diff-pair engagement on the routed PCB,
    so ``match_group_length_skew`` (and the diff-pair rules) degrade
    to no-ops.  This is exactly the trap PR #2692 fixed for diff-pair
    rules; we apply the same fix preventatively for match groups.
    """
    sidecar_path = output_dir / "net_class_map.json"
    sidecar_path.write_text(json.dumps(net_class_map_to_dict(net_class_map), indent=2))
    print(f"   Wrote net-class-map sidecar: {sidecar_path}")
    return sidecar_path


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """Route the PCB by invoking ``kct route`` with the proven flag recipe.

    Returns True if ``kct route`` reports full success (return code 0);
    False if it produced a partial routing (still acceptable -- the
    output file is written either way and downstream DRC continues).

    Issue #2991: Previously this function called ``router.route_all()``
    directly through the in-process API, configured with a custom
    ``DesignRules(...)`` block.  Mirroring the sibling board-05 bake
    in PR #2981, replace the in-process call with a subprocess
    invocation of the ``kct route`` CLI:

        --manufacturer jlcpcb --strategy negotiated --no-auto-layers
        --layers 4 --seed 42 --timeout 600

    Recipe-vs-AC deviation (Issue #2991, builder empirical validation
    2026-05-17):

      The issue body cites ``--auto-fix --auto-layers --auto-mfr-tier``
      as the verified recipe yielding 29/31 nets.  Two independent
      problems block that as-stated recipe; both were verified
      empirically against main (commit 46bd8601) before the recipe
      was adjusted to the form above:

      1. ``--auto-mfr-tier`` regressed to ~17/31 nets, well under the
         current in-process baseline of 25/31.  This matches Scout
         commit ``92fc35cb`` (2026-05-16) which explicitly notes:
         "Auto-mfr-tier path regressed to 3/31 per attempt (~40s/net
         with C++ router vs previous 13s/net baseline, likely
         VectorCollisionChecker overhead)".  ``--auto-mfr-tier`` is
         NOT the recipe that produced the 29/31 result.

      2. The Scout 2 verified 29/31 recipe -- ``negotiated +
         differential-pairs, 4L`` (commit 92fc35cb) -- DOES yield
         29/31 (94%) routing completion on this board.  However, the
         CLI's ``--differential-pairs`` mode in main (as of 2026-05-17)
         places diff-pair sibling traces at OVERLAPPING positions
         (within-pair clearance -0.150mm, negative).  Under jlcpcb
         tier-1 rules this produces ~20,300 ``diffpair_clearance_intra``
         violations -- a catastrophic routed-DRC regression that blows
         past the 70-error allowlist by ~290x.

      The rich ``NetClassRouting`` per-pair / per-group declarations
      that ``build_net_class_map()`` assembled previously fed the
      in-process router, but ``kct route`` does NOT accept
      ``--net-class-map`` or equivalent (verified via ``kct route
      --help``).  This is the same gap the curator flagged as an
      "open concern" on issue #2991: the routing-time NetClassRouting
      fields (``intra_pair_clearance``, ``coupled_continuity_threshold``,
      etc.) do not project through the subprocess boundary.

      Update (refresh tracker #3295, 2026-06-07): with #3012 closed
      (PR #3022 added the ``min_spacing_cells`` floor in
      ``CoupledPathfinder``), the *DRC* blocker against
      ``--differential-pairs`` is fixed.  However, an empirical re-enable
      test on the same date showed the ``CoupledPathfinder`` pre-pass
      hangs CPU-bound for >40 minutes on this board (well past the
      ``--timeout 600`` budget, which the pre-pass does not honour).
      This recipe therefore continues to omit ``--differential-pairs``.
      Net yield is 28/31 (matching the PR #3276 baseline: DQ3,
      MIPI_CLK_N, MIPI_DAT0_N remain stranded per #3275) and routed-DRC
      stays under the per-board allowlist (currently 25; measured 23
      under jlcpcb-tier1 / 17 under jlcpcb).  See
      the inline comment block adjacent to the ``cmd`` list for the
      full empirical record.

    What each flag does:

    - ``--manufacturer jlcpcb``: triggers the jlcpcb design-rule
      profile so the router applies the tier-1 trace/space/via floor.
    - ``--strategy negotiated``: the negotiated rip-up/reroute strategy
      (explicit for clarity; this is also the default).
    - ``--no-auto-layers --layers 4``: pin a 4-layer stackup (the
      board's declared topology: F.Cu / In1.Cu GND / In2.Cu PWR / B.Cu).
      The router's layer-escalation loop would otherwise spend the
      wall-clock budget probing 2L and 6L attempts before settling on
      4L; pinning saves time for actual routing.
    - ``--seed 42``: deterministic output.  The Phase 3N CI gate
      (``scripts/ci/check_matchgroup_coverage.py``) re-invokes this
      script with ``--step route --seed 42`` and asserts a byte-stable
      re-route across PRs.  ``kct route`` honours ``--seed`` by
      seeding the global ``random`` module (route_cmd.py:5296-5299).
      This is the issue's stated HARD LIMIT and is preserved.
    - ``--timeout 600``: outer wall-clock budget; per-net timeout
      defaults to 30 s.  600 s gives the pure-Python fallback path on
      CI runners (no native router_cpp.*.so) enough budget for 31
      nets while remaining under the GitHub Actions 10-min ceiling.

    Skip nets ``GND``, ``+1V2``, ``+1V8`` remain handled via copper
    pours on inner planes (In1.Cu / In2.Cu) emitted post-route by
    ``auto_create_zones_for_pour_nets``.

    Per-group ``NetClassRouting`` sidecar engagement note:
        The rich ``NetClassRouting`` instances assembled by
        ``build_net_class_map()`` (DDR / MIPI / HDMI / ADDR) are
        emitted into the ``net_class_map.json`` sidecar BEFORE the
        subprocess runs.  ``kct check`` consumes that sidecar to fire
        ``match_group_length_skew`` and the diff-pair rules during DRC.

    NOTE on Phase 3H (#2723) dependency:
        When ``--length-match-groups`` and ``apply_match_group_tuning``
        land, the CLI itself will perform group-level meander
        insertion; no further change here will be required.

    Issue #3414 (per-board manufacturable bar):
        Issue #3402's per-board ``starting_layers`` audit cited Issue
        #2723 (this CLI flag, now landed and in use here) as the likely
        path to close the 84% routability gap.  Empirical validation in
        the #3414 worktree DISPROVED that hypothesis: the flag engages
        the orchestrator but reach plateaus at 28/31 (90%); three nets
        (DQ3, MIPI_CLK_N, MIPI_DAT0_N) are stranded by the negotiated
        single-ended router's ordering/escape dynamics, not by any
        missing length-matching pass.  Isolation evidence (2026-06-09,
        main @ post-#3431):

          - The 3 stranded nets route 3/3 in 0.2s when everything
            else is skipped.
          - The 6-net MIPI bundle alone routes 6/6 in 11s.
          - The 11-net DDR bundle ALONE on an empty board routes only
            9/11 (DQ5, DM0 fail) -- the failure is order-dependent
            chaos inside a trivially hand-routable parallel bundle.

        Residual gap decomposition (filed by the #3414 recovery PR):
        #3438 (negotiated bundle reach -- the 28/31 blocker),
        #3439 (CoupledPathfinder 60s/pair budget overrun -- blocks the
        diff-pair continuity/skew DRC errors), #3440 (match-group
        tuner leaves ADDR_BUS at 15.4mm skew), #3441 (auto-grid
        0.127mm puts every 0.1mm-aligned pad off-grid).

    Recovery run #3 (2026-06-09, main @ post-#3437) -- refined #3438
    root cause:
        Re-measured baseline is unchanged: 28/31, 23 DRC errors,
        stranded nets DQ3 / MIPI_CLK_N / MIPI_DAT0_N (solo, seed 42).
        Iteration 0 dipped to 26/31 after PR #3434 but iteration 1's
        whole-cell rip-up recovers the two TMDS_N nets, restoring 28.

        The DDR bundle is a FULL BUS REVERSAL (U1 right column DQ0 at
        +4.4mm faces U2 left column DQ0 at -4.4mm -- every pair of the
        11 nets crosses), not a parallel bundle.  Isolation repros:

          - 11-net DDR bundle alone:        10/11 (DQ3 fails in 0.04s)
          - 10-net bundle (DQ0 removed):    10/10 in 5s
          - 5-net bundle (DQS pair, DM0, DQ4, DQ3): 5/5
          - priority-forced orders (monotone / reverse / outside-in):
            10/11, 10/11, 8/11 -- the failing member shifts with the
            order; no static order completes the reversal.

        DQ3's failure is an INSTANT empty-frontier abort, not search
        exhaustion: foreign escape stubs and vias are hard obstacles
        even in sharing mode (``is_trace_blocked``'s
        ``cell.net != net && usage_count == 0`` clause), and legal via
        sites need ~0.5mm of F.Cu travel from the stub end (0.6mm via
        vs 0.8mm pitch), so sibling vias + stub halos seal the exit.
        Because the failure is hard (no overflow), PathFinder receives
        no congestion signal AND the negotiated loop declares
        "Convergence achieved" at overflow==0 with unrouted nets
        remaining, abandoning unused wall budget (verified with
        --timeout 1500: loop still exits at ~820s with 28/31).
        Removing one net (DQ0) flips
        ``_assign_matrix_layer_preferences``'s sorted-id parity for
        every bundle member, which is why outcomes are chaotically
        sensitive to the exact net set.  All of the above posted to
        #3438.
    """
    print("\n" + "=" * 60)
    print("Routing PCB (via ``kct route`` flag recipe -- Issue #2991)...")
    print("=" * 60)

    # Power and ground nets are handled via copper pours on the inner
    # planes (In1.Cu = GND, In2.Cu = PWR).  Skip them at the trace
    # router so they don't fight for outer-layer corridors.  Derive the
    # skip list from the module-level POUR_NETS contract (Issue #3617) so
    # the router-skip set and the CI gate's pour audit reference the same
    # declaration.
    skip_nets = list(POUR_NETS)

    # Emit the JSON sidecar BEFORE invoking the subprocess.  The CI
    # gate (scripts/ci/check_matchgroup_coverage.py:223-235) requires
    # the sidecar to exist on disk after the route step completes,
    # even when ``kct route`` exits non-zero (partial routing).  The
    # sidecar is the single source of truth for the group / diff-pair
    # declarations consumed by ``kct check --net-class-map``.
    net_class_map = build_net_class_map()
    print(f"\n1. Net classes assembled: {len(net_class_map)} entries")
    print(f"   Diff pairs declared: {len(generate_pcb.DIFFPAIRS)}")
    print("   Match groups (length_match_group): 4")
    sidecar_path = write_sidecar(net_class_map, output_path.parent)

    # Issue #2996: ``kct route`` now accepts ``--net-class-map`` (this PR)
    # which merges the rich NetClassRouting declarations
    # (intra_pair_clearance, coupled_routing, length_match_group, ...)
    # into the autorouter's net_class_map at routing time.  This closes
    # the *projection gap*: pre-#2996, ``--differential-pairs`` had no
    # way to consume the sidecar's per-pair ``intra_pair_clearance``
    # overrides and fell back to defaults that resolved to -0.150 mm
    # (overlapping sibling traces, ~20K ``diffpair_clearance_intra``
    # violations -- the bug this issue documents).
    #
    # Issue #3003: hardens the ``DiffPairRouter.route_differential_pair_coupled``
    # inline serpentine shim so it (a) gates
    # ``match_pair_lengths(add_serpentines=True)`` on ``length_critical=True``
    # (length-critical pairs are routed by the audited Phase 3I tuner
    # instead of this shim), and (b) when it does run, threads
    # ``intra_pair_clearance_mm`` + the partner route through
    # ``create_serpentine`` so the bulge biases away from the partner and
    # is DRC-rejected if it would violate intra-pair clearance.
    #
    # Issue #3012 (CLOSED via PR #3022, 2026-05-18): added the
    # ``min_spacing_cells`` floor in ``CoupledPathfinder`` so coupled
    # routing now consults
    # ``net_class.effective_intra_pair_clearance()`` (see
    # ``src/kicad_tools/router/diffpair_routing.py:700-704, :769-774,
    # :828-830``).  Prior to PR #3022, ``--differential-pairs`` on
    # board 07 produced ~459 ``diffpair_clearance_intra`` violations
    # because both centerlines were laid at ``pair.rules.spacing``
    # without consulting the per-pair intra-clearance override.
    #
    # Re-enable attempt (refresh tracker #3295, 2026-06-07):
    # The #3275 curator hypothesis was that with #3012 closed, the
    # easy win on board 07 would be to re-append ``--differential-pairs``
    # so MIPI_CLK_N / MIPI_DAT0_N route through ``CoupledPathfinder``
    # instead of failing on corridor contention in the single-ended
    # negotiated loop.  Empirical test on this worktree (2026-06-07,
    # main @ 956f9487, C++ backend v1.0.0):
    #
    #   - With ``--differential-pairs`` added to the command line,
    #     the ``route_all_with_diffpairs`` pre-pass
    #     (route_cmd.py:7253-7295) enters the ``CoupledPathfinder``
    #     loop and pegs CPU at 99.7% for >40 minutes without emitting
    #     any per-pair progress (no "Routing pair ..." lines, no
    #     "coupled pathfinder" diagnostics).  The ``--timeout 600``
    #     argument is forwarded only to the negotiated phase
    #     (``_phase2_strategy`` closure), NOT to the diff-pair pre-pass,
    #     so the run never self-aborts and never reaches the
    #     single-ended negotiator that produces the 28/31 baseline.
    #   - This is a wall-clock regression of >4x the recipe's stated
    #     ``--timeout 600`` budget (and would blow past the GitHub
    #     Actions 10-min job ceiling on CI).
    #
    # Conclusion: ``--differential-pairs`` cannot be re-enabled here
    # until the CoupledPathfinder is either (a) made interruptible
    # against a wall-clock budget or (b) profiled and made fast enough
    # to complete in under the recipe budget.  Filing this empirical
    # observation as part of the refresh; #3275 retains its open
    # follow-up status (the curator's diagnosis named the right
    # mechanism, but the proposed easy-win does NOT empirically land).
    #
    # Issue #3098 (M-G milestone): add ``--length-match-groups`` so the
    # ``apply_match_group_tuning`` orchestrator hook is engaged on the
    # routed PCB.  Before this PR the recipe omitted the flag (it was
    # gated as a "Phase 3H dependency" while #2723 was pending) and the
    # post-route ADDR_BUS skew measured 21.165 mm vs the declared 0.500 mm
    # tolerance -- the ``match_group_length_skew`` rule was engaged but
    # failing because no group-aware serpentine insertion ran.
    #
    # The flag composes with ``--net-class-map``: the orchestrator
    # consumes the rich per-group declarations
    # (``length_match_group``, ``length_match_tolerance_mm``,
    # ``length_match_reference``) from the sidecar JSON to drive
    # ``apply_match_group_tuning`` after the main routing pass.
    # Issue #3414 history: empirical validation in the #3414 worktree
    # (origin/main @ ca8e6899) tested several router knobs to lift this
    # board's reach above 28/31 (90%):
    #
    #   - ``--differential-pairs`` (with #3320 / #3330 fixed):  28/31
    #   - ``--placement-feedback`` (default budget):            29/31
    #     (iteration-0 preserved a +1 net but no component moves)
    #   - ``--placement-feedback-no-anchor J1,J2,J3`` + long
    #     outer-timeout:                                        26/31
    #     (pf wandered to a WORSE placement over iterations)
    #   - Pin swap (DQ3 -> outermost on U1.25 + U2.1, mirroring):
    #     same 28/31 with the failures shifted to the NEW occupants
    #     of the previously-failing pin positions -- proving the
    #     blocker is the negotiated escape order, not net identity
    #   - Wider channels (J1/U3 from 20mm -> 30mm; U1/U2 30mm -> 40mm):
    #     27/31 (worse -- larger area gives the router more room to
    #     waste in poor partial moves)
    #
    # Recovery-run addendum (2026-06-09, main @ post-#3426/#3431):
    #
    #   - ``--iterations 25 --early-stop-patience 8``: no gain --
    #     iteration 0 is the high-water mark (the #3101 pattern);
    #     later rip-up iterations only regress.
    #   - ``--strategy monte-carlo --mc-trials 20``: unviable --
    #     pure-Python multiprocessing workers, >25 min wall-clock
    #     (CI budget is 10 min); killed.
    #   - ``--micro-via-in-pad-fallback``: no gain on the 3 nets.
    #   - ``--grid 0.1`` (board pads are 0.1mm-aligned): WORSE --
    #     53 pads still classified off-grid and 15 nets end only
    #     partially connected via waypoint injection (#3441).
    #   - ``--differential-pairs`` re-test: CoupledPathfinder blows
    #     its 60s/pair budget on EVERY pair (~14k iters/60s, pure
    #     Python; #3439) and the burned budget collapsed the run.
    #   - Two-pass (pre-route the 3 stranded nets, then the rest with
    #     ``--preserve-existing``): failures SHIFT to the partners
    #     (MIPI_CLK_P, MIPI_DAT1_P) -- ordering chaos, not geometry.
    #   - PR #3434 head (target-aware in-pad stubs): 26/31 on this
    #     board (TMDS_D0_N/TMDS_D1_N newly stranded; load-contended
    #     probe -- warning posted on the PR).
    #   - Load sensitivity: concurrent routing runs on the same host
    #     degrade iteration-0 reach to 25-26/31 (wall-clock-budgeted
    #     per-net A*).  Reach comparisons are only valid solo.
    #
    # Recovery-run #3 addendum (2026-06-09, main @ post-#3437, solo):
    #
    #   - Baseline re-measure: 28/31, 23 DRC (same 3 stranded nets).
    #   - ``--targeted-ripup`` (NEW CLI flag wired by the #3414
    #     recovery PR; the route_all_negotiated implementation existed
    #     but was CLI-unreachable): WORSE -- 26/31.  Displacement is
    #     lossy: displaced siblings are re-placed best-effort and the
    #     (1 + N_blockers) x per-net-timeout cost blows the wall
    #     budget (DQ3 pulls in 10 DDR siblings).
    #   - ``--targeted-ripup --per-net-timeout 15 --timeout 1500``:
    #     24/31 at iters 1-2; best-restore returns iter-0's 26.
    #   - ``--per-net-timeout 60 --timeout 1500`` (default rip-up):
    #     28/31 -- the per-net budget is NOT the binding constraint;
    #     the loop exits "Convergence achieved" at overflow==0 with
    #     3 nets unrouted and ~680s of budget unused.
    #   - Per-net sidecar ``priority`` order forcing on the DDR
    #     bundle (monotone/reverse/outside-in): 10/11, 10/11, 8/11.
    #     The full-reversal bundle never completes under any static
    #     order; the failing member follows the order position.
    #   - Two-pass with ``--preserve-existing``: pass 2 is strictly
    #     harder (preserved copper is a hard obstacle).  Also found
    #     and fixed en route: ``parse_vias`` matched ZERO vias on
    #     KiCad-8 output (uuid-before-net field order), so
    #     --preserve-existing silently dropped every via -- this had
    #     invalidated the earlier two-pass experiments repo-wide.
    #
    # Conclusion: the gap to 100% is an algorithmic gap in the
    # negotiated router's handling of full-reversal pad-array bundles
    # (hard-failing nets emit no PathFinder congestion signal, and the
    # loop declares convergence at overflow==0 with unrouted nets),
    # not a CLI/config gap.  The recipe stays at the proven 28/31
    # baseline; the residual gap is tracked in #3438 (reach -- now
    # with minimal 5/10/11-net repros), #3439 (coupled routing perf),
    # #3440 (match-group tuner), #3441 (auto-grid).
    #
    # Issue #3439 addendum (2026-06-10): the CoupledPathfinder now has
    # (a) a corridor-bounded search mode (P side routed single-ended
    # via the C++ backend, coupled A* restricted to the dilated
    # corridor) and (b) an aggregate coupled-phase cap auto-derived as
    # ``max(per_pair, 0.25 * timeout)``.  Empirical re-test with
    # ``--differential-pairs`` on this recipe (load-contended host):
    #   - DQS coupled search COMPLETES in ~4s (vs blowing the 60s
    #     budget at 14k iterations before) but is rejected for the
    #     #3320 polarity-swap centerline overlap.
    #   - MIPI pairs still budget-exit: their P sides cannot route
    #     single-ended at all (the #3438 reach gap), so no corridor
    #     exists; the open search remains intractable.
    #   - The aggregate cap fires at 150s and defers the 3 TMDS pairs;
    #     total coupled phase = 150s vs 420s pre-#3439, and reach no
    #     longer collapses (26-28/31 vs the 7/31 incident).
    # ``--differential-pairs`` is now SAFE to enable (bounded, reach-
    # preserving) but is not yet a quality win: no pair survives to a
    # committed coupled route until #3438 (MIPI reach) and #3320
    # (DQS swap overlap) land.  The recipe therefore still omits it.
    #
    # Issue #3441 addendum (2026-06-10): the "--grid 0.1: WORSE"
    # entry above was a router bug, not a grid property.  Waypoint
    # injection (#2330) exists only in the Python pathfinder, yet the
    # hardcoded use_waypoint_injection=True ALSO disabled the sub-grid
    # escape pre-pass and PIN_ACCESS retry -- under --backend cpp all
    # three off-grid recovery mechanisms were off at once.  With the
    # backend-aware gate (PR for #3441):
    #
    #   - auto-grid (this recipe, solo, seed 42): 29/31, 0 partial --
    #     +1 over the 28/31 baseline (the re-enabled PIN_ACCESS retry
    #     recovers an extra net); auto-grid deliberately KEEPS 0.127mm
    #     (53/244 pads -- U4 BGA-49 45 + J3 8 -- are genuinely off the
    #     0.1mm lattice, and 0.127 aligns the BGA exactly; forcing
    #     --grid 0.1 measured 23-26/31 across the recovery work).
    #   - explicit --grid 0.1: 13/31 + 15 partial -> 23/31 + 6 partial;
    #     the residual TMDS_D0/D1 disconnects at U4 sit in the
    #     two-phase EscapeRouter pipeline (#3143 escape endpoint
    #     overrides), i.e. the #3438 lane.
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(input_path),
        "--output",
        str(output_path),
        "--manufacturer",
        "jlcpcb",
        "--strategy",
        "negotiated",
        "--no-auto-layers",
        "--layers",
        "4",
        "--seed",
        "42",
        "--timeout",
        "600",
        # Issue #3538: bound the per-net A* search by an ITERATION budget
        # (fixed node-expansion count) instead of the per-net wall-clock
        # cutoff, so the seed-42 re-route lands the SAME copper -- and the
        # SAME DRC count -- regardless of runner speed/load.  This is the
        # fix for the "#3466 wall-clock-budget cliff" that forced the
        # board-07 floor in .github/routed-drc-tolerance.yml to absorb a
        # machine-variance band (21 -> 28 -> 34 -> ...).  --timeout 600
        # above is now a SAFETY backstop only; the iteration budget is the
        # binding constraint.  Combined with --seed 42 + PYTHONHASHSEED=42
        # the re-route is reproducible across CI ubuntu-latest and local
        # macOS arm64.
        "--deterministic-budget",
        "--skip-nets",
        ",".join(skip_nets),
        "--net-class-map",
        str(sidecar_path),
        "--length-match-groups",
    ]

    # Issue #3146: Pin PYTHONHASHSEED for the subprocess so any string-
    # keyed dict/set iteration in the negotiated router (net_order
    # construction, net_names lookup, etc.) is reproducible across
    # runner environments.  Without this, CPython's per-process hash
    # randomization makes the iteration order of any ``set[str]`` or
    # ``dict[str, ...]`` non-deterministic between processes -- the
    # primary remaining source of A* push-order drift after the C++
    # tie-break fix (PR closing #3146 / #3144).  We force "42" rather
    # than passing through whatever the parent has set so the inner
    # routing process is reproducible even when the outer test harness
    # leaves PYTHONHASHSEED unset (the common case on developer boxes).
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "42"

    print(f"\n2. Input: {input_path}")
    print(f"   Output: {output_path}")
    print(f"   Skipping pour nets: {skip_nets}")
    print(f"   Command: PYTHONHASHSEED={env['PYTHONHASHSEED']} {' '.join(cmd)}")
    print("\n3. Routing...")

    result = subprocess.run(cmd, capture_output=False, text=True, env=env)

    # ``kct route`` returns 0 on full success and a non-zero code on
    # partial / failed routing.  Either way it writes a routed PCB to
    # ``output_path`` (the partial-results file is at
    # ``<stem>_partial.kicad_pcb``).  As long as the output file
    # exists, downstream steps (zone generation + DRC) can run; report
    # success/partial purely informationally.
    success = result.returncode == 0

    if not output_path.exists():
        print(f"\n   ERROR: ``kct route`` did not produce {output_path}", file=sys.stderr)
        return False

    if success:
        print("\n   SUCCESS: ``kct route`` reports all signal nets routed!")
    else:
        print(
            f"\n   PARTIAL: ``kct route`` exited with code {result.returncode} "
            "(partial routing; downstream zone + DRC will continue)"
        )

    # Phase 3H (#2723) integration point.  When --length-match-groups
    # lands, the CLI's own routing pipeline will apply
    # apply_match_group_tuning between route_all and the optimizer; no
    # further change required HERE.
    # TODO Phase 3H (#2723): verify --length-match-groups consumes the
    # net_class_map.json sidecar so group meandering engages.

    # Issue #2835: emit copper-pour zones for GND + power nets so the
    # net-status report doesn't flag pour-net pads (~179 pads on this
    # board) as "incomplete".  Without zones, PR #2777's per-net
    # bounding-box partitioning never runs on this board.  Layer
    # assignment is stackup-aware (4-layer): GND -> In1.Cu (full board
    # outline), power nets (+1V2 / +1V8) distributed across In2.Cu / F.Cu
    # with per-net bounding outlines.
    #
    # ``kct route`` may pour zones for known power nets internally on
    # some recipes, but the board's per-net layer-aware zone declaration
    # is more authoritative.  ``auto_create_zones_for_pour_nets`` is
    # idempotent (it adds zones by net+layer; duplicate calls are
    # detected by the upstream ``auto_pour_if_missing`` helper used
    # elsewhere).  Use the board's authoritative ``skip_nets``
    # declaration so the zone-net set matches the router-skip set
    # exactly.
    print("\n4. Generating copper-pour zones...")
    try:
        from kicad_tools.router.net_class import NetClass
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        pour_nets_decl: list[tuple[str, NetClass]] = []
        for net_name in skip_nets:
            if net_name == "GND":
                pour_nets_decl.append((net_name, NetClass.GROUND))
            else:
                pour_nets_decl.append((net_name, NetClass.POWER))
        # Issue #3818: ``kct route`` already runs an internal auto-pour
        # (``auto_pour_if_missing`` with ``force_pour_nets=skip_nets``) that
        # leaves a zone per pour net at its OWN edge inset.  Calling
        # ``auto_create_zones_for_pour_nets`` additively here would stack a
        # SECOND, overlapping same-net same-layer zone (this recipe's 0.5 mm
        # inset) on top -- and KiCad's fill resolver awards the shared region
        # to one of the two duplicates non-deterministically, leaving the
        # other with ZERO ``filled_polygon`` regions.  The copper-union audit
        # counts that empty duplicate as a "dead pour" and the match-group CI
        # gate flakes PASS/FAIL on functionally-identical re-routes.
        # ``replace_existing=True`` drops the router's zones first so the
        # board ends with EXACTLY ONE authoritative zone per (net, layer),
        # making the per-zone zero-fill term deterministic across runs.
        zone_count = auto_create_zones_for_pour_nets(
            output_path, pour_nets_decl, edge_clearance=0.5, replace_existing=True
        )
        print(f"   Created {zone_count} zone(s) for {[n for n, _ in pour_nets_decl]}")
    except Exception as exc:  # pragma: no cover - degrade gracefully
        print(f"   Zone generation skipped: {exc}")

    # Issue #3617 (sibling of board 06's #3413 phase 4 / #3509): FILL the
    # zones.  Until this PR the recipe created pour zone OUTLINES (step 4
    # above) but never invoked the filler, so the committed/CI artifact had
    # zone boundaries with ZERO ``filled_polygon`` copper — every pour
    # net's connectivity was a boundary-test illusion (#3482).  The
    # stitcher and the copper-union audit below need real fill geometry.
    print("\n5. Filling zones (first pass)...")
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

    # Issue #3271 / #3617: pad-aware post-route stitching for plane-net pad
    # connectivity.  GND / +1V2 / +1V8 are pour nets — the router skips
    # them on the signal layers and they rely on the In1.Cu / In2.Cu pours
    # for power-plane connectivity.  Without per-pad stitching vias, SMD
    # pads on those nets stay stranded (no copper between the pad and the
    # pour) and the connectivity DRC rule flags them.  ``--avoid-pad-overlap``
    # (issue #3271) post-filters via placements that would land in a same-net
    # pad (would-be ``via_in_pad`` DRC errors under jlcpcb standard tier).
    print("\n6. Pad-aware post-route stitching for plane nets (issue #3271)...")
    try:
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
        for net_name in skip_nets:
            stitch_argv.extend(["--net", net_name])
        stitch_result = subprocess.run(stitch_argv, capture_output=True, text=True)
        if stitch_result.returncode == 0:
            for line in stitch_result.stdout.strip().split("\n")[-12:]:
                print(f"   {line}")
        else:
            print(f"   Stitch failed (rc={stitch_result.returncode}):")
            if stitch_result.stderr:
                print(f"   stderr: {stitch_result.stderr.strip()}")
    except Exception as exc:  # pragma: no cover - degrade gracefully
        print(f"   Stitch step skipped: {exc}")

    # Issue #3617: repair the stitcher's residual then iterate repair <->
    # re-fill (max ``MAX_POUR_REPAIR_ROUNDS``).  ``_repair_pour_connectivity``
    # audits geometrically (shapely copper union — immune to the #3482
    # boundary-test false positive) and places offset vias + F.Cu stubs +
    # island bridges that stay legal at the jlcpcb standard tier (no
    # via-in-pad).  Each re-fill recomputes the pours with the new copper
    # carved in, which can shift fill edges away from a previous round's
    # bridge endpoint.  The loop converges when the copper-union audit
    # reports every pour net in one component, and short-circuits when the
    # zone filler is structurally unavailable (every fill invocation
    # failing — e.g. kicad-cli missing — means no round can ever clear the
    # zero-fill-zone audit term, so iterating just grinds repair geometry
    # against an unfillable board; mirrors board 06's #3509 short-circuit).
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
            f"\n7. Pour-connectivity repair round {repair_round} "
            f"(offset vias + stubs + island bridges)..."
        )
        try:
            rep_vias, rep_bridges = _repair_pour_connectivity(output_path, skip_nets)
            print(f"   Placed {rep_vias} repair via(s) + {rep_bridges} bridge trace(s)")
        except Exception as exc:
            print(f"   WARNING: pour-connectivity repair failed: {exc}")

        print(f"7b. Re-filling zones (round {repair_round})...")
        fill_result = subprocess.run(fill_argv, capture_output=True, text=True)
        refill_ok = fill_result.returncode == 0
        if not refill_ok:
            print(f"   Zone re-fill failed (rc={fill_result.returncode}):")
            if fill_result.stderr:
                print(f"   stderr: {fill_result.stderr.strip()}")

        print(f"7c. Copper-union pour-connectivity audit (round {repair_round})...")
        pour_ok = _run_pour_audit(f"[r{repair_round}]")
        if pour_ok:
            break
        # Issue #3617: short-circuit when the filler is structurally
        # unavailable (first pass AND this round's re-fill both failed).
        if not first_fill_ok and not refill_ok:
            print(
                "   Zone filler unavailable (every fill invocation failed) -- "
                "aborting repair loop; pours CANNOT converge without fills "
                "(Issue #3617)."
            )
            break
    if not pour_ok:
        print("   POUR CONNECTIVITY: FAIL (see above)")
    else:
        print("   POUR CONNECTIVITY: PASS")

    # Issue #3617 / #3532: the pour-repair emitter (_repair_pour_connectivity)
    # connects pad -> offset-via stubs and cross-component island bridges with
    # single straight segments to raw shapely-derived endpoints, so it ships
    # arbitrary-angle copper (~22 degrees off the 0/45/90/135 set) that
    # bypasses the router's on-grid A* output and fails the fleet 45-census
    # gate (tests/test_fleet_45_census.py).  Quantize the artifact through the
    # shared #3532 machinery (kicad_tools.router.quantize.quantize_pcb_file),
    # which replaces each off-angle segment with an EXACT two-leg dogleg
    # (45-degree leg + axis-aligned leg) that preserves the original
    # endpoints bit-for-bit -- so pour connectivity is unchanged.  Mirror the
    # softstart recipe's quantize -> re-fill fixpoint: a dogleg's small
    # perpendicular bulge can graze a foreign via barrel, so re-fill carves
    # clearance around the converged geometry before the final audit.
    print("\n8. 45-degree quantization of pour-repair copper (#3532 / #3617)...")
    try:
        from kicad_tools.router.quantize import quantize_pcb_file

        quantized = quantize_pcb_file(output_path)
        if quantized:
            print(f"   Quantized {len(quantized)} off-angle repair segment(s)")
            print("8b. Re-filling zones after quantization...")
            fill_result = subprocess.run(fill_argv, capture_output=True, text=True)
            if fill_result.returncode == 0:
                print("8c. Copper-union pour-connectivity audit (post-quantize)...")
                pour_ok = _run_pour_audit("[quant]")
                print(
                    "   POUR CONNECTIVITY (post-quantize): "
                    + ("PASS" if pour_ok else "FAIL (see above)")
                )
            else:
                print(
                    f"   Zone re-fill after quantization failed "
                    f"(rc={fill_result.returncode}); committed copper still "
                    f"connectivity-correct (dogleg preserves endpoints)."
                )
        else:
            print("   No off-angle segments: repair copper already 45-aligned")
    except Exception as exc:  # pragma: no cover - degrade gracefully
        print(f"   WARNING: 45-degree quantization step skipped: {exc}")

    return success


def run_drc(pcb_path: Path) -> bool:
    """Run kct check --mfr jlcpcb on the routed PCB."""
    print("\n" + "=" * 60)
    print("Running DRC (kct check --mfr jlcpcb)...")
    print("=" * 60)

    sidecar = pcb_path.parent / "net_class_map.json"
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
    if sidecar.exists():
        cmd.extend(["--net-class-map", str(sidecar)])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
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
        # 2026-07-05 (#3912): --skip-preflight skips BOM/ERC/LCSC/cosmetic
        # checks for this synthetic match-group test board.  It does NOT
        # suppress the connectivity safety floor added in #3912 -- if a
        # pre-existing drc_report.json next to the routed PCB contains net
        # shorts, export still aborts non-zero.  Cosmetic-check skipping is
        # safe here; shipping a shorted board is not.
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

        # Phase 4N (#2660) pattern: re-route only for the CI regression gate.
        python generate_design.py --step route --seed 42
    """
    import argparse
    import random

    parser = argparse.ArgumentParser(
        prog="generate_design",
        description="Board 07 (matchgroup-test) design generator + Phase 3N CI re-route hook.",
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=None,
        help="Output directory (default: ./output relative to this script).",
    )
    parser.add_argument(
        "--step",
        choices=["all", "schematic", "pcb", "route"],
        default="all",
        help=(
            "Run only the specified step.  ``route`` re-routes the existing "
            "committed unrouted PCB into ``output/matchgroup_test_routed.kicad_pcb``  "
            "without regenerating the schematic or unrouted PCB; used by the "
            "Phase 3N CI gate to detect routing-algorithm regressions."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Seed the global ``random`` module with N before routing for "
            "reproducible output (Issue #2589).  Required by the Phase 3N "
            "CI gate so re-routes are deterministic across PRs."
        ),
    )
    args = parser.parse_args()

    if args.output_dir is not None:
        output_dir = Path(args.output_dir)
    else:
        output_dir = Path(__file__).parent / "output"

    output_dir = output_dir.resolve()

    if args.seed is not None:
        random.seed(args.seed)
        print(f"[seed] Seeded global random with --seed {args.seed}")

    try:
        if args.step == "all":
            project_path = create_project(output_dir, "matchgroup_test")
            sch_path = create_schematic(output_dir)
            pcb_path = create_pcb(output_dir)
            routed_path = output_dir / "matchgroup_test_routed.kicad_pcb"
            route_success = route_pcb(pcb_path, routed_path)
            drc_ok = run_drc(routed_path)

            # LVS (#3779) -- copper-only hard gate.  Board 07 is a PCB-first
            # routing fixture (MIPI/TMDS connectors with no schematic-side
            # net), so the label comparator reports every pad as
            # ``schematic_net=None`` (advisory noise, not a real defect).  The
            # copper-extracted comparator correctly ignores ``None``-net pads,
            # so we gate on copper only (``run_label=False``): a copper
            # short/open raises ``BoardNetlistMismatch`` and fails the recipe.
            # Writes ``output/lvs.json`` so ``kct board-metrics`` surfaces
            # ``lvs_clean: true``.  This step only runs in ``--step all`` (the
            # ``--step route`` CI branch has no schematic).
            copper_clean, _label_clean = write_lvs_report(
                sch_path,
                routed_path,
                output_dir,
                require_clean=True,
                run_copper=True,
                run_label=False,
            )

            # Export manufacturing bundle (#3147) so ``kct fleet status``
            # reports ``ship_ready=true`` (the bundle's manifest mtime must
            # be newer than the freshly routed PCB).  Run unconditionally:
            # routing is allowed to be PARTIAL on this board, and a current
            # bundle still clears the ``"artifacts stale"`` blocker.
            mfg_ok = export_manufacturing_bundle(routed_path, output_dir)

            print("\n" + "=" * 60)
            print("SUMMARY")
            print("=" * 60)
            print(f"\nOutput dir: {output_dir}")
            print(f"  Project:   {project_path.name}")
            print(f"  Schematic: {sch_path.name}")
            print(f"  PCB:       {pcb_path.name}")
            print(f"  Routed:    {routed_path.name}")
            print("\nResults:")
            print(f"  Routing: {'SUCCESS' if route_success else 'PARTIAL'}")
            print(f"  DRC:     {'PASS' if drc_ok else 'FAIL (see above)'}")
            # #3912: mfg_ok means `kct export` wrote a bundle (exit 0), not
            # that the board is DRC-clean.  Board DRC status is the "DRC:"
            # line above.  Label "WRITTEN" so PASS is never mistaken for a
            # DRC pass.
            print(f"  MFG:     {'WRITTEN' if mfg_ok else 'FAILED (see above)'}")

            return 0 if route_success else 1

        if args.step == "schematic":
            create_schematic(output_dir)
            return 0

        if args.step == "pcb":
            create_pcb(output_dir)
            return 0

        if args.step == "route":
            pcb_path = output_dir / "matchgroup_test.kicad_pcb"
            if not pcb_path.exists():
                print(
                    f"Error: unrouted PCB not found at {pcb_path}.  Run "
                    "``python generate_design.py --step pcb`` first or "
                    "use ``--step all``.",
                    file=sys.stderr,
                )
                return 1
            routed_path = output_dir / "matchgroup_test_routed.kicad_pcb"
            route_pcb(pcb_path, routed_path)
            if not routed_path.exists():
                print(
                    f"Error: routed PCB not written to {routed_path}.",
                    file=sys.stderr,
                )
                return 1
            return 0

        print(f"Error: unknown step {args.step!r}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
