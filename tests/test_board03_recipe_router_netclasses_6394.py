"""Board 03's recipe declares the router's net classes itself (Issue #6394).

``generate_design.py`` replays ``routing-plan.json`` instead of calling
``kct route``, so before #6394 a fresh regen wrote sidecars that declared only
``Default``; the classes reached the committed sidecars only through
``scripts/boards/declare_router_netclasses_6390.py``.  The recipe's
pre-fill ``apply_manufacturing_profile`` now passes the map ``kct route``
routed under as ``router_net_classes=``.  These tests strip the committed
``.kicad_pro`` back to ``Default`` and check that the recipe alone restores
the same declaration -- including the classifier's ``Digital``/``Clock``
classes for nets the sidecar map does not name, so no class is loosened.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "03-usb-joystick"
OUTPUT = BOARD_DIR / "output"
STEM = "usb_joystick_routed"


def _load_recipe():
    if str(BOARD_DIR) not in sys.path:
        sys.path.insert(0, str(BOARD_DIR))
    spec = importlib.util.spec_from_file_location(
        "board03_generate_design_6394", BOARD_DIR / "generate_design.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage(tmp_path: Path, *, with_map: bool = True) -> Path:
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copy(OUTPUT / f"{STEM}{suffix}", tmp_path / f"{STEM}{suffix}")
    shutil.copy(OUTPUT / "fabrication_overrides.json", tmp_path / "fabrication_overrides.json")
    if with_map:
        shutil.copy(OUTPUT / "net_class_map.json", tmp_path / "net_class_map.json")
    pro = tmp_path / f"{STEM}.kicad_pro"
    data = json.loads(pro.read_text())
    settings = data["net_settings"]
    settings["classes"] = [c for c in settings["classes"] if c["name"] == "Default"]
    settings["netclass_assignments"] = {}
    data.get("meta", {}).pop("kct_router_netclasses", None)
    pro.write_text(json.dumps(data, indent=2))
    return tmp_path / f"{STEM}.kicad_pcb"


def _net_settings(pcb: Path) -> dict:
    return json.loads(pcb.with_suffix(".kicad_pro").read_text())["net_settings"]


def test_fresh_regen_declares_router_classes(tmp_path: Path) -> None:
    pcb = _stage(tmp_path)
    _load_recipe().apply_manufacturing_profile(pcb)

    settings = _net_settings(pcb)
    classes = {c["name"]: c for c in settings["classes"]}
    assignments = settings["netclass_assignments"]
    assert "Power" in classes
    assert classes["Power"]["clearance"] == pytest.approx(0.2)
    for net in ("GND", "VCC", "VBUS"):
        assert assignments.get(net) == "Power", net
    for net in ("USB_D+", "USB_D-", "USB_MCU_D+", "USB_MCU_D-"):
        assert assignments.get(net) == "HighSpeed", net
    dru = pcb.with_suffix(".kicad_dru").read_text()
    assert 'rule "Reviewed clearance - Power"' in dru


def test_fresh_regen_matches_the_committed_declaration(tmp_path: Path) -> None:
    """Classifier classes survive: nothing the committed sidecars hold is loosened."""
    pcb = _stage(tmp_path)
    _load_recipe().apply_manufacturing_profile(pcb)

    committed = json.loads((OUTPUT / f"{STEM}.kicad_pro").read_text())["net_settings"]
    fresh = _net_settings(pcb)
    assert fresh["netclass_assignments"] == committed["netclass_assignments"]
    assert {c["name"]: c["clearance"] for c in fresh["classes"]} == {
        c["name"]: c["clearance"] for c in committed["classes"]
    }
    # A net the sidecar map does not name keeps its classifier class.
    assert fresh["netclass_assignments"]["BTN1"] == "Digital"


def test_no_sidecar_map_declares_nothing(tmp_path: Path) -> None:
    recipe = _load_recipe()
    pcb = _stage(tmp_path, with_map=False)
    assert recipe.router_net_class_map(pcb) is None
