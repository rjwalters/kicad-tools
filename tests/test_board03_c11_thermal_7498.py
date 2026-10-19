"""Board 03 fresh-route keepout under C11.2's lower thermal spoke (Issue #7498).

A fresh lattice plan routed JOY_Y_RAW at y=93.5, directly under C11.2 (GND,
F.Cu).  After the 0.3 mm native zone carve that track left the pad's lower
spoke without pour, so the recipe's native DRC failed on one ``starved_thermal``
while the committed plan (JOY_Y_RAW at y=93.8) passed.  The harness now stages
a track/via keepout under the pad; these tests pin it without routing.
"""

import json
import runpy
import sys
from pathlib import Path

import pytest
from shapely.geometry import LineString, box

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_file

REPO = Path(__file__).resolve().parents[1]
BOARD = REPO / "boards/03-usb-joystick"
INPUT_PCB = BOARD / "output/usb_joystick.kicad_pcb"
TRACK_WIDTH = 0.2
FRESH_BAD_TRACK_Y = 93.5  # the starving JOY_Y_RAW run from the issue's measurement

sys.path.insert(0, str(REPO / "scripts/research"))
import diffpair_bench  # noqa: E402
import expand_route_experiment as ere  # noqa: E402


def _keepout():
    (entry,) = ere.BOARDS["03"].route_keepouts
    name, layers, points = entry
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return name, layers, (min(xs), max(xs), min(ys), max(ys))


def _c11_pad2():
    """(x, y, w, h) of C11.2 in file coordinates (C11 is unrotated)."""
    doc = parse_file(INPUT_PCB)
    for fp in doc.find_children("footprint"):
        ref = next(
            p.get_string(1) for p in fp.find_all("property") if p.get_string(0) == "Reference"
        )
        if ref != "C11":
            continue
        at = fp.find_child("at")
        assert at.get_float(2) in (0.0, None) if len(at.children) > 2 else True
        for pad in fp.find_all("pad"):
            if pad.get_string(0) == "2":
                pat, size = pad.find_child("at"), pad.find_child("size")
                return (
                    at.get_float(0) + pat.get_float(0),
                    at.get_float(1) + pat.get_float(1),
                    size.get_float(0),
                    size.get_float(1),
                )
    raise AssertionError("C11.2 not found")


def test_keepout_sits_under_c11_pad2_lower_spoke():
    _, layers, (x0, x1, y0, y1) = _keepout()
    px, py, w, h = _c11_pad2()
    assert layers == ("F.Cu",)
    # Spans the pad's width, starts at its lower edge, so the lower spoke sector
    # is covered and the pad itself stays reachable.
    assert x0 <= px - w / 2 + 0.01 and x1 >= px + w / 2 - 0.01
    assert y0 == pytest.approx(py + h / 2, abs=0.01)


def test_keepout_rejects_the_starving_track_but_not_the_reviewed_one():
    _, _, (x0, x1, y0, y1) = _keepout()
    half = TRACK_WIDTH / 2
    area = box(x0, y0, x1, y1)
    bad = LineString([(x0 - 1, FRESH_BAD_TRACK_Y), (x1 + 1, FRESH_BAD_TRACK_Y)]).buffer(half)
    assert bad.intersects(area)
    plan = json.loads((BOARD / "routing-plan.json").read_text())
    checked = 0
    for source in plan["copper"]:
        if not source.startswith("(segment") or '"F.Cu"' not in source:
            continue
        pts = {}
        for key in ("start", "end"):
            frag = source.split(f"({key} ")[1].split(")")[0].split()
            pts[key] = (float(frag[0]), float(frag[1]))
        copper = LineString([pts["start"], pts["end"]]).buffer(half)
        assert not copper.intersects(area), source
        checked += 1
    assert checked > 0


def test_staged_input_carries_keepout_and_plan_still_fingerprints(tmp_path):
    routing = runpy.run_path(str(BOARD / "routing_plan.py"))
    staged, _ = diffpair_bench.prepare_input(ere.BOARDS["03"], tmp_path)
    name, _, _ = _keepout()
    rule_areas = [z for z in PCB.load(str(staged)).zones if z.keepout is not None]
    assert [z.name for z in rule_areas] == [name]
    assert (
        rule_areas[0].keepout.tracks_allowed is False
        and rule_areas[0].keepout.vias_allowed is False
    )
    assert rule_areas[0].keepout.copperpour_allowed
    # The committed plan must replay onto the keepout-free input and its
    # fingerprint must not change when a fresh route carries the rule area.
    assert routing["fingerprint"](staged) == routing["fingerprint"](INPUT_PCB)
    plan = json.loads((BOARD / "routing-plan.json").read_text())
    assert routing["fingerprint"](staged) == plan["physical_sha256"]
