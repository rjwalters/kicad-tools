"""Pour-oracle completion must not depend on how KiCad names its links (Issue #5934).

Measured on board 03 (KiCad 10.0.1): ``kicad-cli pcb drc`` run repeatedly on
one byte-identical board reports the same NUMBER of ``unconnected_items`` per
net every time, but not the same records.  KiCad's ratsnest names an arbitrary
spanning tree over the copper clusters, and wherever several items share an
anchor (a via and the track ending on it, a pad and the track leaving it, a
zone island and the via inside it) an arbitrary one of them.  The report order
moves too.  Capping KiCad to one thread (``MaximumThreads=1``) did not change
this, so it cannot be configured away.

Three oracle-stage replays from one post-fill board landed 561, 547 and 563
copper nodes before this fix.  These tests pin each layer of the fix without
kicad-cli:

* :func:`canonical_links` makes the order and end orientation irrelevant;
* :meth:`PourLinkCloser._cluster_links` rebuilds a net's links from the
  board's own clusters whenever their count agrees with KiCad's, so the
  closer's input no longer depends on which items KiCad named;
* :func:`_merge_unconnected_per_net` makes the two-project union depend on
  per-net counts only, so the counts the loop compares are reproducible.

The end-to-end guarantee (two full board-03 routes, oracle on, identical
copper and pours) is ``tests/test_pour_fill_determinism_5578.py``.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from shapely.geometry import box  # noqa: E402

from kicad_tools.drc.geometric import GeometricDRCResult  # noqa: E402
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType  # noqa: E402
from kicad_tools.router.oracle_completion import (  # noqa: E402
    ClosureAttempt,
    OracleEndpoint,
    OracleLink,
    PourLinkCloser,
    canonical_links,
    links_from_violations,
    run_oracle_completion,
)


def _violation(a: tuple[str, float, float], b: tuple[str, float, float]) -> DRCViolation:
    return DRCViolation(
        type=ViolationType.UNCONNECTED_ITEMS,
        type_str="unconnected_items",
        severity=Severity.ERROR,
        message="Missing connection between items",
        locations=[Location(a[1], a[2]), Location(b[1], b[2])],
        items=[a[0], b[0]],
    )


def _links(*pairs) -> list[OracleLink]:
    return links_from_violations([_violation(a, b) for a, b in pairs])


# Two reports KiCad actually produced, back to back, for one board-03 file
# (round-2 board, 2026-10-06): same per-net counts, different items named.
_RUN_A = (
    (("Pad 2 [VCC] of U1 on F.Cu", 149.7, 79.838), ("Via [VCC] on F.Cu - B.Cu", 152.16, 80.26)),
    (("Via [VCC] on F.Cu - B.Cu", 160.28, 67.5), ("Pad 2 [VCC] of J3 on F.Cu", 165.976, 72.96)),
    (
        ("Zone [VCC] on In2.Cu, priority 0", 109.0, 58.0),
        ("Zone [VCC] on In2.Cu, priority 0", 109.0, 58.0),
    ),
    (
        ("Zone [GND] on F.Cu, priority 0", 109.0, 58.0),
        ("Zone [GND] on In1.Cu, priority 0", 109.0, 58.0),
    ),
)
_RUN_B = (
    (
        ("Pad 2 [VCC] of U1 on F.Cu", 149.7, 79.838),
        ("Track [VCC] on F.Cu, length 1.2400 mm", 152.16, 81.5),
    ),
    (
        ("Track [VCC] on F.Cu, length 1.3800 mm", 158.9, 67.5),
        ("Pad 2 [VCC] of J3 on F.Cu", 165.976, 72.96),
    ),
    (
        ("Track [VCC] on F.Cu, length 1.2400 mm", 152.16, 81.5),
        ("Via [VCC] on F.Cu - B.Cu", 154.72, 80.47),
    ),
    (
        ("Zone [GND] on F.Cu, priority 0", 109.0, 58.0),
        ("Zone [GND] on B.Cu, priority 0", 109.0, 58.0),
    ),
)


class TestCanonicalLinks:
    def test_order_and_orientation_do_not_matter(self):
        links = _links(*_RUN_A)
        expected = canonical_links(links)
        rng = random.Random(5934)
        for _ in range(20):
            shuffled = list(links)
            rng.shuffle(shuffled)
            flipped = [
                OracleLink(net=lk.net, a=lk.b, b=lk.a) if rng.random() < 0.5 else lk
                for lk in shuffled
            ]
            assert canonical_links(flipped) == expected

    def test_zone_layer_naming_does_not_move_a_link(self):
        """``Zone F.Cu <-> Zone In1.Cu`` and ``... <-> B.Cu`` sort to the same slot."""
        pad = (
            ("Pad 2 [GND] of C11 on F.Cu", 131.275, 92.5),
            ("Zone [GND] on F.Cu, priority 0", 109.0, 58.0),
        )
        one = canonical_links(_links(pad, _RUN_A[3]))
        two = canonical_links(_links(_RUN_B[3], pad))
        assert [lk.a.kind + lk.b.kind for lk in one] == [lk.a.kind + lk.b.kind for lk in two]
        assert [lk.a.description for lk in one][0] == [lk.a.description for lk in two][0]

    def test_natural_reference_order(self):
        links = _links(
            (("Pad 1 [GND] of R10 on F.Cu", 1.0, 1.0), ("Zone [GND] on In1.Cu", 0.0, 0.0)),
            (("Pad 1 [GND] of R2 on F.Cu", 2.0, 2.0), ("Zone [GND] on In1.Cu", 0.0, 0.0)),
        )
        assert [lk.a.ref for lk in canonical_links(links)] == ["R2", "R10"]

    def test_loop_hands_the_closer_the_same_links_for_any_report_order(self, tmp_path: Path):
        """``run_oracle_completion`` itself canonicalizes before calling the closer."""
        violations = [_violation(a, b) for a, b in _RUN_A]
        seen: list[list[str]] = []
        for perm in itertools.islice(itertools.permutations(violations), 6):
            board = tmp_path / "b.kicad_pcb"
            board.write_text("x")
            reports = iter(
                [
                    GeometricDRCResult(ran=True, unconnected_items=list(perm)),
                    GeometricDRCResult(ran=True, unconnected_items=[]),
                ]
            )

            def closer(path, links, banned):
                seen.append([lk.describe() for lk in links])
                return ClosureAttempt(applied=1)

            run_oracle_completion(
                board,
                oracle=lambda _p: next(reports),
                closer=closer,
                nets={"VCC", "GND"},
                max_rounds=1,
            )
        assert len(seen) == 6
        assert all(s == seen[0] for s in seen)


# ---------------------------------------------------------------------------
# Cluster-level closing
# ---------------------------------------------------------------------------


@dataclass
class _Pad:
    number: str
    net_name: str


@dataclass
class _Footprint:
    reference: str
    pads: list[_Pad]


@dataclass
class _Net:
    name: str


@dataclass
class _FakePCB:
    footprints: list[_Footprint]
    nets: dict = field(default_factory=lambda: {1: _Net("GND")})
    board_origin: tuple[float, float] = (0.0, 0.0)


# GND, three pad-bearing clusters:
#   comp 0  the In1.Cu plane, reached by U1.1          (owns the plane)
#   comp 1  C1.1 + C1.2 joined by a trace on F.Cu      (stranded)
#   comp 2  R1.1 alone                                 (stranded)
# plus a padless sliver KiCad's ratsnest ignores.
_PAD_GEOMS = {
    ("U1", "1"): box(10, 10, 11, 11),
    ("C1", "1"): box(20, 20, 21, 21),
    ("C1", "2"): box(22, 20, 23, 21),
    ("R1", "1"): box(40, 40, 41, 41),
}
_COMPS = [
    {"In1.Cu": box(0, 0, 100, 100), "F.Cu": box(10, 10, 11, 11)},
    {"F.Cu": box(20, 20, 23, 21)},
    {"F.Cu": box(40, 40, 41, 41)},
    {"In1.Cu": box(60, 60, 60.2, 60.2)},
]


@pytest.fixture
def fake_board(monkeypatch):
    import kicad_tools.router.link_router as lr
    import kicad_tools.schema.pcb as pcb_mod

    pcb = _FakePCB(
        footprints=[
            _Footprint("U1", [_Pad("1", "GND")]),
            _Footprint("C1", [_Pad("2", "GND"), _Pad("1", "GND")]),
            _Footprint("R1", [_Pad("1", "GND")]),
        ]
    )

    def terminal_for_pad(_pcb, ref, num):
        g = _PAD_GEOMS[(ref, num)]
        return lr.LinkTerminal(f"{ref}.{num}", (g.centroid.x, g.centroid.y), {"F.Cu": g})

    monkeypatch.setattr(pcb_mod.PCB, "load", staticmethod(lambda _p: pcb))
    monkeypatch.setattr(lr, "_build_model", lambda _pcb: object())
    monkeypatch.setattr(lr, "net_components", lambda _m, _n: list(_COMPS))
    monkeypatch.setattr(lr, "terminal_for_pad", terminal_for_pad)
    return pcb


def _closer_inputs(tmp_path: Path, links, monkeypatch) -> tuple[dict, list[str], list[str]]:
    """What the closer would weld and route for ``links`` (welds all fail)."""
    record: dict = {}

    def weld(self, _path, pads_by_net, _attempt):
        record["weld"] = {net: sorted(keys) for net, keys in pads_by_net.items()}
        return set()

    def route(self, _path, pending, _attempt):
        record["route"] = [lk.describe() for lk in pending]

    monkeypatch.setattr(PourLinkCloser, "_weld_pads", weld)
    monkeypatch.setattr(PourLinkCloser, "_route_links", route)
    closer = PourLinkCloser(via_size=0.6, via_drill=0.3, clearance=0.2, trace_width=0.2)
    attempt = closer(tmp_path / "b.kicad_pcb", links, frozenset())
    return record.get("weld", {}), record.get("route", []), attempt.notes


class TestClusterLinks:
    # Two different spanning trees KiCad could name over the same 3 clusters.
    NAMING_A = (
        (("Pad 2 [GND] of C1 on F.Cu", 22.5, 20.5), ("Pad 1 [GND] of U1 on F.Cu", 10.5, 10.5)),
        (("Pad 1 [GND] of R1 on F.Cu", 40.5, 40.5), ("Zone [GND] on In1.Cu, priority 0", 0, 0)),
    )
    NAMING_B = (
        (
            ("Track [GND] on F.Cu, length 1.0 mm", 21.0, 20.5),
            ("Pad 1 [GND] of R1 on F.Cu", 40.5, 40.5),
        ),
        (("Zone [GND] on B.Cu, priority 0", 0, 0), ("Pad 1 [GND] of R1 on F.Cu", 40.5, 40.5)),
    )

    def test_closer_input_is_independent_of_kicad_naming(self, tmp_path, fake_board, monkeypatch):
        weld_a, route_a, notes_a = _closer_inputs(tmp_path, _links(*self.NAMING_A), monkeypatch)
        weld_b, route_b, notes_b = _closer_inputs(tmp_path, _links(*self.NAMING_B), monkeypatch)
        assert (weld_a, route_a) == (weld_b, route_b)
        assert not notes_a and not notes_b
        # One canonical pad per stranded cluster (C1.1, not the C1.2 run A
        # named); the plane's own cluster (U1) is left alone.
        assert weld_a == {"GND": ["C1.1", "R1.1"]}
        assert len(route_a) == 2

    def test_count_disagreement_falls_back_to_kicads_links(self, tmp_path, fake_board, monkeypatch):
        """3 KiCad links = 4 clusters, but the model has 3: keep KiCad's, say so."""
        extra = (
            ("Pad 1 [GND] of U1 on F.Cu", 10.5, 10.5),
            ("Zone [GND] on In1.Cu, priority 0", 0, 0),
        )
        links = _links(*self.NAMING_A, extra)
        weld, _route, notes = _closer_inputs(tmp_path, links, monkeypatch)
        assert weld == {"GND": ["C1.2", "R1.1", "U1.1"]}
        assert any("closing KiCad's links as named" in n for n in notes)

    def test_canonical_clusters_can_be_disabled(self, tmp_path, fake_board, monkeypatch):
        def closer_with(flag: bool):
            record = {}
            monkeypatch.setattr(
                PourLinkCloser,
                "_weld_pads",
                lambda self, _p, pbn, _a: (
                    record.update(w={n: sorted(k) for n, k in pbn.items()}) or set()
                ),
            )
            monkeypatch.setattr(PourLinkCloser, "_route_links", lambda *a: None)
            PourLinkCloser(0.6, 0.3, 0.2, 0.2, canonical_clusters=flag)(
                tmp_path / "b.kicad_pcb", _links(*self.NAMING_A), frozenset()
            )
            return record["w"]

        assert closer_with(False) == {"GND": ["C1.2", "R1.1", "U1.1"]}
        assert closer_with(True) == {"GND": ["C1.1", "R1.1"]}


# ---------------------------------------------------------------------------
# Two-project union
# ---------------------------------------------------------------------------


class TestPerNetMerge:
    @staticmethod
    def _merge(primary, alternate):
        from kicad_tools.cli.route_cmd import _merge_unconnected_per_net

        def net_of(v):
            got = links_from_violations([v])
            return got[0].net if got else None

        return _merge_unconnected_per_net(primary, alternate, net_of)

    def test_differently_named_equal_counts_do_not_inflate(self):
        """The old raw-record union turned 4 links into up to 7 here."""
        a = [_violation(x, y) for x, y in _RUN_A]
        b = [_violation(x, y) for x, y in _RUN_B]
        merged = self._merge(a, b)
        counts = {}
        for lk in links_from_violations(merged):
            counts[lk.net] = counts.get(lk.net, 0) + 1
        assert counts == {"VCC": 3, "GND": 1}

    def test_counts_depend_only_on_per_net_counts(self):
        a = [_violation(x, y) for x, y in _RUN_A]
        b = [_violation(x, y) for x, y in _RUN_B]
        extra_gnd = _violation(
            ("Pad 2 [GND] of C11 on F.Cu", 131.275, 92.5), ("Zone [GND] on F.Cu", 109.0, 58.0)
        )
        merged = self._merge(a, b + [extra_gnd])
        nets = [lk.net for lk in links_from_violations(merged)]
        # GND: the alternate project strands one more cluster -> its 2 links win.
        assert nets.count("GND") == 2 and nets.count("VCC") == 3
        assert extra_gnd in merged

    def test_unresolvable_records_are_kept_from_primary_only(self):
        orphan = DRCViolation(
            type=ViolationType.UNCONNECTED_ITEMS,
            type_str="unconnected_items",
            severity=Severity.ERROR,
            message="Missing connection between items",
            locations=[Location(0, 0)],
            items=["Something odd"],
        )
        assert self._merge([orphan], [orphan, orphan]) == [orphan]


def test_endpoint_parse_round_trip_for_synthetic_cluster_links():
    """The synthetic pad end the cluster pass emits is a real pad end."""
    end = OracleEndpoint(
        description="Pad 1 [GND] of C1", kind="pad", net="GND", x=0, y=0, ref="C1", pad="1"
    )
    assert end.pad_key == "C1.1"
    assert OracleEndpoint.parse("Pad 1 [GND] of C1", 0, 0).pad_key == "C1.1"


# ---------------------------------------------------------------------------
# The refill must not read stale fills (KiCad-side net reassignment)
# ---------------------------------------------------------------------------

_BOARD = """(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew")
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "VCC")
\t(via
\t\t(at 5 5)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net 1)
\t\t(uuid "c65f8e7b-7772-56a6-a6d6-04ea410c369d")
\t)
\t(zone
\t\t(net 2)
\t\t(net_name "VCC")
\t\t(layer "In2.Cu")
\t\t(uuid "99b8f0a0-0000-0000-0000-000000000001")
\t\t(polygon
\t\t\t(pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))
\t\t)
\t\t(filled_polygon
\t\t\t(layer "In2.Cu")
\t\t\t(pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))
\t\t)
\t)
)
"""


class TestStaleFillStrip:
    """``_run_fill_zones_via_drc`` hands KiCad a board without stale fills.

    Measured (board 03, KiCad 10.0.1): a GND via placed after the last fill
    sat inside the stale VCC ``In2.Cu`` fill, and ``kicad-cli pcb drc
    --refill-zones --save-board`` saved it as ``(net "VCC")`` in 2 of 8 runs
    on one byte-identical file -- KiCad resolves the two-net cluster it loads
    in an arbitrary order.  Stripping the stale fills first: 8/8 identical,
    via kept on GND.
    """

    @staticmethod
    def _run(tmp_path, monkeypatch, *, refills: bool, report: bool, save: bool = True):
        import subprocess

        from kicad_tools.cli import runner

        board = tmp_path / "b.kicad_pcb"
        board.write_text(_BOARD)
        seen: dict = {}

        def fake_run(cmd, *a, **k):
            seen["board"] = Path(cmd[-1]).read_text()
            seen["cmd"] = cmd
            if report:
                Path(cmd[cmd.index("--output") + 1]).write_text('{"violations": []}')
            if save and "--save-board" in cmd:
                # KiCad rewrites the board with fresh fills (Issue #6023).
                Path(cmd[-1]).write_text(_BOARD)
            return subprocess.CompletedProcess(cmd, 5, "", "")

        monkeypatch.setattr(runner, "_kicad_drc_supports_refill", lambda _cli: refills)
        monkeypatch.setattr(runner.subprocess, "run", fake_run)
        result = runner._run_fill_zones_via_drc(board, None, Path("/fake/kicad-cli"))
        return board, seen, result

    def test_refill_sees_no_stale_fill(self, tmp_path, monkeypatch):
        board, seen, result = self._run(tmp_path, monkeypatch, refills=True, report=True)
        assert result.success
        assert "--refill-zones" in seen["cmd"]
        assert "filled_polygon" not in seen["board"]
        # The zone outline and the copper are untouched.
        assert "(polygon" in seen["board"] and "c65f8e7b" in seen["board"]

    def test_failed_fill_restores_the_original_board(self, tmp_path, monkeypatch):
        board, seen, result = self._run(tmp_path, monkeypatch, refills=True, report=False)
        assert not result.success
        assert "filled_polygon" not in seen["board"]
        assert board.read_text() == _BOARD

    def test_no_strip_when_kicad_cannot_save_a_refill(self, tmp_path, monkeypatch):
        """KiCad 8/9 DRC refills in memory only: stripping would ship no fill."""
        board, seen, _result = self._run(tmp_path, monkeypatch, refills=False, report=True)
        assert "filled_polygon" in seen["board"]
