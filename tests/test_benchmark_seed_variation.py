"""Tests for ``benchmarks/external/seed_variation.py`` (issue #6332). No routing."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "benchmarks" / "external" / "seed_variation.py"

BOARD = """(kicad_pcb (version 20240108)
  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1) (uuid "aaa"))
  (via (at 1 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "bbb"))
)
"""


@pytest.fixture(scope="module")
def sv():
    spec = importlib.util.spec_from_file_location("seed_variation", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fingerprint_ignores_uuids_and_order(sv, tmp_path):
    a, b = tmp_path / "a.kicad_pcb", tmp_path / "b.kicad_pcb"
    a.write_text(BOARD)
    lines = BOARD.replace('"aaa"', '"zzz"').splitlines()
    b.write_text("\n".join([lines[0], lines[2], lines[1], *lines[3:]]))
    fa, fb = sv.geometry_fingerprint(a), sv.geometry_fingerprint(b)
    assert fa == fb and fa["segments"] == 1 and fa["vias"] == 1


def _route_factory(geometry_for):
    def route(argv):
        seed = int(argv[argv.index("--seed") + 1])
        out = Path(argv[argv.index("-o") + 1])
        out.write_text(BOARD.replace("(end 1 0)", f"(end {geometry_for(seed, out)} 0)"))
        return 0

    return route


def test_verdicts(sv, tmp_path):
    board = tmp_path / "in.kicad_pcb"
    board.write_text(BOARD)
    same = sv.measure([board], [1, 2, 3], timeout=None, route=_route_factory(lambda s, o: 1))
    assert same["verdict"] == "deterministic"
    varied = sv.measure([board], [1, 2, 3], timeout=None, route=_route_factory(lambda s, o: s))
    assert varied["verdict"] == "seed-dependent"
    assert varied["boards"][0]["distinct_geometries_across_seeds"] == 3

    def flaky(seed, out):
        return 9 if "repeat" in out.name else 1

    assert sv.measure([board], [1, 2], timeout=5, route=_route_factory(flaky))["verdict"] == (
        "nondeterministic"
    )


def test_crash_is_recorded_and_inconclusive(sv, tmp_path):
    board = tmp_path / "in.kicad_pcb"
    board.write_text(BOARD)

    def boom(argv):
        raise RuntimeError("kaput")

    res = sv.measure([board], [1, 2], timeout=None, route=boom)
    assert res["verdict"] == "inconclusive"
    assert res["boards"][0]["runs"][0]["error"] == "RuntimeError: kaput"
