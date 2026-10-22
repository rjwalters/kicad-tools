"""Board 03 USB_D skew is inside the 0.5 mm stretch by both checkers (issue #6730).

#6729 made our net-length model match kicad-cli (via span, pad trim), and the
committed board 03 copper then read 1.246 mm, later 1.32 mm, apart.  The
arm C re-derivation (#7713) gave USB_D+/USB_D- equal track lengths and equal
via counts (3 / 3), which leaves 0.224 mm of via-barrel skew.  These tests hold
the committed artifact to the 0.5 mm stretch target with our measurement and
with kicad-cli on the same file, and keep ``usb_geometry()``'s bounds passing.
The 2.5 mm USB2_FS gate is unchanged.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.router.diffpair_length import DiffPairLengthTracker
from kicad_tools.schema.pcb import PCB

BOARD = Path(__file__).resolve().parents[1] / "boards" / "03-usb-joystick"
OUTPUT = BOARD / "output"
ROUTED = OUTPUT / "usb_joystick_routed.kicad_pcb"
USB_NETS = ("USB_D+", "USB_D-", "USB_MCU_D+", "USB_MCU_D-")


def _routing_plan():
    spec = importlib.util.spec_from_file_location(
        "b03_routing_plan_6730", BOARD / "routing_plan.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def _our_lengths() -> dict[str, float]:
    pcb = PCB.load(ROUTED)
    by_name = {n.name: n.number for n in pcb.nets.values()}
    return {n: DiffPairLengthTracker.measure_net_from_pcb(pcb, by_name[n]) for n in USB_NETS}


def test_usb_d_skew_is_inside_the_stretch_target():
    plan = _routing_plan()
    lengths = _our_lengths()
    skew = abs(lengths["USB_D+"] - lengths["USB_D-"])
    assert skew == pytest.approx(0.224, abs=0.001)
    assert skew <= plan.USB_SKEW_STRETCH_TARGET_MM == 0.5
    assert plan.USB_FS_MAX_SKEW_MM == 2.5
    # The bounds the stretch must be met inside (it raises on any failure).
    plan.usb_geometry(ROUTED)


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")
def test_usb_lengths_and_skew_match_kicad_cli(tmp_path):
    """kicad-cli reports every net's length through a custom DRC ``length`` rule."""
    board = tmp_path / "board.kicad_pcb"
    shutil.copy(ROUTED, board)
    shutil.copy(ROUTED.with_suffix(".kicad_pro"), board.with_suffix(".kicad_pro"))
    # The board's own library table, so kicad-cli sees the committed footprints.
    shutil.copy(OUTPUT / "fp-lib-table", tmp_path / "fp-lib-table")
    shutil.copytree(OUTPUT / "footprints", tmp_path / "footprints")
    board.with_suffix(".kicad_dru").write_text(
        "(version 1)\n"
        "(rule len (constraint length (max 0.001mm)) (condition \"A.NetName != ''\"))\n"
    )
    out = tmp_path / "drc.json"
    subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            "-o",
            str(out),
            str(board),
        ],
        capture_output=True,
        timeout=300,
        check=False,
    )
    if not out.exists():
        pytest.skip("kicad-cli produced no DRC report")
    actual: dict[str, float] = {}
    for violation in json.loads(out.read_text()).get("violations", []):
        m = re.search(r"actual ([0-9.]+) mm", violation.get("description", ""))
        names = {
            g.group(1)
            for item in violation.get("items", [])
            if (g := re.search(r"\[(.*?)\] of", item.get("description", "")))
        }
        if m and len(names) == 1 and (name := names.pop()) in USB_NETS:
            actual[name] = float(m.group(1))
    if not actual:
        pytest.skip("kicad-cli reported no length for the USB nets")
    assert set(actual) == set(USB_NETS)
    ours = _our_lengths()
    for name, kicad_len in actual.items():
        assert ours[name] == pytest.approx(kicad_len, abs=1e-3), name
    assert abs(actual["USB_D+"] - actual["USB_D-"]) <= 0.5
