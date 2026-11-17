"""Datasheet and native-serialization regressions for the assembled LVDS demo."""

import importlib.util
from pathlib import Path

from kicad_tools.sexp import parse_file


def _generator():
    path = (
        Path(__file__).resolve().parents[1] / "boards/06-diffpair-test/assembled-demo/generate.py"
    )
    spec = importlib.util.spec_from_file_location("lvds_demo_generate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ti_soic_driver_receiver_have_different_power_pinouts():
    parts = {p["ref"]: p for p in _generator().components()}
    # TI SLLS373M tables5-1/5-2: the receiver is not pin-compatible with TX.
    for odd in (1, 3, 5, 7):
        tx, rx = parts[f"U{odd}"], parts[f"U{odd + 1}"]
        assert tx["pins"]["1"] == rx["pins"]["8"] == "+3V3"
        assert tx["pins"]["4"] == rx["pins"]["5"] == "GND"
        assert tx["pins"]["7"] == rx["pins"]["2"]
        assert tx["pins"]["8"] == rx["pins"]["1"]
        assert all(tx["pins"][p] is None for p in ("3", "5", "6"))
        assert all(rx["pins"][p] is None for p in ("3", "4", "6"))


def test_each_receiver_has_one_100_ohm_termination_and_local_bypass():
    parts = {p["ref"]: p for p in _generator().components()}
    for channel in range(1, 5):
        resistor = parts[f"R{channel}"]
        assert resistor["value"] == "100"
        assert set(resistor["pins"].values()) == {f"LVDS{channel}_P", f"LVDS{channel}_N"}
        for first in (4 * channel - 3, 4 * channel - 1):
            assert parts[f"C{first}"]["value"] == "1nF"
            assert parts[f"C{first + 1}"]["value"] == "100nF"


def test_footprint_placement_precedes_pads_for_native_kicad(tmp_path):
    generator = _generator()
    pcb = parse_file(generator.make_pcb(tmp_path, generator.components()))
    for fp in pcb.find_children("footprint"):
        tags = [c.name for c in fp.children]
        assert tags.index("at") < tags.index("pad")
        # All electrical pad numbers have an explicit reviewed circuit entry.
        ref = next(
            p.get_string(1) for p in fp.find_children("property") if p.get_string(0) == "Reference"
        )
        part = next(p for p in generator.components() if p["ref"] == ref)
        assert {p.get_string(0) for p in fp.find_children("pad")} == set(part["pins"])


def test_generator_leaves_routed_project_to_the_router(tmp_path, monkeypatch):
    """Issue #6258: ``kct route`` derives ``*_routed.kicad_pro`` from the source.

    A generator-authored routed project is not a copy of the source (its own
    ``meta.filename``), so the router refused it as a DRC sidecar conflict.
    """
    generator = _generator()
    stale = tmp_path / "diffpair_test_routed.kicad_pro"
    stale.write_text("{}")
    monkeypatch.setattr("sys.argv", ["generate.py", str(tmp_path)])
    generator.main()
    assert (tmp_path / "diffpair_test.kicad_pro").is_file()
    assert not stale.exists()
