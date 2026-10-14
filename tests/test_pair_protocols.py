"""The protocol defaults table and the one resolver that reads it (Issue #6443).

Piece (a) of P1, epic #6441: one table for skew, uncoupled-length allowance,
gap and via count; every value sourced or tagged ``uncited``; a cited or
explicit limit is an error and an uncited one a warning.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.router.diffpair import DifferentialPairRules, DifferentialPairType
from kicad_tools.router.pair_protocols import (
    GENERIC_SKEW_MM,
    PROTOCOL_DEFAULTS,
    SKEW_STRETCH_TARGET_MM,
    UNCITED,
    PairProtocol,
    Sourced,
    parse_protocol,
)
from kicad_tools.router.rules import NetClassRouting, net_class_map_from_path
from kicad_tools.validate.diffpair_engagement import resolve_pair_thresholds
from kicad_tools.validate.rules.diffpair_length_skew import (
    DEFAULT_SKEW_TOLERANCE_MM,
    DiffPairLengthSkewRule,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARDS = REPO_ROOT / "boards"
B03 = BOARDS / "03-usb-joystick"


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def test_every_protocol_has_a_row_and_every_value_a_source():
    assert set(PROTOCOL_DEFAULTS) == set(PairProtocol)
    seen = 0
    for protocol, row in PROTOCOL_DEFAULTS.items():
        assert row.protocol is protocol
        for name, value in row.sourced_values().items():
            seen += 1
            assert isinstance(value, Sourced), (protocol, name)
            assert value.source.strip(), f"{protocol.name}.{name} has an empty source"
            assert value.cited == (value.source != UNCITED)
            assert value.value > 0
    assert seen > 20


def test_no_bare_numbers_in_the_table():
    """A row field is a ``Sourced`` or ``None`` -- never a number with no provenance."""
    for row in PROTOCOL_DEFAULTS.values():
        for name, value in vars(row).items():
            if name != "protocol":
                assert value is None or isinstance(value, Sourced), (row.protocol, name)


def test_the_cited_values():
    fs = PROTOCOL_DEFAULTS[PairProtocol.USB2_FS]
    hs = PROTOCOL_DEFAULTS[PairProtocol.USB2_HS]
    assert (fs.max_skew_mm.value, fs.max_skew_mm.cited) == (2.5, True)
    assert "Renesas" in fs.max_skew_mm.source
    assert (hs.max_skew_mm.value, hs.max_skew_mm.cited) == (1.27, True)
    assert "TI SPRAAR7J" in hs.max_skew_mm.source
    for row in (fs, hs):
        assert row.max_uncoupled_per_terminal_mm.value == 6.35
        assert "TI SPRAAR7J" in row.max_uncoupled_per_terminal_mm.source
        assert (row.max_vias_per_net.value, row.max_vias_per_net.cited) == (4, True)
    # TI SPRAAR7J is high-speed guidance.  The full-speed row says it borrows it.
    for borrowed in (fs.max_uncoupled_per_terminal_mm, fs.max_vias_per_net):
        assert "high-speed guidance, applied to full speed" in borrowed.source
    for own in (hs.max_uncoupled_per_terminal_mm, hs.max_vias_per_net, fs.max_skew_mm):
        assert "applied to full speed" not in own.source


def test_only_the_usb2_rows_are_cited_today():
    """A new citation should be a deliberate edit, with this test updated."""
    cited = {
        p.name
        for p, row in PROTOCOL_DEFAULTS.items()
        if row.max_skew_mm is not None and row.max_skew_mm.cited
    }
    assert cited == {"USB2_FS", "USB2_HS"}


def test_generic_default_matches_the_rule_default():
    assert GENERIC_SKEW_MM == DEFAULT_SKEW_TOLERANCE_MM == 0.5
    assert SKEW_STRETCH_TARGET_MM == 0.5


def test_parse_protocol():
    assert parse_protocol(None) is None
    assert parse_protocol("usb2-fs") is PairProtocol.USB2_FS
    assert parse_protocol(PairProtocol.LVDS) is PairProtocol.LVDS
    with pytest.raises(ValueError, match="unknown pair protocol 'USB2_XS'"):
        parse_protocol("USB2_XS")


# ---------------------------------------------------------------------------
# The legacy table is a shim over it
# ---------------------------------------------------------------------------

_TYPE_ROW = {
    DifferentialPairType.USB2: PairProtocol.USB2_FS,
    DifferentialPairType.USB3: PairProtocol.USB3_SS,
    DifferentialPairType.ETHERNET: PairProtocol.ETHERNET,
    DifferentialPairType.HDMI: PairProtocol.HDMI_TMDS,
    DifferentialPairType.LVDS: PairProtocol.LVDS,
    DifferentialPairType.CUSTOM: PairProtocol.CUSTOM,
}


@pytest.mark.parametrize("pair_type", list(DifferentialPairType))
def test_for_type_returns_table_values_and_warns(pair_type):
    row = PROTOCOL_DEFAULTS[_TYPE_ROW[pair_type]]
    with pytest.warns(DeprecationWarning, match="for_type is deprecated"):
        rules = DifferentialPairRules.for_type(pair_type)
    assert rules.spacing == row.gap_mm.value
    assert rules.trace_width == row.trace_width_mm.value
    assert rules.impedance == row.diff_impedance_ohm.value
    expected = row.max_skew_mm.value if row.max_skew_mm is not None else GENERIC_SKEW_MM
    assert rules.max_length_delta == expected


def test_internal_callers_do_not_trip_the_deprecation():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert (
            DifferentialPairRules.for_pair_type(DifferentialPairType.USB2).max_length_delta == 2.5
        )
        assert DifferentialPairRules.from_protocol("USB2_HS").max_length_delta == 1.27
        # A row with no gap/width of its own borrows the CUSTOM row's.
        assert DifferentialPairRules.from_protocol("PCIE_G1").spacing == 0.2


# ---------------------------------------------------------------------------
# Resolution order, provenance, severity
# ---------------------------------------------------------------------------


def _resolve(**kwargs):
    nc = NetClassRouting(name="Cls", coupled_routing=True, **kwargs)
    return resolve_pair_thresholds({"X_P": nc, "X_N": nc}, "X_P", "X_N")


def test_class_field_beats_protocol_beats_generic():
    explicit = _resolve(pair_protocol="USB2_FS", skew_tolerance_mm=0.8)
    assert (explicit.skew_mm, explicit.skew_source, explicit.skew_severity) == (
        0.8,
        "class:Cls",
        "error",
    )
    table = _resolve(pair_protocol="USB2_FS")
    assert table.skew_mm == 2.5
    assert table.skew_source.startswith("protocol:USB2_FS (Renesas")
    assert table.skew_severity == "error"
    generic = _resolve()
    assert (generic.skew_mm, generic.skew_source, generic.skew_severity) == (
        0.5,
        "default:generic",
        "error",
    )
    assert generic.protocol is None
    assert generic.max_uncoupled_per_terminal_mm is None
    assert generic.max_vias_per_net is None


def test_uncited_protocol_limit_is_a_warning():
    got = _resolve(pair_protocol="USB3_SS")
    assert (got.skew_mm, got.skew_source, got.skew_severity) == (
        0.1,
        "protocol:USB3_SS (uncited)",
        "warning",
    )
    # ... until the class states the number itself.
    assert _resolve(pair_protocol="USB3_SS", skew_tolerance_mm=0.1).skew_severity == "error"


def test_protocol_without_a_skew_value_gets_the_generic_default():
    for name in ("PCIE_G2_PLUS", "CUSTOM"):
        got = _resolve(pair_protocol=name)
        assert (got.skew_mm, got.skew_source, got.protocol) == (0.5, "default:generic", name)


def test_uncoupled_and_via_limits_resolve_with_provenance():
    table = _resolve(pair_protocol="USB2_HS")
    assert (table.max_uncoupled_per_terminal_mm, table.max_vias_per_net) == (6.35, 4)
    assert table.provenance["max_uncoupled_per_terminal_mm"].startswith("protocol:USB2_HS (TI")
    assert table.max_uncoupled_total_mm is None  # derived by piece (b)
    explicit = _resolve(
        pair_protocol="USB2_HS",
        max_uncoupled_per_terminal_mm=4.0,
        max_uncoupled_total_mm=12.0,
        max_vias_per_net=2,
    )
    assert (explicit.max_uncoupled_per_terminal_mm, explicit.max_vias_per_net) == (4.0, 2)
    assert explicit.max_uncoupled_total_mm == 12.0
    assert explicit.provenance["max_vias_per_net"] == "class:Cls"
    assert explicit.provenance["max_uncoupled_total_mm"] == "class:Cls"


def test_router_side_accessor_agrees_with_the_resolver():
    """``effective_skew_tolerance`` is what the tuner and escape planner read."""
    for kwargs in (
        {},
        {"pair_protocol": "USB2_FS"},
        {"pair_protocol": "USB2_HS"},
        {"pair_protocol": "MIPI_DPHY"},
        {"pair_protocol": "PCIE_G2_PLUS"},
        {"pair_protocol": "USB2_FS", "skew_tolerance_mm": 0.3},
    ):
        nc = NetClassRouting(name="Cls", **kwargs)
        assert nc.effective_skew_tolerance() == _resolve(**kwargs).skew_mm, kwargs


def test_sidecar_round_trip_and_unknown_protocol():
    nc = NetClassRouting(name="Cls", pair_protocol="USB2_FS", max_vias_per_net=2)
    data = nc.to_dict()
    assert data["pair_protocol"] == "USB2_FS"
    assert NetClassRouting.from_dict(data) == nc
    assert NetClassRouting.from_dict({"name": "Cls", "pair_protocol": "usb2_fs"}) == (
        NetClassRouting(name="Cls", pair_protocol="USB2_FS")
    )
    # An old sidecar has none of the new keys.
    assert NetClassRouting.from_dict({"name": "Cls"}).pair_protocol is None
    with pytest.raises(ValueError, match="unknown pair protocol"):
        NetClassRouting.from_dict({"name": "Cls", "pair_protocol": "USB9"})


def test_unknown_protocol_fails_where_the_class_is_declared():
    """A typo must not survive until something resolves the limits mid-route."""
    with pytest.raises(ValueError, match="unknown pair protocol 'USB9'"):
        NetClassRouting(name="Cls", pair_protocol="USB9")
    with pytest.raises(ValueError, match="unknown pair protocol"):
        dataclasses.replace(NetClassRouting(name="Cls"), pair_protocol="USB2_XS")
    # Known spellings are accepted and stored as given.
    assert NetClassRouting(name="Cls", pair_protocol="usb2_fs").pair_protocol == "usb2_fs"
    assert NetClassRouting(name="Cls", pair_protocol="usb2_fs").effective_skew_tolerance() == 2.5
    assert NetClassRouting(name="Cls").pair_protocol is None


def test_board06_committed_readiness_is_current():
    """Board 06's report hash-binds its recipe and sidecar (Issue #6443).

    Editing ``assembled-demo/generate.py`` or ``output/net_class_map.json``
    without re-pinning (``scripts/boards/refresh_readiness.py``) turns the
    committed report from ``ready`` into ``unverified``.
    """
    from kicad_tools.cli.board_readiness import read_readiness

    report = read_readiness(BOARDS / "06-diffpair-test")
    assert report["blockers"] == []
    assert report["status"] == "ready"


# ---------------------------------------------------------------------------
# Severity in the skew rule
# ---------------------------------------------------------------------------


def _skew_finding(skew_mm: float, **class_kwargs):
    nc = NetClassRouting(name="Cls", coupled_routing=True, **class_kwargs)
    limits = resolve_pair_thresholds({"X_P": nc, "X_N": nc}, "X_P", "X_N")
    pcb = SimpleNamespace(
        nets={1: SimpleNamespace(name="X_P", number=1), 2: SimpleNamespace(name="X_N", number=2)}
    )
    rule = DiffPairLengthSkewRule(
        skew_data={("X_P", "X_N"): skew_mm},
        engaged_pairs={(1, 2)},
        threshold_map={(1, 2): limits.skew_mm},
        severity_map={(2, 1): limits.skew_severity},  # order is normalised
        source_map={(1, 2): limits.skew_source},
    )
    found = rule.check(pcb, None).violations
    return found[0] if found else None


def test_skew_over_a_cited_limit_is_an_error():
    finding = _skew_finding(3.0, pair_protocol="USB2_FS")
    assert finding.severity == "error"
    assert "exceeds tolerance 2.500 mm [protocol:USB2_FS (Renesas" in finding.message


def test_skew_over_an_uncited_limit_is_a_warning():
    finding = _skew_finding(0.3, pair_protocol="USB3_SS")
    assert finding.severity == "warning"
    assert finding.message.endswith("[protocol:USB3_SS (uncited)]")


def test_skew_over_an_explicit_or_generic_limit_is_an_error():
    assert _skew_finding(0.3, pair_protocol="USB3_SS", skew_tolerance_mm=0.1).severity == "error"
    generic = _skew_finding(0.6)
    assert generic.severity == "error"
    assert generic.message.endswith("[default:generic]")


def test_skew_within_the_limit_has_no_finding():
    assert _skew_finding(2.4, pair_protocol="USB2_FS") is None
    assert _skew_finding(1.2, pair_protocol="USB2_HS") is None
    assert _skew_finding(1.3, pair_protocol="USB2_HS").severity == "error"


def test_rule_without_maps_keeps_its_old_message_and_severity():
    pcb = SimpleNamespace(
        nets={1: SimpleNamespace(name="A", number=1), 2: SimpleNamespace(name="B", number=2)}
    )
    rule = DiffPairLengthSkewRule(skew_data={("A", "B"): 0.9}, engaged_pairs={(1, 2)})
    (finding,) = rule.check(pcb, None).violations
    assert finding.severity == "error"
    assert finding.message.endswith("exceeds tolerance 0.500 mm")


# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------


def _load_map(path: Path):
    return net_class_map_from_path(path)


def test_board03_pairs_are_full_speed_usb_at_2p5_mm():
    ncm = _load_map(B03 / "output" / "net_class_map.json")
    for p, n in (("USB_D+", "USB_D-"), ("USB_MCU_D+", "USB_MCU_D-")):
        got = resolve_pair_thresholds(ncm, p, n)
        assert (got.skew_mm, got.protocol, got.skew_severity) == (2.5, "USB2_FS", "error")
        assert got.skew_stretch_mm == 0.5
    # The continuity override is piece (b)'s to remove.
    assert ncm["USB_D+"].coupled_continuity_threshold == 0.25


def test_board03_local_check_reads_the_table_and_the_committed_copper_passes():
    spec = importlib.util.spec_from_file_location("b03_routing_plan_6443", B03 / "routing_plan.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        assert mod.USB_FS_MAX_SKEW_MM == 2.5
        assert mod.USB_SKEW_STRETCH_TARGET_MM == 0.5
        geometry = mod.usb_geometry(B03 / "output" / "usb_joystick_routed.kicad_pcb")
    finally:
        sys.modules.pop(spec.name, None)
    # The reviewed copper clears the gate and the stretch target.
    assert geometry["branch_skew_mm"] == pytest.approx(0.166, abs=0.001)
    assert geometry["branch_skew_mm"] < mod.USB_SKEW_STRETCH_TARGET_MM < mod.USB_FS_MAX_SKEW_MM


def test_board06_usb2_takes_the_high_speed_default():
    ncm = _load_map(BOARDS / "06-diffpair-test" / "regression-fixture" / "net_class_map.json")
    assert ncm["USB2_D+"].skew_tolerance_mm is None  # the uncited 3.0 mm is gone
    usb2 = resolve_pair_thresholds(ncm, "USB2_D+", "USB2_D-")
    assert (usb2.skew_mm, usb2.protocol, usb2.skew_severity) == (1.27, "USB2_HS", "error")
    assert "TI SPRAAR7J" in usb2.skew_source
    # Every other class keeps its explicit number.
    for p, n, mm, protocol in (
        ("USB3_TX1+", "USB3_TX1-", 0.5, "USB3_SS"),
        ("PCIE_TX+", "PCIE_TX-", 0.5, "PCIE_G1"),
        ("MIPI_CLK+", "MIPI_CLK-", 0.3, "MIPI_DPHY"),
    ):
        got = resolve_pair_thresholds(ncm, p, n)
        assert (got.skew_mm, got.protocol, got.skew_severity) == (mm, protocol, "error")
        assert got.skew_source.startswith("class:")
    lvds = _load_map(BOARDS / "06-diffpair-test" / "output" / "net_class_map.json")
    got = resolve_pair_thresholds(lvds, "LVDS1_P", "LVDS1_N")
    assert (got.skew_mm, got.protocol, got.skew_source) == (0.1, "LVDS", "class:LVDS")


def _committed_sidecars() -> list[Path]:
    return sorted(BOARDS.glob("**/net_class_map.json"))


def test_no_other_board_declares_a_protocol_or_changes_a_threshold():
    """Only boards 03 and 06 opt in; an untagged class still resolves as before."""
    paths = [p for p in _committed_sidecars() if "/manufacturing/" not in p.as_posix()]
    assert len(paths) >= 6
    for path in paths:
        board = path.relative_to(BOARDS).parts[0]
        raw = json.loads(path.read_text())
        ncm = _load_map(path)
        for net, entry in raw.items():
            if not isinstance(entry, dict) or net not in ncm:
                continue
            if board in ("03-usb-joystick", "06-diffpair-test"):
                continue
            assert entry.get("pair_protocol") is None, (path, net)
            before = entry.get("skew_tolerance_mm")
            before = 0.5 if before is None else before
            got = resolve_pair_thresholds(ncm, net, net)
            assert got.skew_mm == before, (path, net)
            assert got.skew_severity == "error", (path, net)
