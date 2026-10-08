"""Fast proof that the #6052 UUID canonicalisation fixes an order-sensitive fill (Issue #6115).

``tests/test_pour_fill_determinism_5578.py::test_board03_pour_fill_is_reproducible``
guards the fill *geometry* but needs two full board-03 routes (~22 min).  The
``board06_pour_escape`` byte-identity test does not: its fill does not depend
on track order, so it passes with or without the fix.

This module builds a tiny synthetic board whose kicad-cli fill really does
depend on the order tracks are loaded in, so a handful of refills proves both
halves:

* **control** -- shuffles of the same copper (random UUIDs, shuffled file
  order) fill to *different* ``filled_polygon`` rings.  If KiCad ever stops
  being order-sensitive on this fixture the control fails, so the test cannot
  pass vacuously.
* **fix** -- the same shuffles after
  :func:`~kicad_tools.core.canonical_uuids.canonicalize_board_uuids` all fill
  to the same rings.

Fixture: a 40x40 mm GND pour on F.Cu with 80 axis-aligned segments (0.2/0.3 mm
wide, on a 0.5 mm grid, nets A/B/C) in a 10x10 mm patch.  Their knock-outs
share many collinear edges, and the vertex count of the merged outline
(1468-1471 on KiCad 10.0.x) depends on the order the tracks are loaded in.

Fixture history (why it looks like this): a first version used randomly
angled segments with about a quarter on the pour's own net.  It *looked* very
order-sensitive, but KiCad 10.0.6 fills that board non-deterministically even
for byte-identical input (island/outline vertex counts changed from run to run
when the same file was refilled serially), so it measured kicad-cli noise and
made both assertions flaky.  Same-net tracks are what triggered it; this
fixture has none, and refilling one board repeatedly gives one result.
"""

from __future__ import annotations

import random
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kicad_tools.core.canonical_uuids import canonicalize_board_uuids
from kicad_tools.sexp import parse_file, parse_string

_SEGMENTS = 80
_NETS = (2, 3, 4)  # none on net 1 (GND, the pour's net) -- see module docstring
# Seeds 0, 1 and 3 fill to three different outlines on KiCad 10.0.1 and 10.0.6.
_RAW_SEEDS = (0, 1, 3)
_CANON_SEEDS = (0, 1, 3)

_HEADER = """(kicad_pcb (version 20240108) (generator "test") (general (thickness 1.6))
(layers (0 "F.Cu" signal) (31 "B.Cu" signal))
(net 0 "") (net 1 "GND") (net 2 "A") (net 3 "B") (net 4 "C")
(zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "22222222-2222-4222-8222-222222222222")
  (hatch edge 0.5) (connect_pads (clearance 0.3)) (min_thickness 0.2)
  (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
  (polygon (pts (xy 100 100) (xy 140 100) (xy 140 140) (xy 100 140))))"""


def _segments() -> list[tuple[float, float, float, float, float, int]]:
    rng = random.Random(5)
    out = []
    for _ in range(_SEGMENTS):
        x, y = (105 + 0.5 * rng.randrange(0, 20) for _ in range(2))
        dx, dy = rng.choice(((1, 0), (0, 1)))
        length = rng.randrange(1, 6) * 0.5
        out.append(
            (x, y, x + dx * length, y + dy * length, rng.choice((0.2, 0.2, 0.3)), rng.choice(_NETS))
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

    jobs = [(seed, False) for seed in _RAW_SEEDS] + [(seed, True) for seed in _CANON_SEEDS]
    with ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(_refill, tmp_path, s, c, kicad_cli) for s, c in jobs]
        results = [f.result() for f in futures]
    raw, canon = results[: len(_RAW_SEEDS)], results[len(_RAW_SEEDS) :]

    assert raw[0] and canon[0], "kicad-cli poured nothing -- the comparison is vacuous"
    distinct_raw = {repr(r) for r in raw}
    assert len(distinct_raw) > 1, (
        "control failed: every un-canonicalised track order filled identically, so this "
        "fixture no longer detects order-sensitive fills (Issue #6115)"
    )
    assert all(r == canon[0] for r in canon), (
        "canonicalised boards with identical copper filled differently: the fill is still "
        "sensitive to track order (Issue #6052)"
    )
