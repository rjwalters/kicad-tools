"""Board 03 route flows must carry the committed net-class map (Issue #6829).

A map-less board 03 route leaves ``pair_protocol`` null on the USB nets, so the
report and ``kct check`` hold USB_D to the generic 0.5 mm skew default instead
of its declared USB2_FS limit. The generic fallback is deliberate (#6443);
the fix is that no in-repo flow routes board 03 without the committed map.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RESEARCH = REPO_ROOT / "scripts" / "research"
MAP_REL = "boards/03-usb-joystick/output/net_class_map.json"


def _load(name: str):
    path = RESEARCH / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_6829", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _map_arg(args: list[str] | tuple[str, ...]) -> str:
    assert "--net-class-map" in args, f"board 03 route args lack --net-class-map: {args}"
    return args[list(args).index("--net-class-map") + 1]


def test_committed_map_declares_usb2_fs() -> None:
    data = json.loads((REPO_ROOT / MAP_REL).read_text())
    assert "USB2_FS" in json.dumps(data)


def test_expand_route_board03_args_carry_map() -> None:
    ere = _load("expand_route_experiment")
    args = ere.net_class_map_args(ere.BOARDS["03"])
    assert Path(_map_arg(args)) == REPO_ROOT / MAP_REL


def test_expand_route_missing_map_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ere = _load("expand_route_experiment")
    monkeypatch.setattr(ere, "REPO", tmp_path)
    with pytest.raises(FileNotFoundError, match="net-class map not found"):
        ere.net_class_map_args(ere.BOARDS["03"])


def test_boards_without_map_get_no_flag() -> None:
    ere = _load("expand_route_experiment")
    assert ere.net_class_map_args(ere.BOARDS["00"]) == []


@pytest.mark.parametrize(
    "name", ["net_order_experiment", "ripup_experiment", "escape_flow_experiment"]
)
def test_experiment_harness_board03_kct_args_carry_map(name: str) -> None:
    mod = _load(name)
    assert Path(_map_arg(mod.BOARDS["03"].kct_args)) == REPO_ROOT / MAP_REL


def test_determinism_smoke_board03_flags_carry_map() -> None:
    text = (REPO_ROOT / "scripts" / "ci" / "board_route_determinism_smoke.sh").read_text()
    block = re.search(r"^\s*03\)(.*?)^\s*;;", text, re.S | re.M)
    assert block is not None
    assert f"--net-class-map {MAP_REL}" in block.group(1)
