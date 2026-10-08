"""Fast proof that the #6052 UUID canonicalisation fixes an order-sensitive fill (Issue #6115).

``tests/test_pour_fill_determinism_5578.py::test_board03_pour_fill_is_reproducible``
guards the fill *geometry* but needs two full board-03 routes (~22 min).  The
``board06_pour_escape`` byte-identity test does not: its fill does not depend
on track order, so it passes with or without the fix.

This module builds a tiny synthetic board whose kicad-cli fill really does
depend on the order tracks are loaded in, so one pair of refills proves both
halves:

* **control** -- two shuffles of the same copper (random UUIDs, shuffled file
  order) fill to *different* ``filled_polygon`` rings.  If KiCad ever stops
  being order-sensitive on this fixture the control fails, so the test cannot
  pass vacuously.
* **fix** -- the same two shuffles after
  :func:`~kicad_tools.core.canonical_uuids.canonicalize_board_uuids` fill to
  identical rings.

Fixture: a 40x40 mm GND pour on F.Cu with 150 short, thin, randomly-angled
track segments scattered in a 10x10 mm patch.  About a quarter belong to the
pour's own net (GND) -- same-net tracks overlapping the pour are what make
KiCad's island/knock-out result depend on load order; segments on other nets
alone leave the fill order-insensitive.  Generated from a fixed seed, so the
board is identical on every run.  Four kicad-cli refills run in parallel
(a few seconds each).
"""

from __future__ import annotations

import math
import random
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kicad_tools.core.canonical_uuids import canonicalize_board_uuids
from kicad_tools.sexp import parse_file, parse_string

_SEGMENTS = 150
_NETS = (1, 2, 3, 4)  # net 1 is GND, the pour's net
_SHUFFLE_SEEDS = (0, 1)

_HEADER = """(kicad_pcb (version 20240108) (generator "test") (general (thickness 1.6))
(layers (0 "F.Cu" signal) (31 "B.Cu" signal))
(net 0 "") (net 1 "GND") (net 2 "A") (net 3 "B") (net 4 "C")
(zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "22222222-2222-4222-8222-222222222222")
  (hatch edge 0.5) (connect_pads (clearance 0.3)) (min_thickness 0.2)
  (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
  (polygon (pts (xy 100 100) (xy 140 100) (xy 140 140) (xy 100 140))))"""


def _segments() -> list[tuple[float, float, float, float, float, int]]:
    rng = random.Random(3)
    out = []
    for _ in range(_SEGMENTS):
        x, y = rng.uniform(105, 115), rng.uniform(105, 115)
        angle, length = rng.uniform(0, math.pi), rng.uniform(1, 4)
        out.append(
            (
                x,
                y,
                x + length * math.cos(angle),
                y + length * math.sin(angle),
                rng.choice((0.1, 0.13, 0.2)),
                rng.choice(_NETS),
            )
        )
    return out


def _board_text(shuffle_seed: int) -> str:
    """The fixture with random copper UUIDs in a ``shuffle_seed``-dependent order."""
    rng = random.Random(shuffle_seed)
    copper = [
        f"(segment (start {x1:.3f} {y1:.3f}) (end {x2:.3f} {y2:.3f}) (width {w}) "
        f'(layer "F.Cu") (net {net}) '
        f'(uuid "{uuid.UUID(int=rng.getrandbits(128), version=4)}"))'
        for x1, y1, x2, y2, w, net in _segments()
    ]
    rng.shuffle(copper)
    return "\n".join([_HEADER, *copper, ")"])


def _fill_rings(path: Path) -> list[tuple[str, str, tuple[tuple[float, float], ...]]]:
    """Every ``filled_polygon`` ring as ``(net, layer, points)``, sorted."""
    rings = defaultdict(list)
    for zone in parse_file(path).find_all("zone"):
        net = zone.find("net").get_string(0) or ""
        for filled in zone.find_all("filled_polygon"):
            layer = filled.find("layer").get_string(0) or ""
            pts = filled.find("pts")
            ring = tuple((xy.get_float(0), xy.get_float(1)) for xy in pts.find_all("xy"))
            rings[(net, layer)].append(ring)
    return sorted((n, ly, r) for (n, ly), rs in rings.items() for r in rs)


def _refill(root: Path, shuffle_seed: int, canonical: bool, kicad_cli: Path):
    from kicad_tools.cli.runner import _run_fill_zones_via_drc

    pcb = root / f"{'canon' if canonical else 'raw'}{shuffle_seed}" / "board.kicad_pcb"
    pcb.parent.mkdir(parents=True)
    text = _board_text(shuffle_seed)
    if canonical:
        doc = parse_string(text)
        canonicalize_board_uuids(doc)
        text = doc.to_string()
    pcb.write_text(text)
    # The bare kicad-cli fill step (run_fill_zones adds a second DRC pass for
    # thermal remediation, which doubles the runtime and is not order-related).
    result = _run_fill_zones_via_drc(pcb, None, kicad_cli)
    assert result.success, result.stderr
    return _fill_rings(pcb)


@pytest.mark.timeout(180)
def test_canonicalisation_makes_an_order_sensitive_fill_reproducible(tmp_path):
    pytest.importorskip("shapely")
    from kicad_tools.cli.runner import find_kicad_cli

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        pytest.skip("kicad-cli not installed -- zone fill is a no-op, nothing to compare")

    jobs = [(seed, canonical) for canonical in (False, True) for seed in _SHUFFLE_SEEDS]
    with ThreadPoolExecutor(len(jobs)) as pool:
        futures = [pool.submit(_refill, tmp_path, s, c, kicad_cli) for s, c in jobs]
        raw_a, raw_b, canon_a, canon_b = (f.result() for f in futures)

    assert raw_a and canon_a, "kicad-cli poured nothing -- the comparison is vacuous"
    assert raw_a != raw_b, (
        "control failed: two track orders without canonicalisation filled identically, so "
        "this fixture no longer detects order-sensitive fills (Issue #6115)"
    )
    assert canon_a == canon_b, (
        "canonicalised boards with identical copper filled differently: the fill is still "
        "sensitive to track order (Issue #6052)"
    )
