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
