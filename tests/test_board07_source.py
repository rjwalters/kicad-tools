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
    assert (
        json.loads((tmp_path / "sdram_constraints.json").read_text())["stackup"]
        == "JLC06161H-2116A"
    )
