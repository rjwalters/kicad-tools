"""One copper-contact model, held to kicad-cli (Issues #6452, #6453, #6470).

``NetStatusAnalyzer(strict=True)``, ``ConnectivityValidator.validate()`` and
the label-free LVS partition (``extract_pad_occurrences``) all decide
connectivity on the shared graph of :mod:`kicad_tools.validate.copper_contact`.
Their open counts must equal ``kicad-cli pcb drc``'s ``unconnected_items``,
per net, on every committed routed board.

The goldens below were measured with kicad-cli 10.0.6 (``pcb drc``, no
``--refill-zones``).  ``test_goldens_match_live_kicad_cli`` re-measures them
when kicad-cli is installed, and the synthetic fixtures re-check their own
kicad-cli verdicts the same way, like
``tests/test_net_status_pad_rotation_6460.py``.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.validate.connectivity import ConnectivityValidator

REPO = Path(__file__).resolve().parent.parent

# kicad-cli 10.0.6 ``unconnected_items`` per net.
GOLDENS: dict[str, dict[str, int]] = {
    "boards/00-simple-led/output/simple_led_routed.kicad_pcb": {},
    "boards/01-voltage-divider/output/voltage_divider_routed.kicad_pcb": {},
    "boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb": {},
    "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb": {},
    "boards/03-usb-joystick/regression-fixture/usb_joystick_routed.kicad_pcb": {
        "VBUS": 1,
        "VCC": 1,
        "GND": 11,
    },
    "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb": {},
    "boards/05-bldc-motor-controller/legacy_drv8301/output/bldc_controller_routed.kicad_pcb": {
        "+24V": 5,
        "+5V": 4,
        "+3V3": 14,
        "GND": 33,
        "VIN": 1,
    },
    "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb": {},
    "boards/05-bldc-motor-controller/regression-fixture/bldc_controller_routed.kicad_pcb": {
        "+24V": 5,
        "+5V": 4,
        "+3V3": 14,
        "GND": 33,
        "VIN": 1,
    },
    "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb": {},
    "boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb": {
        "+3V3": 1,
        "+1V8": 1,
    },
    "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb": {},
    "boards/07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb": {
        "DQ3": 1,
        "DQ4": 1,
        "TMDS_D0_N": 1,
        "TMDS_D1_N": 1,
    },
    "boards/09-usbc-pd-power/engineering/connectivity-investigation/before-split.kicad_pcb": {},
    "boards/09-usbc-pd-power/engineering/routing-investigation/fine_partial.kicad_pcb": {
        "+5V_OUT": 3,
        "CC1": 1,
        "CC2": 1,
        "DISCHARGE": 1,
        "GND": 31,
        "MON_ALERT": 1,
        "PD_OK2": 1,
        "PMOS_GATE": 4,
        "PMOS_SOURCE": 3,
        "SDA": 3,
        "SENSE_N": 2,
        "SW": 2,
        "VBUS_RAW": 7,
        "VIN": 5,
        "VOUT_PRE": 5,
        "VREG_1V2": 1,
    },
    "boards/09-usbc-pd-power/engineering/routing-investigation/first.kicad_pcb": {
        "+5V_OUT": 4,
        "CC2": 1,
        "DISCHARGE": 1,
        "GND": 32,
        "PD_OK2": 1,
        "PMOS_GATE": 5,
        "SDA": 3,
        "SENSE_N": 1,
        "SENSE_P": 1,
        "SINK_GATE": 1,
        "SW": 2,
        "VBUS_RAW": 7,
        "VIN": 4,
        "VOUT_PRE": 5,
    },
    "boards/09-usbc-pd-power/engineering/routing-investigation/host-bus/name-only-source.kicad_pcb": {
        "SCL": 3,
        "PD_ALERT": 2,
        "SDA": 3,
        "MON_ALERT": 2,
    },
    # GND 28 counts 6 orphan track/via fragments that reach no pad: kicad-cli
    # reports each as an unconnected item, and so does the shared model.
    "boards/09-usbc-pd-power/engineering/routing-investigation/host-bus/numbered-source.kicad_pcb": {
        "GND": 28,
        "MON_ALERT": 2,
        "PD_ALERT": 2,
        "SCL": 3,
        "SDA": 3,
    },
    "boards/09-usbc-pd-power/engineering/routing-investigation/support-routing/support.kicad_pcb": {
        "GND": 30,
        "PD_OK2": 1,
        "PD_OK3": 1,
        "SCL": 3,
        "SDA": 2,
        "VBUS_RAW": 7,
    },
    "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb": {},
}

# Pad groups the label-free partition fuses across declared nets.  Each is a
# short kicad-cli also reports (``shorting_items``): the exact pad outlines
# expose SENSE_P's track on U3.9 (SENSE_N), which the old eroded pad box hid.
EXPECTED_SHORTS: dict[str, list[list[str]]] = {
    "boards/09-usbc-pd-power/engineering/routing-investigation/fine_partial.kicad_pcb": [
        ["GND", "SW", "VIN"],  # C5.2 overlaps U2.1 / U2.2 / U2.3
        ["SENSE_N", "SENSE_P"],
    ],
    "boards/09-usbc-pd-power/engineering/routing-investigation/first.kicad_pcb": [
        ["+3V3", "GND"],
        ["+5V_OUT", "KELVIN_N"],
        ["SENSE_N", "SENSE_P"],
    ],
}


def _validate_opens(result) -> dict[str, int]:
    opens: Counter[str] = Counter()
    for issue in result.errors:
        if issue.issue_type == "zone_island":
            opens[issue.net_name] += 1
        else:
            opens[issue.net_name] += max(len(issue.islands) - 1, 1)
    return dict(opens)


def _analyzer_opens(path: Path) -> dict[str, int]:
    result = NetStatusAnalyzer(path, strict=True).analyze()
    return {s.net_name: s.open_connections for s in result.nets if s.open_connections}


def _partition_shorts(path: Path) -> list[list[str]]:
    validator = ConnectivityValidator(path)
    groups, _ = validator.extract_pad_occurrences()
    net_of = {}
    for (fi, pi), node in validator._pad_ids.items():
        pad = validator.pcb.footprints[fi].pads[pi]
        net_of[node] = validator.pcb.nets[pad.net_number].name if pad.net_number else ""
    shorts = []
    for group in groups:
        nets = sorted({net_of[node] for node in group} - {""})
        if len(nets) > 1:
            shorts.append(nets)
    return sorted(shorts)


@pytest.mark.parametrize("board", sorted(GOLDENS))
def test_analyzer_open_count_matches_kicad_cli(board: str):
    assert _analyzer_opens(REPO / board) == GOLDENS[board]


@pytest.mark.parametrize("board", sorted(GOLDENS))
def test_validate_open_count_matches_kicad_cli(board: str):
    result = ConnectivityValidator(REPO / board).validate()
    assert _validate_opens(result) == GOLDENS[board]


@pytest.mark.parametrize("board", sorted(GOLDENS))
def test_partition_shorts_are_the_kicad_reported_ones(board: str):
    assert _partition_shorts(REPO / board) == sorted(EXPECTED_SHORTS.get(board, []))


# ---------------------------------------------------------------------------
# Live kicad-cli
# ---------------------------------------------------------------------------


def _kicad_cli() -> Path | None:
    from kicad_tools.cli.runner import find_kicad_cli

    return find_kicad_cli()


def _kicad_unconnected(path: Path, out_dir: Path) -> dict[str, int]:
    out = out_dir / f"{path.stem}.drc.json"
    subprocess.run(
        [str(_kicad_cli()), "pcb", "drc", "--format", "json", "-o", str(out), str(path)],
        capture_output=True,
        check=False,
    )
    per: Counter[str] = Counter()
    for item in json.loads(out.read_text()).get("unconnected_items", []):
        nets = {
            found[-1]
            for entry in item["items"]
            if (found := re.findall(r"\[([^\]]+)\]", entry.get("description", "")))
        }
        per[",".join(sorted(nets))] += 1
    return dict(per)


@pytest.mark.skipif(_kicad_cli() is None, reason="kicad-cli not installed")
@pytest.mark.parametrize("board", sorted(GOLDENS))
def test_goldens_match_live_kicad_cli(board: str, tmp_path: Path):
    assert _kicad_unconnected(REPO / board, tmp_path) == GOLDENS[board]


# ---------------------------------------------------------------------------
# Synthetic fixtures: each checked in all three models (and live kicad-cli)
# ---------------------------------------------------------------------------

_HEADER = """(kicad_pcb (version 20240108) (generator "t") (generator_version "8.0")
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)
    (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "GND") (net 2 "VCC")
"""


def _fp(ref: str, x: float, y: float, pad: str) -> str:
    return (
        f'  (footprint "T:{ref}" (layer "F.Cu") (uuid "fp-{ref}") (at {x} {y})\n'
        f'    (property "Reference" "{ref}" (at 0 -3 0) (layer "F.SilkS") (uuid "r-{ref}"))\n'
        f"{pad})\n"
    )


def _smd(num: str, shape: str, size: tuple[float, float], net: int, *, at=(0, 0), extra="") -> str:
    name = {1: "GND", 2: "VCC"}[net]
    return (
        f'    (pad "{num}" smd {shape} (at {at[0]} {at[1]}) (size {size[0]} {size[1]}) '
        f'(layers "F.Cu") {extra}(net {net} "{name}"))\n'
    )


def _tht(num: str, size: float, drill: float, net: int, *, at=(0, 0)) -> str:
    name = {1: "GND", 2: "VCC"}[net]
    return (
        f'    (pad "{num}" thru_hole circle (at {at[0]} {at[1]}) (size {size} {size}) '
        f'(drill {drill}) (layers "*.Cu") (net {net} "{name}"))\n'
    )


def _zone(net: int, layer: str, x0: float, y0: float, x1: float, y1: float, uid: str) -> str:
    name = {1: "GND", 2: "VCC"}[net]
    pts = f"(xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1})"
    return (
        f'  (zone (net {net}) (net_name "{name}") (layer "{layer}") (uuid "{uid}")\n'
        "    (connect_pads yes (clearance 0.2)) (min_thickness 0.2)\n"
        "    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))\n"
        f"    (polygon (pts {pts}))\n"
        f'    (filled_polygon (layer "{layer}") (pts {pts})))\n'
    )


def _via(x: float, y: float, net: int = 1) -> str:
    return f'  (via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {net}) (uuid "v{x}_{y}"))\n'


def _seg(a, b, net: int = 1, layer: str = "F.Cu", width: float = 0.2) -> str:
    return (
        f"  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) "
        f'(layer "{layer}") (net {net}) (uuid "s{a[0]}_{a[1]}_{b[0]}_{b[1]}"))\n'
    )


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(_HEADER + body + ")\n")
    return path


def _three_models(path: Path, net: str) -> tuple[int, int, int]:
    """Open count on ``net`` from analyzer, validate() and the partition."""
    analyzer = _analyzer_opens(path).get(net, 0)
    validate = _validate_opens(ConnectivityValidator(path).validate()).get(net, 0)
    validator = ConnectivityValidator(path)
    groups, _ = validator.extract_pad_occurrences()
    on_net = set()
    for (fi, pi), node in validator._pad_ids.items():
        pad = validator.pcb.footprints[fi].pads[pi]
        if validator.pcb.nets[pad.net_number].name == net:
            on_net.add(node)
    partition = sum(1 for group in groups if group & on_net) - 1
    return analyzer, validate, partition


def _check_kicad(path: Path, tmp_path: Path, net: str, expected: int) -> None:
    if _kicad_cli() is None:
        return
    assert _kicad_unconnected(path, tmp_path).get(net, 0) == expected


def _cross_zone_board(tmp_path: Path, *, with_via: bool) -> Path:
    """F.Cu fill island with an SMD pad; In1.Cu plane (another zone) with a PTH pad.

    The only bridge is a through via touching both fills (Issue #6452).
    """
    body = (
        _fp("U1", 3, 5, _smd("1", "rect", (1, 1), 1))
        + _fp("U2", 25, 5, _tht("1", 1.6, 0.8, 1))
        + _zone(1, "F.Cu", 0, 0, 10, 10, "zf")
        + _zone(1, "In1.Cu", 0, 0, 30, 10, "zi")
        + (_via(8, 5) if with_via else "")
    )
    return _write(tmp_path, f"cross_zone_{with_via}", body)


@pytest.mark.parametrize("with_via", [True, False])
def test_via_bridges_fills_of_two_zones(tmp_path: Path, with_via: bool):
    path = _cross_zone_board(tmp_path, with_via=with_via)
    expected = 0 if with_via else 1
    assert _three_models(path, "GND") == (expected,) * 3
    _check_kicad(path, tmp_path, "GND", expected)


@pytest.mark.parametrize("with_chain", [True, False])
def test_padless_track_chain_bridges_two_zones(tmp_path: Path, with_chain: bool):
    """A two-segment chain with no pad on it joins two zones' fills."""
    chain = _seg((8, 5), (15, 5)) + _seg((15, 5), (22, 5)) if with_chain else ""
    body = (
        _fp("U1", 3, 5, _smd("1", "rect", (1, 1), 1))
        + _fp("U2", 27, 5, _smd("1", "rect", (1, 1), 1))
        + _zone(1, "F.Cu", 0, 0, 10, 10, "za")
        + _zone(1, "F.Cu", 20, 0, 30, 10, "zb")
        + chain
    )
    path = _write(tmp_path, f"chain_{with_chain}", body)
    expected = 0 if with_chain else 1
    assert _three_models(path, "GND") == (expected,) * 3
    _check_kicad(path, tmp_path, "GND", expected)


# U1.1: roundrect 1.475 x 0.3 at (10, 10), corner radius 0.075; its real copper
# ends at x = 10.7375.  The old pad box eroded by 0.1 mm ended at 10.6375.
_RR = _smd("1", "roundrect", (1.475, 0.3), 1, extra="(roundrect_rratio 0.25) ")


@pytest.mark.parametrize(
    ("fill_x0", "expected"),
    [
        (10.6875, 0),  # fill overlaps the real copper by 0.05 mm (< 0.1 erosion)
        (10.7875, 1),  # fill stops 0.05 mm short of the real copper
    ],
)
def test_shallow_roundrect_fill_contact(tmp_path: Path, fill_x0: float, expected: int):
    """Contact depth below the old erosion is still contact (Issue #6453)."""
    body = (
        _fp("U1", 10, 10, _RR)
        + _fp("U2", 15, 10, _smd("1", "rect", (1, 1), 1))
        + _zone(1, "F.Cu", fill_x0, 9, 20, 11, "z")
    )
    path = _write(tmp_path, f"rr_{fill_x0}", body)
    assert _three_models(path, "GND") == (expected,) * 3
    _check_kicad(path, tmp_path, "GND", expected)


def test_roundrect_box_corner_does_not_touch_foreign_fill(tmp_path: Path):
    """A foreign fill over the pad's *box* corner but clear of its rounded
    corner is neither a bond nor a short (Issue #6453)."""
    body = (
        _fp("U1", 10, 10, _RR)
        + _fp("U2", 15, 10, _smd("1", "rect", (1, 1), 1))
        + _fp("U3", 11.5, 11.5, _smd("1", "rect", (0.5, 0.5), 2))
        + _seg((10, 10), (15, 10))
        + _zone(2, "F.Cu", 10.72, 10.13, 12, 12, "zv")
    )
    path = _write(tmp_path, "rr_corner", body)
    assert _partition_shorts(path) == []
    assert _three_models(path, "GND") == (0, 0, 0)
    assert _three_models(path, "VCC") == (0, 0, 0)


def test_overlapping_pads_bond_and_disjoint_same_number_pads_do_not(tmp_path: Path):
    """Exposed pad + ``*.Cu`` thermal-via pads are one conductor; a disjoint
    pad with the same number is not (Issues #6435, #5151); two footprints'
    overlapping pads bond (``C5.2`` / ``U2.1`` on board 09)."""
    ep = _smd("9", "rect", (3, 3), 1)
    vias = "".join(_tht("9", 0.6, 0.3, 1, at=(dx, dy)) for dx in (-0.8, 0.8) for dy in (-0.8, 0.8))
    lone = _smd("9", "rect", (1, 1), 1, at=(5, 0))
    body = (
        _fp("U1", 10, 10, ep + vias + lone)
        + _fp("U2", 30, 10, _smd("1", "rect", (1, 1), 1))
        + _fp("C5", 30.8, 10, _smd("2", "rect", (1, 1), 1))
    )
    path = _write(tmp_path, "overlap", body)
    validator = ConnectivityValidator(path)
    groups, bindings = validator.extract_pad_occurrences()
    u1_9 = sorted(len(group) for group in groups if any(bindings[n] == ("U1", "9") for n in group))
    assert u1_9 == [1, 5]
    assert frozenset({"U2.1", "C5.2"}) in validator.extract_pad_partition()


@pytest.mark.parametrize(
    ("start_x", "expected"),
    [
        (9.7, 0),  # trace end 0.3 mm from the pad centre, on pad copper
        (10.65, 1),  # trace copper ends 0.05 mm outside the pad (edge 10.5)
    ],
)
def test_trace_endpoint_on_pad_copper_not_centre(tmp_path: Path, start_x: float, expected: int):
    """``validate()`` matches trace ends to pad copper, not centres (Issue #6470)."""
    body = (
        _fp("U1", 10, 10, _smd("1", "rect", (1, 1), 1))
        + _fp("U2", 20, 10, _smd("1", "rect", (1, 1), 1))
        + _seg((start_x, 10), (20, 10))
    )
    path = _write(tmp_path, f"endpoint_{start_x}", body)
    assert _three_models(path, "GND") == (expected,) * 3
    _check_kicad(path, tmp_path, "GND", expected)


def test_orphan_track_counts_as_open(tmp_path: Path):
    """A track touching nothing is an unconnected item to kicad-cli (Issue #6452)."""
    body = (
        _fp("U1", 10, 10, _smd("1", "rect", (1, 1), 1))
        + _fp("U2", 20, 10, _smd("1", "rect", (1, 1), 1))
        + _seg((10, 10), (20, 10))
        + _seg((10, 14), (20, 14))
    )
    path = _write(tmp_path, "orphan", body)
    assert _analyzer_opens(path) == {"GND": 1}
    result = ConnectivityValidator(path).validate()
    assert _validate_opens(result) == {"GND": 1}
    assert result.unconnected_pad_count == 0
    status = NetStatusAnalyzer(path).analyze().get_net("GND")
    assert status is not None
    assert (status.island_count, status.padless_components) == (1, 1)
    _check_kicad(path, tmp_path, "GND", 1)


# ---------------------------------------------------------------------------
# Pad-less components: what kicad-cli counts and what it does not
# ---------------------------------------------------------------------------
#
# kicad-cli 10.0.6 reports a detached track or via as an unconnected item, and
# a fill island once a track or via sits on it.  It does NOT report a lone
# fill island -- a component made only of fill copper -- whatever the zone's
# ``island_removal_mode`` and whether the island is a second ``filled_polygon``
# of the same zone or a zone of its own.

_SQ = tuple[float, float, float, float]
_MAIN: _SQ = (98, 98, 105, 102)  # covers both pads of ``_two_pads``
_LONE: _SQ = (110, 98, 115, 102)
_LONE2: _SQ = (120, 98, 125, 102)


def _fills_zone(fills: list[_SQ], uid: str, *, mode: int = 1) -> str:
    """One F.Cu GND zone holding one ``filled_polygon`` per square in ``fills``."""

    def pts(sq: _SQ) -> str:
        x0, y0, x1, y1 = sq
        return f"(xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1})"

    filled = "".join(f'    (filled_polygon (layer "F.Cu") (pts {pts(sq)}))\n' for sq in fills)
    return (
        f'  (zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "{uid}")\n'
        "    (connect_pads yes (clearance 0))\n"
        "    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2)"
        f" (island_removal_mode {mode}))\n"
        f"    (polygon (pts {pts(fills[0])}))\n"
        f"{filled}  )\n"
    )


def _two_pads(x2: float = 103) -> str:
    return _fp("R1", 100, 100, _smd("1", "rect", (1, 1), 1)) + _fp(
        "R2", x2, 100, _smd("1", "rect", (1, 1), 1)
    )


_PAD_TRACK = _seg((100, 100), (103, 100))

# name -> (board body, opens kicad-cli reports, lone fill islands, padless_components)
_PADLESS_CASES: dict[str, tuple[str, int, int, int]] = {
    # Row 1: a lone fill island is not an unconnected item.
    "lone_fill_same_zone_mode0": (_two_pads() + _fills_zone([_MAIN, _LONE], "z", mode=0), 0, 1, 0),
    "lone_fill_same_zone_mode1": (_two_pads() + _fills_zone([_MAIN, _LONE], "z", mode=1), 0, 1, 0),
    "lone_fill_same_zone_mode2": (_two_pads() + _fills_zone([_MAIN, _LONE], "z", mode=2), 0, 1, 0),
    "lone_fill_other_zone": (
        _two_pads() + _fills_zone([_MAIN], "z1") + _fills_zone([_LONE], "z2"),
        0,
        1,
        0,
    ),
    "two_lone_fills_pads_joined_by_track": (
        _two_pads() + _PAD_TRACK + _fills_zone([_LONE, _LONE2], "z"),
        0,
        2,
        0,
    ),
    "lone_fill_single_pad": (
        _fp("R1", 100, 100, _smd("1", "rect", (1, 1), 1)) + _fills_zone([_LONE], "z"),
        0,
        1,
        0,
    ),
    # Row 2: a fill island with only a via or a track on it is one.
    "fill_island_with_via_only": (
        _two_pads() + _fills_zone([_MAIN, _LONE], "z") + _via(112, 100),
        1,
        0,
        1,
    ),
    "fill_island_with_track_only": (
        _two_pads() + _fills_zone([_MAIN, _LONE], "z") + _seg((112, 100), (113, 100)),
        1,
        0,
        1,
    ),
    # Row 3: a detached track or via, with every pad connected, is one.
    "detached_track_all_pads_connected": (
        _two_pads() + _PAD_TRACK + _seg((100, 105), (103, 105)),
        1,
        0,
        1,
    ),
    "detached_via_all_pads_connected": (_two_pads() + _PAD_TRACK + _via(101, 105), 1, 0, 1),
    # Mixed: the lone fill is skipped, the rest is still counted.
    "lone_fill_and_detached_track": (
        _two_pads() + _PAD_TRACK + _seg((100, 105), (103, 105)) + _fills_zone([_LONE], "z"),
        1,
        1,
        1,
    ),
    "lone_fill_and_two_open_pads": (_two_pads() + _fills_zone([_LONE], "z"), 1, 1, 0),
    "pads_on_two_fill_islands": (_two_pads(112) + _fills_zone([_MAIN, _LONE], "z"), 1, 0, 0),
}


@pytest.mark.parametrize("name", sorted(_PADLESS_CASES))
def test_padless_components_match_kicad_cli(tmp_path: Path, name: str):
    """Lone fill islands are not opens; track/via fragments are (Issue #6452).

    Each fixture is re-measured with the installed kicad-cli when present.
    """
    body, expected, lone_fills, padless = _PADLESS_CASES[name]
    path = _write(tmp_path, name, body)
    want = {"GND": expected} if expected else {}

    assert _analyzer_opens(path) == want
    status = NetStatusAnalyzer(path, strict=True).analyze().get_net("GND")
    assert status is not None
    assert status.padless_components == padless

    result = ConnectivityValidator(path).validate()
    assert _validate_opens(result) == want
    assert result.is_fully_routed is (expected == 0)
    assert result.connected_nets == (1 if expected == 0 else 0)
    # A lone fill island stays visible, but only as a warning.
    assert [(w.issue_type, w.net_name) for w in result.warnings] == [
        ("zone_island", "GND")
    ] * lone_fills

    _check_kicad(path, tmp_path, "GND", expected)


def test_contact_graph_open_count_skips_fill_only_components(tmp_path: Path):
    """``open_count`` ignores components made solely of fill copper."""
    body = (
        _two_pads() + _PAD_TRACK + _seg((100, 105), (103, 105)) + _fills_zone([_LONE, _LONE2], "z")
    )
    validator = ConnectivityValidator(_write(tmp_path, "graph", body))
    graph = validator._contact_graph(1)
    assert len(graph.components()) == 4
    assert len(graph.fill_only_components()) == 2
    assert len(graph.counted_components()) == 2
    assert graph.open_count() == 1
