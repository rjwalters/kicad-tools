"""Engine unit tests for the HV creepage/clearance audit (Issue #4327).

Covers:

* the pure surface-path geometry (``surface_path_length``): creepage ==
  clearance with no intervening slot; creepage > clearance (by the detour
  length) when a slot lies on the straight path; a mounting-hole cutout that
  is NOT between the pair does not lengthen the path; multiple slots in
  series.
* the board-level census (``compute_creepage_census``): a slot between the HV
  net and a neighbor yields creepage > clearance; the same board without the
  slot yields creepage == clearance; a board with no HV nets yields an empty
  census.
* HV net selection via a net-class map with a name-pattern fallback.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools._shapely import has_shapely
from kicad_tools.creepage.engine import (
    BOARD_EDGE_LABEL,
    compute_creepage_census,
    is_mains_suspect_name,
    mains_suspect_nets,
    resolve_hv_nets,
    surface_path_length,
)

from .fixtures import (
    board_mains_named_source,
    board_no_hv_source,
    board_same_footprint_fail_source,
    board_same_footprint_only_source,
    board_source,
)

pytestmark = pytest.mark.skipif(not has_shapely(), reason="creepage requires shapely")


# ---------------------------------------------------------------------------
# Pure geometry: surface_path_length
# ---------------------------------------------------------------------------


def _box(x0, y0, x1, y1):
    from shapely.geometry import box

    return box(x0, y0, x1, y1)


def test_no_obstacle_creepage_equals_clearance():
    a = _box(0.0, -0.5, 1.0, 0.5)
    b = _box(9.0, -0.5, 10.0, 0.5)
    clearance, creepage = surface_path_length(a, b, obstacles=[])
    assert clearance == pytest.approx(8.0, abs=1e-6)
    assert creepage == pytest.approx(clearance, abs=1e-9)


def test_slot_on_path_lengthens_creepage():
    # Thin copper centered on y=0 so nearest points are (0,0) and (10,0).
    a = _box(-1.0, -1e-3, 0.0, 1e-3)
    b = _box(10.0, -1e-3, 11.0, 1e-3)
    slot = _box(4.9, -3.0, 5.1, 3.0)  # tall bar straddling the straight path

    clearance, creepage = surface_path_length(a, b, obstacles=[slot])
    assert clearance == pytest.approx(10.0, abs=1e-3)
    # Detour: (0,0)->(4.9,3)->(5.1,3)->(10,0) (or the symmetric bottom route).
    expected = 2 * math.hypot(4.9, 3.0) + 0.2
    assert creepage > clearance
    assert creepage == pytest.approx(expected, abs=0.05)


def test_offset_cutout_not_between_pair_does_not_lengthen():
    # A mounting-hole cutout well above the straight path must not detour it.
    a = _box(0.0, -0.5, 1.0, 0.5)
    b = _box(9.0, -0.5, 10.0, 0.5)
    hole = _box(4.5, 5.0, 5.5, 6.0)  # entirely off the y~0 straight line

    clearance, creepage = surface_path_length(a, b, obstacles=[hole])
    assert creepage == pytest.approx(clearance, abs=1e-9)


def test_multiple_slots_in_series_sum_detours():
    a = _box(-1.0, -1e-3, 0.0, 1e-3)
    b = _box(20.0, -1e-3, 21.0, 1e-3)
    slot1 = _box(4.9, -3.0, 5.1, 3.0)
    slot2 = _box(14.9, -3.0, 15.1, 3.0)

    clearance, creepage = surface_path_length(a, b, obstacles=[slot1, slot2])
    assert clearance == pytest.approx(20.0, abs=1e-3)
    # Two slots each force a detour up to y=3 and back -- creepage exceeds the
    # single-slot detour and, of course, the straight clearance.
    assert creepage > clearance + 1.0


def test_overlapping_geometries_have_zero_creepage():
    a = _box(0.0, 0.0, 5.0, 5.0)
    b = _box(4.0, 0.0, 9.0, 5.0)  # overlaps a
    clearance, creepage = surface_path_length(a, b, obstacles=[_box(2.0, 2.0, 3.0, 3.0)])
    assert clearance <= 0.0
    assert creepage == clearance


# ---------------------------------------------------------------------------
# Board-level census
# ---------------------------------------------------------------------------


def _load(tmp_path, source, name="board.kicad_pcb"):
    from kicad_tools.schema.pcb import PCB

    p = tmp_path / name
    p.write_text(source)
    return PCB.load(p)


def _hv_map():
    from kicad_tools.router.rules import net_class_map_from_dict

    return net_class_map_from_dict({"L_MAINS": {"name": "HV"}})


def _conductor_pair(report, net_b):
    for pair in report.pairs:
        if pair.kind == "conductor" and pair.net_b == net_b:
            return pair
    raise AssertionError(f"no conductor pair against {net_b!r} in census")


def test_census_no_slot_creepage_equals_clearance(tmp_path):
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    assert set(hv.values()) == {"L_MAINS"}

    report = compute_creepage_census(pcb, hv, min_mm=1.5)
    pair = _conductor_pair(report, "GND")
    assert pair.clearance_mm == pytest.approx(18.0, abs=1e-3)
    assert pair.creepage_mm == pytest.approx(pair.clearance_mm, abs=1e-6)


def test_census_slot_lengthens_creepage(tmp_path):
    pcb = _load(tmp_path, board_source(with_slot=True))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = compute_creepage_census(pcb, hv, min_mm=1.5)

    pair = _conductor_pair(report, "GND")
    assert pair.clearance_mm == pytest.approx(18.0, abs=1e-3)
    # The milled slot forces the surface path to detour around it.
    assert pair.creepage_mm > pair.clearance_mm + 2.0
    # Clearance and creepage are genuinely distinct values.
    assert pair.creepage_mm != pytest.approx(pair.clearance_mm)


def test_census_reports_board_edge_pair(tmp_path):
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = compute_creepage_census(pcb, hv, min_mm=1.5)

    edge_pairs = [p for p in report.pairs if p.kind == "edge"]
    assert len(edge_pairs) == 1
    assert edge_pairs[0].net_b == BOARD_EDGE_LABEL
    assert edge_pairs[0].net_a == "L_MAINS"
    # L_MAINS pad edge at x=111 vs board edge at x=100 -> 11 mm to the wall.
    assert edge_pairs[0].clearance_mm == pytest.approx(9.0, abs=1.5)


def test_pass_flag_and_margin(tmp_path):
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())

    report = compute_creepage_census(pcb, hv, min_mm=1.5)
    assert report.passed  # all gaps are >> 1.5 mm
    pair = _conductor_pair(report, "GND")
    assert pair.margin_mm == pytest.approx(pair.creepage_mm - 1.5, abs=1e-6)

    strict = compute_creepage_census(pcb, hv, min_mm=100.0)
    assert not strict.passed  # 18 mm < 100 mm required
    assert not _conductor_pair(strict, "GND").passed


def test_no_hv_nets_yields_empty_census(tmp_path):
    pcb = _load(tmp_path, board_no_hv_source())
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    assert hv == {}

    report = compute_creepage_census(pcb, hv, min_mm=1.5)
    assert report.pairs == []
    assert report.hv_nets == []
    assert report.passed  # vacuously true -> exit 0


def test_name_pattern_fallback_without_map(tmp_path):
    # No map: 'GND' matches the built-in GROUND name pattern, so --net-class
    # ground selects it via classify_from_name (no new classifier).
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "ground", net_class_map=None)
    assert set(hv.values()) == {"GND"}


# ---------------------------------------------------------------------------
# Mains/HV name detection + broadened HV fallback (issue #4354)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "AC_LINE",
        "AC_NEUTRAL",
        "ACLINE",
        "FUSED_LINE",
        "FUSED",
        "MAINS_L",
        "L_MAINS",
        "MAINS_IN",
        "HV_BUS",
        "HV",
        "L_LINE",
        "N_LINE",
        "LIVE",
        "NEUTRAL",
        "/AC_LINE",  # hierarchical leading slash
    ],
)
def test_is_mains_suspect_name_positive(name):
    assert is_mains_suspect_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "GND",
        "SDA",
        "SCL",
        "USB_DP",
        "USB_DM",
        "SIG",
        "VCC",
        "ONLINE",  # 'LINE' substring, no token boundary -> must NOT match
        "REMAINS",  # 'MAINS' substring, no token boundary -> must NOT match
        # Bare LINE/HOT/PRIMARY tokens no longer match (issue #4365): they
        # over-matched benign nets and were dropped from MAINS_NAME_RE.
        "PRIMARY",
        "LINE",
        "HOT",
        "LINE_A",
        "SPI_LINE",
        "LINE_IN",
        "HOT_SWAP",
        "PRIMARY_CLK",
        "KEEP_ALIVE",  # 'LIVE' preceded by 'A', not a token boundary
        "",
        None,
    ],
)
def test_is_mains_suspect_name_negative(name):
    assert is_mains_suspect_name(name) is False


def test_mains_suspect_nets_lists_only_mains_named(tmp_path):
    pcb = _load(tmp_path, board_mains_named_source())
    # GND is excluded; the three mains nets are listed (sorted).
    assert mains_suspect_nets(pcb) == ["AC_LINE", "AC_NEUTRAL", "FUSED_LINE"]


def test_broadened_hv_fallback_classifies_mains_without_map(tmp_path):
    # The NetClass enum has no HV member, so this only works via the #4354
    # broadened mains/HV name fallback -- with NO net-class-map at all.
    pcb = _load(tmp_path, board_mains_named_source())
    hv = resolve_hv_nets(pcb, "HV", net_class_map=None)
    assert set(hv.values()) == {"AC_LINE", "AC_NEUTRAL", "FUSED_LINE"}
    assert "GND" not in set(hv.values())


def test_explicit_map_still_governs_over_fallback(tmp_path):
    # A map that classifies the mains nets as a NON-HV class must exclude them
    # from the HV group (explicit operator classification wins; the broadened
    # fallback never double-adds a mapped net).
    from kicad_tools.router.rules import net_class_map_from_dict

    pcb = _load(tmp_path, board_mains_named_source())
    generic = net_class_map_from_dict(
        {
            "AC_LINE": {"name": "Power"},
            "AC_NEUTRAL": {"name": "Power"},
            "FUSED_LINE": {"name": "Power"},
        }
    )
    hv = resolve_hv_nets(pcb, "HV", generic)
    assert hv == {}


def test_broadened_fallback_only_applies_to_hv_target(tmp_path):
    # Asking for a non-HV class must NOT pull in mains-named nets via the #4354
    # fallback (it is gated on target == "hv").
    pcb = _load(tmp_path, board_mains_named_source())
    hv = resolve_hv_nets(pcb, "power", net_class_map=None)
    assert "AC_LINE" not in set(hv.values())


# ---------------------------------------------------------------------------
# Per-net voltage model + pairwise |dV| requirement (Issue #4371)
# ---------------------------------------------------------------------------


def _std():
    from kicad_tools.creepage.standards import get_standard

    return get_standard("iec60664")


def _census_map(pcb, hv, vmap, *, edge=0.0, pd=2, mg="IIIa"):
    from kicad_tools.creepage.engine import voltage_map_from_dict

    std = _std()
    # Route through the shared parser so the census receives the interval-typed
    # map exactly as the CLI does (scalars -> degenerate intervals, #4411).
    parsed, _edge = voltage_map_from_dict(vmap)
    return compute_creepage_census(
        pcb,
        hv,
        None,
        standard="iec60664",
        standard_edition=std.edition,
        pollution_degree=pd,
        material_group=mg,
        voltage_map=parsed,
        standard_obj=std,
        edge_voltage=edge,
    )


def _both_hv_map():
    from kicad_tools.router.rules import net_class_map_from_dict

    return net_class_map_from_dict({"L_MAINS": {"name": "HV"}, "GND": {"name": "HV"}})


def test_voltage_map_from_dict_parses_nets_reserved_keys_and_edge():
    from kicad_tools.creepage.engine import VoltageInterval, voltage_map_from_dict

    voltages, edge = voltage_map_from_dict(
        {
            "/AC_LINE": 150,
            "/AC_NEUTRAL": 0,
            "_edge_voltage": 12.0,
            "_comment": "ignored documentation",
        }
    )
    # Each scalar entry becomes a degenerate interval (v, v) (#4411).
    assert voltages == {
        "/AC_LINE": VoltageInterval(150.0, 150.0),
        "/AC_NEUTRAL": VoltageInterval(0.0, 0.0),
    }
    assert all(iv.is_degenerate for iv in voltages.values())
    assert edge == 12.0


def test_voltage_map_from_dict_accepts_range_and_normalises_order():
    # A {min,max} entry becomes a closed interval; author order is irrelevant
    # (both {min,max} and {max,min} normalise to (min, max)) (#4411).
    from kicad_tools.creepage.engine import VoltageInterval, voltage_map_from_dict

    voltages, _edge = voltage_map_from_dict(
        {
            "/SRC_NEG": {"min": -170, "max": 90},
            "/SRC_NEG_REV": {"min": 90, "max": -170},  # inverted author order
            "/STATIC": 42,
        }
    )
    assert voltages["/SRC_NEG"] == VoltageInterval(-170.0, 90.0)
    assert voltages["/SRC_NEG_REV"] == VoltageInterval(-170.0, 90.0)
    # A scalar alongside ranges stays degenerate.
    assert voltages["/STATIC"] == VoltageInterval(42.0, 42.0)


@pytest.mark.parametrize(
    "bad",
    [
        {"NET": {"min": 0}},  # missing 'max'
        {"NET": {"max": 0}},  # missing 'min'
        {"NET": {"min": 0, "max": 1, "typ": 0.5}},  # extra key
        {"NET": {"min": "x", "max": 1}},  # non-numeric endpoint
        {"NET": {"min": 0, "max": True}},  # bool endpoint
        {"NET": {"min": 0, "max": float("nan")}},  # NaN endpoint
        {"_edge_voltage": {"min": 0, "max": 1}},  # edge stays scalar-only
    ],
)
def test_voltage_map_from_dict_rejects_malformed_range(bad):
    from kicad_tools.creepage.engine import voltage_map_from_dict

    with pytest.raises(ValueError):
        voltage_map_from_dict(bad)


@pytest.mark.parametrize(
    "bad",
    [
        {"NET": "not-a-number"},
        {"NET": True},  # bool is not a voltage
        {"NET": None},
        {"NET": float("nan")},
        {"_edge_voltage": "x"},
    ],
)
def test_voltage_map_from_dict_rejects_non_numeric(bad):
    from kicad_tools.creepage.engine import voltage_map_from_dict

    with pytest.raises((ValueError, TypeError)):
        voltage_map_from_dict(bad)


def test_voltage_map_from_dict_rejects_non_dict():
    from kicad_tools.creepage.engine import voltage_map_from_dict

    with pytest.raises(TypeError):
        voltage_map_from_dict([("A", 1)])


def test_same_potential_pair_requires_zero_and_skips_lookup(tmp_path):
    # Two nets at equal mapped voltage -> dv == 0 -> required 0.0, trivial PASS,
    # and NO standard-table lookup (no creepage provenance attached).
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census_map(pcb, hv, {"L_MAINS": 90.0, "GND": 90.0})
    pair = _conductor_pair(report, "GND")
    assert pair.required_creepage_mm == 0.0
    assert pair.required_clearance_mm == 0.0
    assert pair.passed is True
    v = pair.provenance["voltage"]
    assert v["delta_v_v"] == 0.0
    assert v["same_potential"] is True
    assert "creepage" not in pair.provenance  # lookup was short-circuited


def test_cross_domain_pair_matches_flat_voltage_lookup(tmp_path):
    # V_a=150, V_b=0 -> requirement equals the flat-150V step-up row.
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census_map(pcb, hv, {"L_MAINS": 150.0})  # GND unmapped -> 0 V
    pair = _conductor_pair(report, "GND")
    expected, _ = _std().required_creepage(150.0, 2, "IIIa")
    assert pair.required_creepage_mm == pytest.approx(expected)
    assert pair.provenance["voltage"]["delta_v_v"] == 150.0


def test_sub_50v_steps_up_to_nearest_tabulated_row(tmp_path):
    # dv=30 V steps up to the 32 V row of Table F.4 (issue #4402: the sub-50 V
    # rows are now tabulated, so a 30 V pair no longer jumps to the 50 V row).
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census_map(pcb, hv, {"L_MAINS": 30.0})
    pair = _conductor_pair(report, "GND")
    expected, _ = _std().required_creepage(32.0, 2, "IIIa")
    assert pair.required_creepage_mm == pytest.approx(expected)
    assert pair.provenance["creepage"]["voltage_row_used_v"] == 32.0


def test_board_edge_uses_edge_voltage_default_zero(tmp_path):
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census_map(pcb, hv, {"L_MAINS": 90.0})
    edge = next(p for p in report.pairs if p.kind == "edge")
    assert edge.provenance["voltage"]["delta_v_v"] == 90.0  # |90 - 0|


def test_board_edge_voltage_override(tmp_path):
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census_map(pcb, hv, {"L_MAINS": 90.0}, edge=90.0)
    edge = next(p for p in report.pairs if p.kind == "edge")
    assert edge.provenance["voltage"]["delta_v_v"] == 0.0  # |90 - 90|
    assert edge.required_creepage_mm == 0.0


def test_map_present_net_absent_from_copper_is_ignored(tmp_path):
    # A net named in the map but with no copper must not crash or add pairs.
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census_map(pcb, hv, {"L_MAINS": 150.0, "NOT_ON_BOARD": 300.0})
    assert all(p.net_a != "NOT_ON_BOARD" and p.net_b != "NOT_ON_BOARD" for p in report.pairs)


def test_hv_vs_hv_pair_formed_in_map_mode(tmp_path):
    # With BOTH nets in the HV class, map mode relaxes the HV-vs-HV skip so a
    # same-class pair at different potentials is evaluated (bank-vs-bank).
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _both_hv_map())
    assert set(hv.values()) == {"L_MAINS", "GND"}
    report = _census_map(pcb, hv, {"L_MAINS": 150.0, "GND": 0.0})
    conductor = [p for p in report.pairs if p.kind == "conductor"]
    # Exactly one canonical HV-HV conductor pair (not both directions).
    assert len(conductor) == 1
    assert conductor[0].provenance["voltage"]["delta_v_v"] == 150.0


def test_hv_vs_hv_skipped_without_map(tmp_path):
    # Legacy single-voltage mode still skips HV-vs-HV pairs entirely.
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _both_hv_map())
    report = compute_creepage_census(pcb, hv, min_mm=1.5)
    assert [p for p in report.pairs if p.kind == "conductor"] == []


def test_over_range_delta_v_raises_loud(tmp_path):
    from kicad_tools.creepage.standards import StandardLookupError

    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    with pytest.raises(StandardLookupError):
        _census_map(pcb, hv, {"L_MAINS": 1_000_000.0})


def test_no_voltage_map_leaves_report_in_single_voltage_mode(tmp_path):
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = compute_creepage_census(pcb, hv, min_mm=1.5)
    assert report.voltage_map is None
    assert report.uses_voltage_map is False


# ---------------------------------------------------------------------------
# Worst-case interval ΔV (Issue #4411): swinging nodes can no longer hide the
# real pairwise stress behind a single dominant static value.
# ---------------------------------------------------------------------------


def test_swinging_node_not_reported_same_potential(tmp_path):
    # SCAP_POS(+90) vs TRK_POS[-146..+90]: the two nets share their DOMINANT
    # state (+90) so a scalar model would derive dv == 0 (a same-potential false
    # PASS).  The interval model derives the worst-case excursion:
    #   dv = max(|90 - 90|, |90 - (-146)|) = 236 V  -> a mains-class requirement.
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _both_hv_map())
    report = _census_map(
        pcb,
        hv,
        {"L_MAINS": {"min": -146.0, "max": 90.0}, "GND": 90.0},
    )
    conductor = [p for p in report.pairs if p.kind == "conductor"]
    assert len(conductor) == 1
    pair = conductor[0]
    v = pair.provenance["voltage"]
    assert v["same_potential"] is False
    assert v["delta_v_v"] == pytest.approx(236.0)
    # A real, non-trivial creepage requirement is derived (NOT the 0.0 of a
    # same-potential pair).
    assert pair.required_creepage_mm is not None and pair.required_creepage_mm > 0.0
    expected, _ = _std().required_creepage(236.0, 2, "IIIa")
    assert pair.required_creepage_mm == pytest.approx(expected)


def test_worst_case_provenance_records_governing_endpoints(tmp_path):
    # The binding ΔV came from L_MAINS's low endpoint against GND's high endpoint
    # (or the symmetric assignment) -- provenance must name which endpoints won.
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _both_hv_map())
    report = _census_map(
        pcb,
        hv,
        {"L_MAINS": {"min": -146.0, "max": 90.0}, "GND": 90.0},
    )
    pair = next(p for p in report.pairs if p.kind == "conductor")
    v = pair.provenance["voltage"]
    assert {"net_a_endpoint", "net_b_endpoint"} <= set(v)
    # The governing endpoints are the two that are 236 V apart: one net's -146
    # extreme and the other's +90.  net_a_v / net_b_v echo those governing values.
    assert {v["net_a_v"], v["net_b_v"]} == {-146.0, 90.0}


def test_edge_pair_uses_worst_case_swing_vs_edge(tmp_path):
    # A net swinging about earth: TRK[-146..+90] vs the board edge (0 V) must use
    # the worst-case magnitude 146 V, not the +90 dominant value.
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census_map(pcb, hv, {"L_MAINS": {"min": -146.0, "max": 90.0}})
    edge = next(p for p in report.pairs if p.kind == "edge")
    assert edge.provenance["voltage"]["delta_v_v"] == pytest.approx(146.0)


def test_degenerate_range_equals_scalar_and_stays_byte_identical(tmp_path):
    # {min:v, max:v} is exactly the scalar v: same requirement, same provenance
    # (including NO endpoint keys, so scalar-map JSON is unchanged).
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    scalar = _census_map(pcb, hv, {"L_MAINS": 150.0})
    ranged = _census_map(pcb, hv, {"L_MAINS": {"min": 150.0, "max": 150.0}})
    ps = _conductor_pair(scalar, "GND")
    pr = _conductor_pair(ranged, "GND")
    assert ps.required_creepage_mm == pr.required_creepage_mm
    assert ps.provenance == pr.provenance
    # Degenerate pairs carry NO endpoint provenance (arbitrary when lo == hi).
    assert "net_a_endpoint" not in ps.provenance["voltage"]


def test_scalar_map_report_echo_serialises_as_scalar(tmp_path):
    # Backward-compat: an all-scalar map echoes bare scalars in to_dict (no
    # {min,max} wrapping), while a genuine range surfaces as {min,max}.
    pcb = _load(tmp_path, board_source(with_slot=False))
    hv = resolve_hv_nets(pcb, "HV", _both_hv_map())
    scalar = _census_map(pcb, hv, {"L_MAINS": 150.0, "GND": 0.0}).to_dict()
    assert scalar["voltage_map"] == {"L_MAINS": 150.0, "GND": 0.0}
    ranged = _census_map(pcb, hv, {"L_MAINS": {"min": -146.0, "max": 90.0}, "GND": 0.0}).to_dict()
    assert ranged["voltage_map"] == {"L_MAINS": {"min": -146.0, "max": 90.0}, "GND": 0.0}


def test_resolve_hv_union_pulls_in_net_swinging_across_threshold(tmp_path):
    # A net whose DOMINANT state is below threshold but whose swing crosses it
    # must still be pulled into the census by worst-case magnitude (#4411).
    from kicad_tools.creepage.engine import VoltageInterval

    pcb = _load(tmp_path, board_no_hv_source())  # nets: SIG, GND
    # SIG dominant +5 V (below 30 V), but swings to -150 V -> worst-case 150 V.
    hv = resolve_hv_nets(
        pcb,
        "HV",
        net_class_map=None,
        voltage_map={"SIG": VoltageInterval(-150.0, 5.0)},
        census_threshold=30.0,
    )
    assert set(hv.values()) == {"SIG"}

    # Dominant +5 V AND a shallow -5 V swing: worst-case 5 V stays below 30 V.
    hv2 = resolve_hv_nets(
        pcb,
        "HV",
        net_class_map=None,
        voltage_map={"SIG": VoltageInterval(-5.0, 5.0)},
        census_threshold=30.0,
    )
    assert hv2 == {}


# ---------------------------------------------------------------------------
# Voltage-derived census membership union (issue #4401)
# ---------------------------------------------------------------------------


def test_voltage_union_selects_high_v_non_hv_class_net(tmp_path):
    # The repro: a high-|V| net (SIG at 150 V) that is NEITHER mains-named NOR
    # class-HV. Without a voltage map it is invisible to the census; with the
    # map + threshold it is pulled in by the voltage-derived union.
    pcb = _load(tmp_path, board_no_hv_source())  # nets: SIG, GND
    base = resolve_hv_nets(pcb, "HV", net_class_map=None)
    assert base == {}  # neither SIG nor GND is HV by class/name

    hv = resolve_hv_nets(
        pcb,
        "HV",
        net_class_map=None,
        voltage_map={"SIG": 150.0},
        census_threshold=30.0,
    )
    assert set(hv.values()) == {"SIG"}


def test_voltage_union_is_union_not_replace(tmp_path):
    # A class-HV net at a LOW/unmapped voltage must remain selected even though
    # the voltage-derived pass would not add it (union, not replace); a separate
    # high-|V| non-HV net is added on top.
    pcb = _load(tmp_path, board_source(with_slot=False))  # L_MAINS (HV via map), GND
    hv = resolve_hv_nets(
        pcb,
        "HV",
        _hv_map(),  # classifies L_MAINS as HV
        voltage_map={"L_MAINS": 5.0, "GND": 150.0},  # L_MAINS below threshold
        census_threshold=30.0,
    )
    # L_MAINS kept via class selection despite 5 V; GND added via voltage union.
    assert set(hv.values()) == {"L_MAINS", "GND"}


def test_voltage_union_edge_voltage_shifts_reference(tmp_path):
    # Membership keys on |V - edge_voltage|, not raw |V|.  With edge_voltage=140
    # a 150 V net is only 10 V above the reference -> below the 30 V threshold.
    pcb = _load(tmp_path, board_no_hv_source())
    hv = resolve_hv_nets(
        pcb,
        "HV",
        net_class_map=None,
        voltage_map={"SIG": 150.0},
        edge_voltage=140.0,
        census_threshold=30.0,
    )
    assert hv == {}

    # Same net, edge_voltage=0 -> 150 V >= 30 V -> selected.
    hv2 = resolve_hv_nets(
        pcb,
        "HV",
        net_class_map=None,
        voltage_map={"SIG": 150.0},
        edge_voltage=0.0,
        census_threshold=30.0,
    )
    assert set(hv2.values()) == {"SIG"}


def test_voltage_union_threshold_boundary_is_inclusive(tmp_path):
    # A net exactly AT the threshold is selected (>= boundary).
    pcb = _load(tmp_path, board_no_hv_source())
    hv = resolve_hv_nets(
        pcb,
        "HV",
        net_class_map=None,
        voltage_map={"SIG": 30.0},
        census_threshold=30.0,
    )
    assert set(hv.values()) == {"SIG"}

    # Just below the threshold -> excluded.
    hv2 = resolve_hv_nets(
        pcb,
        "HV",
        net_class_map=None,
        voltage_map={"SIG": 29.999},
        census_threshold=30.0,
    )
    assert hv2 == {}


def test_voltage_union_key_normalization_matches_census(tmp_path):
    # A leading-'/' voltage-map key resolves the same net as the bare name
    # (reuses _norm_net_key, matching the census's own lookup convention).
    pcb = _load(tmp_path, board_no_hv_source())
    hv = resolve_hv_nets(
        pcb,
        "HV",
        net_class_map=None,
        voltage_map={"/SIG": 150.0},  # hierarchical leading slash
        census_threshold=30.0,
    )
    assert set(hv.values()) == {"SIG"}


def test_voltage_union_negative_potential_uses_magnitude(tmp_path):
    # A -150 V net is |−150 − 0| = 150 V from the edge reference -> selected.
    pcb = _load(tmp_path, board_no_hv_source())
    hv = resolve_hv_nets(
        pcb,
        "HV",
        net_class_map=None,
        voltage_map={"SIG": -150.0},
        census_threshold=30.0,
    )
    assert set(hv.values()) == {"SIG"}


def test_no_map_output_byte_identical_to_baseline(tmp_path):
    # The no-op path: passing voltage_map=None (or census_threshold=None) must
    # yield exactly the class/name selection with no voltage influence.
    pcb = _load(tmp_path, board_source(with_slot=False))
    baseline = resolve_hv_nets(pcb, "HV", _hv_map())

    # census_threshold=None disables the union even when a map is present.
    same_no_threshold = resolve_hv_nets(
        pcb, "HV", _hv_map(), voltage_map={"GND": 150.0}, census_threshold=None
    )
    assert same_no_threshold == baseline

    # voltage_map=None disables the union even when a threshold is present.
    same_no_map = resolve_hv_nets(pcb, "HV", _hv_map(), voltage_map=None, census_threshold=30.0)
    assert same_no_map == baseline


# ---------------------------------------------------------------------------
# Same-footprint classification + gate_passed (Issue #4403)
# ---------------------------------------------------------------------------


def _census_samefp(pcb, min_mm=1.0):
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    return compute_creepage_census(pcb, hv, min_mm=min_mm)


def test_intra_footprint_pad_gap_classifies_same_footprint(tmp_path):
    # FET1 holds L_MAINS + SRC_NEG pads 0.4 mm apart; SRC_NEG exists on no other
    # footprint, so the binding L_MAINS<->SRC_NEG gap is component-internal.
    report = _census_samefp(_load(tmp_path, board_same_footprint_fail_source()))
    assert _conductor_pair(report, "SRC_NEG").relationship == "same_footprint"


def test_net_to_net_board_approach_classifies_board(tmp_path):
    # P1(L_MAINS) and P2(GND) are distinct footprints 0.4 mm apart -> board.
    report = _census_samefp(_load(tmp_path, board_same_footprint_fail_source()))
    assert _conductor_pair(report, "GND").relationship == "board"


def test_shared_footprint_but_board_binds_classifies_board(tmp_path):
    # FET2 holds L_MAINS + DIV_MID 1.7 mm apart, but P3/P4 approach to 0.4 mm.
    # Because the binding minimum (0.4) is NOT the intra-footprint gap (1.7),
    # the pair is board-level -- the equality-check guard.
    report = _census_samefp(_load(tmp_path, board_same_footprint_fail_source()))
    div = _conductor_pair(report, "DIV_MID")
    assert div.clearance_mm == pytest.approx(0.4, abs=1e-3)
    assert div.relationship == "board"


def test_edge_pair_is_always_board(tmp_path):
    report = _census_samefp(_load(tmp_path, board_same_footprint_fail_source()))
    edge = next(p for p in report.pairs if p.kind == "edge")
    assert edge.relationship == "board"


def test_relationship_defaults_board_for_plain_board(tmp_path):
    # The single-pad-per-footprint board has no same-footprint pairs at all.
    pcb = _load(tmp_path, board_source(with_slot=False))
    report = compute_creepage_census(pcb, resolve_hv_nets(pcb, "HV", _hv_map()), min_mm=1.5)
    assert all(p.relationship == "board" for p in report.pairs)


def test_gate_passed_excludes_waived_pairs_but_passed_does_not(tmp_path):
    # A board whose ONLY sub-requirement fail is a same-footprint pair: once that
    # pair is waived, gate_passed is True while the raw passed stays False.
    report = _census_samefp(_load(tmp_path, board_same_footprint_only_source()))
    assert report.passed is False  # SRC_NEG (same_footprint) fails
    assert report.gate_passed is False  # nothing waived yet

    for p in report.pairs:
        if p.relationship == "same_footprint":
            p.waived = True
    assert report.gate_passed is True  # waived pair drops out of the gate
    assert report.passed is False  # raw result still counts it (safety gate)


def test_gate_passed_still_fails_on_a_board_pair_after_waiver(tmp_path):
    # With a board-level fail present, waiving same-footprint pairs must NOT
    # rescue the gate.
    report = _census_samefp(_load(tmp_path, board_same_footprint_fail_source()))
    for p in report.pairs:
        if p.relationship == "same_footprint":
            p.waived = True
    assert report.gate_passed is False  # GND / DIV_MID board fails remain
    assert report.passed is False


def test_relationship_serialized_always_and_waived_only_when_true(tmp_path):
    report = _census_samefp(_load(tmp_path, board_same_footprint_only_source()))
    same_fp = _conductor_pair(report, "SRC_NEG")
    board = _conductor_pair(report, "GND")
    # relationship is always present; waived only appears once set True.
    assert same_fp.to_dict()["relationship"] == "same_footprint"
    assert "waived" not in same_fp.to_dict()
    same_fp.waived = True
    assert same_fp.to_dict()["waived"] is True
    assert board.to_dict()["relationship"] == "board"
    assert "waived" not in board.to_dict()
