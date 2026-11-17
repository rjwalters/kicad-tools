"""The reviewed physical fixture must agree with independent circuit generation."""

import importlib.util
from pathlib import Path


def test_reviewed_physical_source_matches_fresh_circuit(tmp_path):
    root = Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test/real_design"
    spec = importlib.util.spec_from_file_location("board07_source", root / "build_source.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.build(tmp_path)
    assert result["electrical_identity_verified"]
    assert (result["parts"], result["bound_pads"]) == (43, 216)
    assert not result["manufacturing_ready"]
    import json

    rules = json.loads((tmp_path / "net_class_map.json").read_text())
    for name, rule in rules.items():
        assert 1 in rule["avoid_layers"] and 4 in rule["avoid_layers"]
        if rule.get("length_match_group"):
            assert rule["trace_width"] == 0.18
            # Issue #7423: the published map tunes the SDRAM groups at route time.
            assert rule["length_critical"] is True
    assert (
        json.loads((tmp_path / "sdram_constraints.json").read_text())["stackup"]
        == "JLC06161H-2116A"
    )


def _routing_fixture(end):
    return f"""(kicad_pcb
  (segment
    (start 0 0)
    (end {end})
    (width 0.18)
    (layer "F.Cu")
    (net 1)
    (uuid "11111111-1111-1111-1111-111111111111")
  )
)
"""


def test_reviewed_build_rejects_hash_bound_off_angle_copper(tmp_path, monkeypatch):
    """A matching manifest cannot bypass either source's angle policy."""
    import hashlib
    import json
    from types import SimpleNamespace

    import pytest

    root = Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test/real_design"
    spec = importlib.util.spec_from_file_location("board07_build_angles", root / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "authored-source"
    reviewed = tmp_path / "reviewed-routing"
    source.mkdir()
    reviewed.mkdir()
    (source / "manifest.json").write_text(json.dumps({"sha256": {}}))
    monkeypatch.setattr(module, "ROOT", tmp_path)

    def unexpected_validation(*args):
        pytest.fail("Off-angle copper reached electrical validation")

    for bad_source in ("authored", "routed"):
        candidate = _routing_fixture("1 2" if bad_source == "routed" else "1 1")
        (reviewed / "sdram_demo.kicad_pcb").write_text(candidate)
        (reviewed / "manifest.json").write_text(
            json.dumps(
                {
                    "authored_source_sha256": {},
                    "sha256": {
                        "sdram_demo.kicad_pcb": hashlib.sha256(candidate.encode()).hexdigest()
                    },
                }
            )
        )

        def build_source(output):
            output.mkdir()
            (output / "sdram_demo.kicad_pcb").write_text(
                _routing_fixture("1 2" if bad_source == "authored" else "1 1")
            )

        monkeypatch.setattr(
            module,
            "load_module",
            lambda name: (
                SimpleNamespace(build=build_source)
                if name == "build_source"
                else SimpleNamespace(check=unexpected_validation)
            ),
        )
        with pytest.raises(RuntimeError, match="off-angle copper"):
            module.build(tmp_path / f"output-{bad_source}")


ROOT07 = Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test/real_design"


def _load_record(name):
    spec = importlib.util.spec_from_file_location(f"board07_{name}", ROOT07 / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vias(path):
    import re

    return {
        (net, (float(x), float(y)))
        for x, y, net in re.findall(
            r'\(via\s+\(at ([-\d.]+) ([-\d.]+)\).*?\(net "([^"]*)"\)', path.read_text(), re.S
        )
    }


def test_class_clearance_repair_is_applied_and_bounded():
    """#6396: every listed via sits at its repaired position, moved <= 50 um.

    #9091 later moved two of these vias again; a via counts as in place when it
    sits where the later record put it.
    """
    import math

    module = _load_record("repair_class_clearance_6396")
    later = {
        (net, old): new for net, old, new, _ in _load_record("repair_copper_gap_9091").VIA_MOVES
    }
    for path in (module.AUTHORED, module.REVIEWED):
        vias = _vias(path)
        for net, old, new, authored in module.VIA_MOVES:
            if path == module.AUTHORED and not authored:
                continue
            assert math.dist(old, new) <= 0.05
            final = later.get((net, new), new)
            assert (net, final) in vias and (net, old) not in vias


def test_copper_gap_repair_is_applied_and_bounded():
    """#9091: both sources carry the repair, and it stays a small, on-grid edit."""
    import hashlib
    import math

    module = _load_record("repair_copper_gap_9091")
    for path in (module.AUTHORED, module.REVIEWED):
        vias = _vias(path)
        for net, old, new, authored in module.VIA_MOVES:
            if path == module.AUTHORED and not authored:
                continue
            assert math.dist(old, new) <= 0.09
            assert (net, new) in vias and (net, old) not in vias
        # Re-running the record on its own output must refuse, never re-apply.
        assert hashlib.sha256(path.read_bytes()).hexdigest() != module.PRE_REPAIR_SHA256[path]
    for _net, _layer, points, _ in module.TROMBONES:
        for (x1, y1), (x2, y2) in zip(points, points[1:], strict=False):
            dx, dy = round(abs(x2 - x1), 4), round(abs(y2 - y1), 4)
            assert dx == 0 or dy == 0 or dx == dy


def test_copper_gap_repair_keeps_byte1_lengths():
    """#9091: the trombones restore exactly the loop length they replace."""
    from kicad_tools.analysis.trace_length import TraceLengthAnalyzer
    from kicad_tools.schema.pcb import PCB

    module = _load_record("repair_copper_gap_9091")
    # Copper lengths of the reviewed artifact before the repair (main 89c2ee058),
    # vias excluded: the repair moves no via on these nets.
    reviewed = {"DQ13": 73.6572, "DQ15": 73.6558, "UDQM": 73.6411}
    board = PCB.load(module.REVIEWED)
    analyzer = TraceLengthAnalyzer()
    for net, before in reviewed.items():
        assert abs(analyzer.analyze_net(board, net).total_length_mm - before) < 0.002, net
