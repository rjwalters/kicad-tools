"""KiCad's pour fill is only nondeterministic around *shorted* copper (Issue #6215).

What #6115 saw
--------------
The first #6115 fixture (150 randomly angled segments, about a quarter on the
pour's own ``GND`` net) filled differently when one byte-identical board was
refilled serially on KiCad 10.0.6: 1 ring of 407 vertices on some runs, 6
rings (916-vertex outline) on others.  #6215 asked whether same-net copper
in a pour -- the normal case on a real board -- makes kicad-cli fills
irreproducible, which would undercut the #6052 guarantee.

What it actually is (measured 2026-10-07, KiCad 10.0.6, macOS)
--------------------------------------------------------------
Not same-net copper, and not output ordering.  The fixture's segments
overlapped each other across *different* nets: they were shorted.  On load,
KiCad's connectivity pass propagates one net across every connected cluster
of tracks, and when a cluster carries conflicting nets **which net wins is
arbitrary from process to process**.  The saved board shows it directly: the
same input came back with its 140-segment cluster relabelled ``A`` (10 of 30
refills), ``B`` (11), ``C`` (4) or ``GND`` (5).  A cluster relabelled to the
pour's own net is not knocked out, so the fill is genuinely different copper:
the 1-ring variant carried 112.9 mm^2 more pour, 43 mm^2 of it on top of
copper that had been ``A``/``B``/``C`` in the input.  Ring/vertex
normalisation does not hide it.

Minimal reproduction: a ``GND`` pour with one ``GND`` segment crossing one
``A`` segment.  Over 20 serial refills KiCad relabelled both segments ``A``
16 times (cross knocked out, 121-vertex ring) and ``GND`` 4 times (no
knock-out, 16-vertex ring).  ``MaximumThreads=1`` in ``kicad_advanced`` (via
``KICAD_CONFIG_HOME``) does not make it deterministic, and kicad-cli has no
thread-count flag or env var, so no setting fixes it.

Why it does not threaten #6052 or board 03
------------------------------------------
A short means a different-net cluster, which already fails DRC
(``shorting_items``), so such a board is not manufacturable whatever its fill
looks like.  On short-free copper the fill is reproducible, however much of
it sits on the pour's net:

* :func:`test_same_net_copper_in_a_pour_fills_reproducibly` -- 150 randomly
  angled segments, ~73% on ``GND``, no different-net contact: identical on
  every refill (measured 15/15 serial and 16/16 at 8-way concurrency).
* A fresh deterministic board-03 route (``--seed 42 --deterministic-budget
  --deterministic-rescue``, 24/24 nets, 0 DRC shorts) refilled identically
  12/12 serially and 16/16 at 8-way concurrency; the committed
  ``usb_joystick_routed.kicad_pcb`` 10/10.

``tests/test_pour_fill_determinism_5578.py::test_board03_pour_fill_is_reproducible``
uses :func:`kicad_net_reassignments` as a precondition, so if a future route
ever hands the fill a short, that test fails as a short rather than as a
spurious #6052 regression.
"""

from __future__ import annotations

import math
import random
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from kicad_tools.sexp import parse_file

_HEADER = """(kicad_pcb (version 20240108) (generator "test") (general (thickness 1.6))
(layers (0 "F.Cu" signal) (31 "B.Cu" signal))
(net 0 "") (net 1 "GND") (net 2 "A") (net 3 "B") (net 4 "C")
(zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "22222222-2222-4222-8222-222222222222")
  (hatch edge 0.5) (connect_pads (clearance 0.3)) (min_thickness 0.2)
  (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
  (polygon (pts (xy 100 100) (xy 140 100) (xy 140 140) (xy 100 140))))"""

#: One ``GND`` segment crossing one ``A`` segment inside the ``GND`` pour:
#: the smallest board on which KiCad's net propagation has to pick a winner.
_MINIMAL_SHORT = "\n".join(
    [
        _HEADER,
        '(segment (start 105 115) (end 125 115) (width 0.25) (layer "F.Cu") (net 2) '
        '(uuid "00000000-0000-4000-8000-000000000001"))',
        '(segment (start 115 105) (end 115 125) (width 0.25) (layer "F.Cu") (net 1) '
        '(uuid "00000000-0000-4000-8000-000000000002"))',
        ")",
    ]
)

_SEGMENTS = 150
_MIN_DIFFERENT_NET_GAP_MM = 0.35  # > the pour's 0.3 mm clearance
_REFILLS = 3


# ---------------------------------------------------------------------------
# Instruments (also used by test_pour_fill_determinism_5578)
# ---------------------------------------------------------------------------


def _track_nets(path: Path) -> dict[str, str]:
    """Map each top-level segment/via/arc UUID to its net *name*.

    Resolves both the numeric ``(net N)`` form kicad-tools writes and the
    name-only ``(net "NAME")`` form KiCad 10 saves.
    """
    doc = parse_file(path)
    names: dict[int, str] = {}
    for node in doc.children:
        if node.name == "net" and node.get_int(0) is not None:
            names[node.get_int(0)] = node.get_string(1) or ""
    out: dict[str, str] = {}
    for node in doc.children:
        if node.name not in ("segment", "via", "arc"):
            continue
        uuid_node, net_node = node.find("uuid"), node.find("net")
        if uuid_node is None or net_node is None:
            continue
        number = net_node.get_int(0)
        net = names.get(number, str(number)) if number is not None else net_node.get_string(0)
        out[uuid_node.get_string(0) or ""] = net or ""
    return out


def _raw_refill(pcb: Path, kicad_cli: Path) -> Path:
    """Refill ``pcb`` with bare ``kicad-cli pcb drc --refill-zones --save-board``.

    Deliberately *not* ``run_fill_zones``: that restores the input's element
    nets afterwards, which would hide exactly what is being measured here.
    """
    report = pcb.with_suffix(".drc.json")
    subprocess.run(
        [
            str(kicad_cli),
            "pcb",
            "drc",
            "--output",
            str(report),
            "--format",
            "json",
            "--refill-zones",
            "--save-board",
            str(pcb),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert report.exists(), f"kicad-cli did not refill {pcb}"
    return pcb


def kicad_net_reassignments(pcb: Path, kicad_cli: Path) -> list[tuple[str, str, str]]:
    """Copper whose net KiCad rewrites on load: ``[(uuid, input_net, saved_net)]``.

    KiCad propagates one net across each connected copper cluster.  On a
    short-free board that is a no-op; a cluster that shorts different nets
    gets an arbitrary winner, and the pour fill around it is then not
    reproducible (Issue #6215).  An empty list means the fill engine was
    handed no short.  Works on a temporary copy; ``pcb`` is not modified.
    """
    before = _track_nets(pcb)
    with tempfile.TemporaryDirectory(prefix="kct6215_") as tmp:
        copy = Path(tmp) / pcb.name
        shutil.copy(pcb, copy)
        for sidecar in (".kicad_pro", ".kicad_dru"):
            if pcb.with_suffix(sidecar).exists():
                shutil.copy(pcb.with_suffix(sidecar), copy.with_suffix(sidecar))
        after = _track_nets(_raw_refill(copy, kicad_cli))
    return sorted(
        (key, net, after[key]) for key, net in before.items() if key in after and after[key] != net
    )


def _fill_rings(path: Path) -> list[tuple[str, tuple[tuple[float, float], ...]]]:
    """Every ``filled_polygon`` ring as ``(layer, points)``, sorted."""
    rings = []
    for zone in parse_file(path).find_all("zone"):
        for filled in zone.find_all("filled_polygon"):
            layer = filled.find("layer").get_string(0) or ""
            pts = filled.find("pts")
            rings.append(
                (layer, tuple((xy.get_float(0), xy.get_float(1)) for xy in pts.find_all("xy")))
            )
    return sorted(rings)


# ---------------------------------------------------------------------------
# Fixture: lots of same-net copper in the pour, no short
# ---------------------------------------------------------------------------


def _same_net_board() -> tuple[str, int]:
    """Randomly angled segments, mostly on the pour's net, never touching another net.

    Same generator shape as the first #6115 fixture, plus a rejection step:
    a segment is dropped if it would come within
    :data:`_MIN_DIFFERENT_NET_GAP_MM` of a segment on a different net.
    Returns ``(board_text, gnd_segment_count)``.
    """
    from shapely.geometry import LineString

    rng = random.Random(3)
    uuids = random.Random(4)  # separate stream, so UUIDs do not perturb the geometry
    kept: list[tuple[object, int, str]] = []
    while len(kept) < _SEGMENTS:
        x, y = rng.uniform(105, 115), rng.uniform(105, 115)
        angle, length = rng.uniform(0, math.pi), rng.uniform(1, 4)
        width = rng.choice((0.1, 0.13, 0.2))
        net = rng.choice((1, 1, 2, 3, 4))
        x2, y2 = x + length * math.cos(angle), y + length * math.sin(angle)
        shape = LineString([(x, y), (x2, y2)]).buffer(width / 2)
        if any(n != net and shape.distance(s) < _MIN_DIFFERENT_NET_GAP_MM for s, n, _ in kept):
            continue
        kept.append(
            (
                shape,
                net,
                f"(segment (start {x:.3f} {y:.3f}) (end {x2:.3f} {y2:.3f}) (width {width}) "
                f'(layer "F.Cu") (net {net}) '
                f'(uuid "{uuid.UUID(int=uuids.getrandbits(128), version=4)}"))',
            )
        )
    text = "\n".join([_HEADER, *(sexp for _, _, sexp in kept), ")"])
    return text, sum(1 for _, net, _ in kept if net == 1)


def _kicad_cli() -> Path:
    from kicad_tools.cli.runner import find_kicad_cli

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        pytest.skip("kicad-cli not installed -- zone fill is a no-op, nothing to compare")
    return kicad_cli


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.timeout(180)
def test_same_net_copper_in_a_pour_fills_reproducibly(tmp_path):
    """Same-net copper inside a pour must not make the fill vary (Issue #6215).

    The #6052 guarantee rests on "identical input gives an identical fill".
    Real pours always contain same-net copper; this pins that it is *not* a
    nondeterminism trigger as long as nothing is shorted.  The fill is
    compared exactly, ring for ring, across serial refills of one board.
    """
    pytest.importorskip("shapely")
    kicad_cli = _kicad_cli()

    text, gnd_segments = _same_net_board()
    assert gnd_segments >= _SEGMENTS // 2, (
        f"fixture is not same-net-heavy ({gnd_segments}/{_SEGMENTS} on GND)"
    )
    source = tmp_path / "source.kicad_pcb"
    source.write_text(text)
    assert kicad_net_reassignments(source, kicad_cli) == [], (
        "the same-net fixture is shorted: KiCad relabelled some of its copper, "
        "so it no longer isolates same-net copper from the #6215 short trigger"
    )

    fills = []
    for run in range(_REFILLS):
        pcb = tmp_path / f"run{run}" / "board.kicad_pcb"
        pcb.parent.mkdir()
        pcb.write_text(text)
        fills.append(_fill_rings(_raw_refill(pcb, kicad_cli)))

    assert fills[0], "kicad-cli poured nothing -- the comparison is vacuous"
    for run, fill in enumerate(fills[1:], start=1):
        assert fill == fills[0], (
            f"refill {run} of a byte-identical, short-free board differs from refill 0 "
            f"({[len(r) for _, r in fill]} vs {[len(r) for _, r in fills[0]]} vertices "
            "per ring).  Same-net copper in a pour should not make kicad-cli's fill "
            "nondeterministic (Issue #6215); if this fails, #6052's reproducibility "
            "guarantee no longer holds on real boards."
        )


@pytest.mark.timeout(60)
def test_net_reassignment_detector_flags_a_short(tmp_path):
    """:func:`kicad_net_reassignments` must catch a short, whichever net KiCad picks.

    KiCad relabels both crossing segments to one net -- ``A`` or ``GND``,
    arbitrarily -- so exactly one of them changes either way.  This keeps the
    board-03 precondition from passing vacuously.
    """
    kicad_cli = _kicad_cli()
    pcb = tmp_path / "short.kicad_pcb"
    pcb.write_text(_MINIMAL_SHORT)
    original = pcb.read_text()

    changed = kicad_net_reassignments(pcb, kicad_cli)

    assert len(changed) == 1, f"expected exactly one relabelled segment, got {changed}"
    _, before, after = changed[0]
    assert {before, after} == {"A", "GND"}, changed
    assert pcb.read_text() == original, "the detector must not modify its input"
