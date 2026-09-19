"""Zone pour-fill reproducibility across identical routes (Issue #5578).

Background
----------

Issue #5578 reported that board 03's ``In2.Cu`` copper pour decomposed into a
different number of ``filled_polygon`` islands on otherwise-identical
``kct route`` runs (5 islands on three runs, 20 on a fourth) even though the
routed copper was byte-identical as a multiset.  The routed-copper half of
that report was retracted -- routed copper *is* reproducible, and
``tests/test_routing_plan_5510.py::test_board_copper_unchanged_by_plan_stage``
hard-asserts it.  The pour-fill half was real.

Root cause (measured 2026-09-19, KiCad 10.0.5)
----------------------------------------------

Board 03 ships a hand-authored ``VCC`` pour on ``In2.Cu``.  ``kct route``'s
auto-pour step (:func:`kicad_tools.router.auto_pour.auto_pour_if_missing`)
adds a second pour for ``VBUS`` on the *same* layer at the *same* priority
(0), and warns that one of the two will be starved of copper.  Which one is
starved is decided by KiCad's own zone ordering when
``kicad-cli pcb drc --refill-zones --save-board`` re-serializes the board
during the fill step -- and that ordering tie-breaks on the zone **UUID**.

The auto-created zone's UUID came from :func:`uuid.uuid4` because the
router's deterministic-UUID toggle (Issue #3272,
:func:`kicad_tools.router.primitives.enable_deterministic_uuids`) is only
flipped on inside ``Autorouter.route_all_negotiated`` -- which runs *long
after* auto-pour.  So every route rolled a fresh random UUID, and whether it
sorted before or after the board's pre-existing ``VCC`` zone UUID
(``99b8f0a0-...``) decided the whole ``In2.Cu`` fill:

    two boards identical byte-for-byte except the VBUS zone UUID,
    filled by the same kicad-cli invocation:

      VBUS uuid e4c41fb8-...  ->  VCC: 4 islands / VBUS: 1 island
      VBUS uuid 18329af8-...  ->  VCC: 0 islands / VBUS: 4 islands

The fix makes generated zone UUIDs a pure function of the zone's own content
(net, layer, priority, boundary) so the same board always produces the same
zone UUID -- with or without ``--seed``, and regardless of when the router's
UUID toggle is flipped.

What this module tests
----------------------

* :func:`test_auto_pour_zone_uuid_is_reproducible` (fast) -- the direct
  regression: repeated ``auto_pour_if_missing`` runs over the same input
  produce byte-identical zone blocks.  This is the test that fails on the
  pre-fix tree.
* :func:`test_board03_pour_fill_is_reproducible` (``@pytest.mark.slow``) --
  the end-to-end check the issue asked for: route board 03 twice with the
  deterministic invocation and compare the pour fills with a
  decomposition-insensitive instrument (``shapely.ops.unary_union`` per
  net+layer), *and* with the island count, which the fix also stabilises.

The union-based comparison is deliberately separate from the island-count
comparison: "same total copper" and "same fragmentation" are different
guarantees, and #5578's scope guards call out that conflating them is what
produced the original false report.

Deliberately out of scope, tracked separately
---------------------------------------------

* **#5590** -- making the outcome reproducible does not make it *fair*.
  Board 03's two ``In2.Cu`` pours still share a layer and a priority, so
  one of them is still starved.  :func:`test_board03_pour_fill_is_reproducible`
  asserts both end up with non-zero copper, which pins the board to the
  non-degenerate branch but does not fix the underlying allocation.
* **#5591** -- the fleet boards emit pads with no ``(uuid ...)``, so KiCad
  invents a random one per pad on load.  That is what
  :data:`_UNION_AREA_TOLERANCE_MM2` exists to absorb.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from kicad_tools.sexp import parse_file

REPO_ROOT = Path(__file__).resolve().parents[1]

BOARD03 = REPO_ROOT / "boards/03-usb-joystick/output/usb_joystick.kicad_pcb"

#: The flags that make ``kct route`` reproducible.  Same set
#: ``tests/test_routing_plan_5510.py`` uses.
_DETERMINISTIC_FLAGS = ("--seed", "42", "--deterministic-budget")


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------


def _zone_blocks(path: Path) -> list[str]:
    """Whole ``(zone ...)`` s-expressions, paren-balanced, in file order.

    ``filled_polygon`` children are kept: this instrument is used to compare
    the *definitions* the fill engine is handed, and for that the fills are
    harmless.  UUIDs are NOT normalised -- the zone UUID is exactly what
    Issue #5578 is about.
    """
    blocks: list[str] = []
    text = path.read_text()
    idx = 0
    while True:
        start = text.find("(zone", idx)
        if start < 0:
            return blocks
        depth = 0
        pos = start
        while pos < len(text):
            if text[pos] == "(":
                depth += 1
            elif text[pos] == ")":
                depth -= 1
                if depth == 0:
                    pos += 1
                    break
            pos += 1
        blocks.append(re.sub(r"\s+", " ", text[start:pos]))
        idx = pos


def _zone_uuids(path: Path) -> list[str]:
    """The ``uuid`` of every ``(zone ...)`` in file order."""
    doc = parse_file(path)
    out: list[str] = []
    for zone in doc.find_all("zone"):
        node = zone.find("uuid")
        out.append((node.get_string(0) or "") if node is not None else "")
    return out


def _fill_rings(path: Path) -> dict[tuple[str, str], list[list[tuple[float, float]]]]:
    """Map ``(net, layer) -> list of filled_polygon point rings``.

    Keyed by net + layer rather than by zone so the comparison survives a
    zone being re-ordered or re-UUID'd; the electrical question is "which
    copper does this net own on this layer", not "which zone object holds
    it".
    """
    doc = parse_file(path)
    out: dict[tuple[str, str], list[list[tuple[float, float]]]] = defaultdict(list)
    for zone in doc.find_all("zone"):
        net_node = zone.find("net")
        net = ""
        if net_node is not None and net_node.children:
            net = str(net_node.children[0].value)
        zone_layer_node = zone.find("layer")
        zone_layer = (zone_layer_node.get_string(0) or "") if zone_layer_node is not None else ""
        for filled in zone.find_all("filled_polygon"):
            layer_node = filled.find("layer")
            layer = (layer_node.get_string(0) or "") if layer_node is not None else zone_layer
            pts_node = filled.find("pts")
            if pts_node is None:
                continue
            ring = [
                (xy.get_float(0) or 0.0, xy.get_float(1) or 0.0) for xy in pts_node.find_all("xy")
            ]
            if len(ring) >= 3:
                out[(net, layer)].append(ring)
    return dict(out)


def _fill_union_signature(path: Path) -> dict[tuple[str, str], tuple[float, tuple[float, ...]]]:
    """Decomposition-insensitive signature of a board's pour fills.

    For each ``(net, layer)`` the polygons are unioned with
    ``shapely.ops.unary_union`` and reduced to ``(area, bounds)`` rounded to
    1 nm.  Two boards with the same signature own exactly the same copper on
    every net+layer even if the fill engine split it into a different number
    of ``filled_polygon`` islands, emitted them in a different order, or
    walked one island's ring from a different start vertex.

    This is the pour-fill analogue of
    ``tests/test_routing_plan_5510.py::_copper_elements`` -- see that
    module's note for why an order-insensitive instrument is mandatory here.

    Compare the results with :func:`_assert_same_fill_copper`, not ``==``:
    the area carries a documented sub-micron residual (see that helper).
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    signature: dict[tuple[str, str], tuple[float, tuple[float, ...]]] = {}
    for key, rings in _fill_rings(path).items():
        polys = []
        for ring in rings:
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty:
                polys.append(poly)
        if not polys:
            signature[key] = (0.0, ())
            continue
        merged = unary_union(polys)
        signature[key] = (
            round(merged.area, 6),
            tuple(round(v, 6) for v in merged.bounds),
        )
    return signature


def _island_counts(path: Path) -> dict[tuple[str, str], int]:
    """Map ``(net, layer) -> number of filled_polygon islands``."""
    return {key: len(rings) for key, rings in _fill_rings(path).items()}


#: Per-``(net, layer)`` union-area tolerance, mm^2.
#:
#: NOT slack for the bug this module gates -- that one moved whole planes
#: (board 03's ``VCC`` pour went from 2.25 mm^2 of copper to **zero**, three
#: orders of magnitude past this epsilon).  It absorbs a separate, much
#: smaller residual that survives the #5578 fix and is tracked on its own
#: in **#5591**: board 03's committed fixture declares pads with no
#: ``(uuid ...)``, so
#: KiCad invents a random one per pad on load and ``--save-board`` persists
#: it.  That perturbs KiCad's internal board-item ordering, which shows up
#: as (a) a pure permutation of the routed-copper emission order (already
#: handled by ``_copper_elements``' sorted multiset) and (b) a one-vertex
#: wobble in the largest ``F.Cu`` GND fill ring.  Measured over three
#: post-fix board-03 routes on 2026-09-19 (KiCad 10.0.5): ring lengths 7660
#: / 7659 / 7660 and areas 3793.945551 / 3793.945555 / 3793.945554 mm^2 --
#: a spread of 4e-6 mm^2 (4 um^2, ~1e-9 relative).  The bounds and the
#: island counts were identical on all three, so both are asserted exactly.
#: Tighten this to ``0.0`` once #5591 lands.
_UNION_AREA_TOLERANCE_MM2 = 1e-3


def _assert_same_fill_copper(
    sig_a: dict[tuple[str, str], tuple[float, tuple[float, ...]]],
    sig_b: dict[tuple[str, str], tuple[float, tuple[float, ...]]],
    *,
    context: str,
) -> None:
    """Assert two union signatures describe the same pour copper."""
    assert sig_a.keys() == sig_b.keys(), (
        f"{context}: the two runs poured copper on a different set of "
        f"net+layer pairs.\na: {sorted(sig_a)}\nb: {sorted(sig_b)}"
    )
    for key in sorted(sig_a):
        area_a, bounds_a = sig_a[key]
        area_b, bounds_b = sig_b[key]
        assert bounds_a == bounds_b, (
            f"{context}: {key} pour occupies a different bounding box "
            f"between runs.\na: {bounds_a}\nb: {bounds_b}"
        )
        assert abs(area_a - area_b) <= _UNION_AREA_TOLERANCE_MM2, (
            f"{context}: {key} owns a different amount of plane copper "
            f"between two identical runs ({area_a} vs {area_b} mm^2, "
            f"tolerance {_UNION_AREA_TOLERANCE_MM2}).  This is the "
            "union-of-islands comparison, so it is insensitive to island "
            "ordering and decomposition: a failure here means a net "
            "genuinely gained or lost pour copper (Issue #5578)."
        )


# ---------------------------------------------------------------------------
# Fast regression: the auto-created zone must not carry a random UUID
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seeded", [False, True], ids=["unseeded", "seeded"])
def test_auto_pour_zone_uuid_is_reproducible(tmp_path, seeded):
    """Auto-pour must emit the same zone UUID for the same board every run.

    Issue #5578 root cause.  ``kct route``'s auto-pour runs *before*
    ``route_all_negotiated`` turns on the deterministic-UUID toggle, so the
    generated zone used to get a fresh ``uuid.uuid4()`` on every run.  On
    board 03 that UUID is the tie-break KiCad uses to decide which of two
    equal-priority ``In2.Cu`` zones wins the contested copper, so a random
    UUID means a random pour fill.

    The ``unseeded`` parametrization is the load-bearing one: a bare
    ``kct route`` (no ``--seed``) must be just as reproducible here, because
    the zone geometry does not depend on the routing RNG at all.
    """
    import random

    from kicad_tools.router.auto_pour import auto_pour_if_missing

    uuid_runs: list[list[str]] = []
    block_runs: list[list[str]] = []
    for run in range(3):
        if seeded:
            random.seed(42)
        else:
            random.seed()
        target = tmp_path / f"run{run}.kicad_pcb"
        shutil.copy(BOARD03, target)
        created, names = auto_pour_if_missing(target, quiet=True)
        assert created > 0, "board 03 must still exercise the auto-pour path"
        assert "VBUS" in names
        uuid_runs.append(_zone_uuids(target))
        block_runs.append(_zone_blocks(target))

    assert uuid_runs[0] == uuid_runs[1] == uuid_runs[2], (
        "auto_pour_if_missing produced different zone UUIDs across runs over "
        "the same input board.  On board 03 the auto-created VBUS pour shares "
        "In2.Cu and priority 0 with the pre-existing VCC pour, so KiCad's "
        "fill resolver tie-breaks on the zone UUID: a random UUID makes the "
        "whole In2.Cu fill (and which net is starved) random too.  Issue "
        f"#5578 regression.  Got: {uuid_runs}"
    )
    assert block_runs[0] == block_runs[1] == block_runs[2], (
        "auto_pour_if_missing produced different zone blocks across runs over "
        "the same input board (Issue #5578)."
    )


def test_auto_pour_zone_uuid_survives_the_router_uuid_toggle(tmp_path):
    """The zone UUID must not depend on the #3272 deterministic-UUID toggle.

    ``enable_deterministic_uuids`` is module-level state that
    ``route_all_negotiated`` latches on and never turns off, so within one
    process the toggle is off for the first route's auto-pour and *on* for
    every later one.  If the zone UUID tracked that toggle, the first route
    of a session would disagree with the rest -- exactly the shape of
    non-determinism #5578 is closing.
    """
    import random

    from kicad_tools.router.auto_pour import auto_pour_if_missing
    from kicad_tools.router.primitives import (
        enable_deterministic_uuids,
        reset_deterministic_uuids,
    )

    def run(name: str) -> list[str]:
        target = tmp_path / f"{name}.kicad_pcb"
        shutil.copy(BOARD03, target)
        auto_pour_if_missing(target, quiet=True)
        return _zone_uuids(target)

    reset_deterministic_uuids()
    random.seed(1)
    off = run("toggle_off")
    try:
        enable_deterministic_uuids(True)
        random.seed(999)
        on = run("toggle_on")
    finally:
        reset_deterministic_uuids()

    assert off == on, (
        "The auto-created zone's UUID changed when the router's "
        "deterministic-UUID toggle flipped.  Generated zone UUIDs must be a "
        "pure function of the zone's own content so they are stable across "
        "routes in the same process (Issue #5578)."
    )


def test_generated_zone_uuids_are_unique_within_a_board(tmp_path):
    """Deterministic must not mean colliding.

    KiCad resolves board items by UUID; two zones sharing one would make the
    board ambiguous.  The derivation keys on the zone's own content, so this
    guards the de-duplication path that keeps distinct zones distinct.
    """
    from kicad_tools.router.auto_pour import auto_pour_if_missing

    target = tmp_path / "unique.kicad_pcb"
    shutil.copy(BOARD03, target)
    auto_pour_if_missing(target, quiet=True)

    uuids = _zone_uuids(target)
    assert all(uuids), "every zone must carry a uuid"
    assert len(set(uuids)) == len(uuids), f"duplicate zone UUID(s) emitted: {uuids}"


def test_zone_generator_uuids_differ_for_distinct_zones():
    """Two zones that differ in net or layer must not derive the same UUID."""
    from kicad_tools.zones.generator import GeneratedZone, ZoneConfig

    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    a = GeneratedZone(config=ZoneConfig(net="GND", layer="F.Cu"), net_number=1, boundary=square)
    b = GeneratedZone(config=ZoneConfig(net="GND", layer="B.Cu"), net_number=1, boundary=square)
    c = GeneratedZone(config=ZoneConfig(net="VCC", layer="F.Cu"), net_number=2, boundary=square)
    a_again = GeneratedZone(
        config=ZoneConfig(net="GND", layer="F.Cu"), net_number=1, boundary=square
    )

    assert a.uuid == a_again.uuid, "same content must derive the same UUID"
    assert len({a.uuid, b.uuid, c.uuid}) == 3, "distinct zones must derive distinct UUIDs"
    # Still a syntactically valid KiCad UUID.
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", a.uuid), (
        a.uuid
    )


# ---------------------------------------------------------------------------
# End-to-end: two identical routes must produce the same pour fill
# ---------------------------------------------------------------------------


def _route(pcb: Path, out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONHASHSEED="0")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "route",
            str(pcb),
            "-o",
            str(out),
            *_DETERMINISTIC_FLAGS,
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


@pytest.mark.slow
@pytest.mark.timeout(1800)
def test_board03_pour_fill_is_reproducible(tmp_path):
    """Two identical board-03 routes must produce the same ``In2.Cu`` pour.

    Board 03 is the reproduction from Issue #5578: a hand-authored ``VCC``
    pour on ``In2.Cu`` plus an auto-poured ``VBUS`` pour on the same layer at
    the same priority.  Both the decomposition-insensitive union signature
    and the raw island count are asserted -- see the module docstring for why
    they are separate guarantees.
    """
    pytest.importorskip("shapely")
    from kicad_tools.cli.runner import find_kicad_cli

    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not installed -- zone fill is a no-op, nothing to compare")

    a = tmp_path / "a.kicad_pcb"
    b = tmp_path / "b.kicad_pcb"
    _route(BOARD03, a)
    _route(BOARD03, b)
    assert a.exists() and b.exists()

    sig_a = _fill_union_signature(a)
    sig_b = _fill_union_signature(b)
    assert sig_a, "no pour fill parsed -- the instrument (or the fill step) is broken"
    # Both contested In2.Cu pours must end up with real copper.  Making the
    # zone UUID deterministic fixes *which* branch KiCad's equal-priority
    # tie-break takes, but the two branches are not equally good: the other
    # one leaves VCC with ZERO filled_polygon islands.  Asserting both pours
    # are non-empty pins board 03 to the non-degenerate branch, so a future
    # change to the UUID derivation cannot silently starve a plane.
    for key in (("VCC", "In2.Cu"), ("VBUS", "In2.Cu")):
        assert key in sig_a, (
            f"board 03 poured no copper at all for {key}.  Both In2.Cu pours "
            "share a layer and a priority, so KiCad awards the contested "
            "region to exactly one of them; this run took the branch that "
            f"starves {key[0]} (Issue #5578).  Got: {sorted(sig_a)}"
        )
        assert sig_a[key][0] > 0.0, f"{key} pour has zero area: {sig_a[key]}"

    _assert_same_fill_copper(sig_a, sig_b, context="board 03, two identical routes")

    counts_a = _island_counts(a)
    counts_b = _island_counts(b)
    assert counts_a == counts_b, (
        "two identical board-03 routes decomposed the same pour copper into a "
        "different number of filled_polygon islands.  The union signatures "
        "matched, so no copper changed -- but the fragmentation did, which "
        "makes committed routed boards diff spuriously (Issue #5578).\n"
        f"a: {counts_a}\nb: {counts_b}"
    )
