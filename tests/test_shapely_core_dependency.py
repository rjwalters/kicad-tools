"""Guards for shapely being a load-bearing core dependency (issue #3824).

shapely backs the two correctness-critical copper paths -- zone-fill
clearance carving and the zone-vs-copper clearance DRC rules.  Historically
it was only an optional extra, which produced two dangerous failure modes
when absent:

1. SILENT BAD FILLS -- ``apply_foreign_pad_clearance`` /
   ``force_solid_on_isolated_island_pads`` returned ``0`` (a no-op) when
   shapely was missing, so the pipeline reported a clean fill while real
   ``clearance_pad_zone`` shorts remained in the copper.
2. UNGUARDED CRASH -- the clearance DRC rules imported shapely with bare
   ``from shapely import ...`` and died with a raw ``ModuleNotFoundError``.

These tests assert the hardened behavior: a shapely-absent environment now
fails **loud** with an actionable install message rather than silently
returning a non-clearance-correct fill, and the DRC rule raises the same
actionable error instead of a raw ``ModuleNotFoundError``.  They also pin
the packaging contract (shapely is a core dependency).
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from typing import Any

import pytest

from kicad_tools import _shapely as shapely_guard
from kicad_tools.sexp import parse_string

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[import-not-found]


# Minimal board with a VCC fill overlapping a foreign-net (GND) pad -- the
# exact shape that requires carving.  Reused from the fill-clearance suite.
_BOARD = """
(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 1 "VCC")
  (net 3 "GND")
  (footprint "lib:foreign"
    (layer "F.Cu")
    (at 5 5)
    (pad "1" thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 3 "GND"))
  )
  (zone
    (net "VCC")
    (layer "F.Cu")
    (uuid "test-zone")
    (hatch edge 0.5)
    (connect_pads (clearance 0.3))
    (min_thickness 0.25)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.4))
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
    (filled_polygon
      (layer "F.Cu")
      (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20))
    )
  )
)
"""


@pytest.fixture
def shapely_absent(monkeypatch: pytest.MonkeyPatch):
    """Simulate a broken/partial install where shapely cannot be imported.

    Blocks ``import shapely`` (and submodules) and resets the cached probe
    in :mod:`kicad_tools._shapely` so the guard re-evaluates as unavailable.
    """
    # Drop any already-imported shapely modules so a fresh import is forced.
    for name in list(sys.modules):
        if name == "shapely" or name.startswith("shapely."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "shapely" or name.startswith("shapely."):
            raise ModuleNotFoundError("No module named 'shapely'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    # Reset the cached availability so has_shapely() re-probes under the block.
    monkeypatch.setattr(shapely_guard, "_SHAPELY_AVAILABLE", None, raising=False)
    yield


# ---------------------------------------------------------------------------
# Shared guard helper
# ---------------------------------------------------------------------------


def test_require_shapely_raises_actionable_when_absent(shapely_absent):
    """``require_shapely`` raises with the friendly install hint, not bare error."""
    with pytest.raises(ModuleNotFoundError) as excinfo:
        shapely_guard.require_shapely("unit-test feature")
    msg = str(excinfo.value)
    assert "unit-test feature" in msg
    assert "kicad-tools[geometry]" in msg
    # Sanity: this is NOT the opaque "No module named 'shapely'" message.
    assert msg != "No module named 'shapely'"


def test_has_shapely_true_when_present():
    pytest.importorskip("shapely")
    # Force a clean re-probe (cache may be poisoned by another test).
    shapely_guard._SHAPELY_AVAILABLE = None
    assert shapely_guard.has_shapely() is True


# ---------------------------------------------------------------------------
# Silent-bad-fill path: must fail LOUD, never silently return 0
# ---------------------------------------------------------------------------


def test_apply_foreign_pad_clearance_fails_loud_without_shapely(shapely_absent):
    """The carving path must raise (not silently return 0) when shapely absent."""
    from kicad_tools.zones.fill_clearance import apply_foreign_pad_clearance

    doc = parse_string(_BOARD)
    with pytest.raises(ModuleNotFoundError) as excinfo:
        apply_foreign_pad_clearance(doc)
    assert "kicad-tools[geometry]" in str(excinfo.value)


def test_force_solid_isolated_islands_fails_loud_without_shapely(shapely_absent):
    """Island remediation must raise (not silently return 0) when shapely absent."""
    from kicad_tools.zones.fill_clearance import force_solid_on_isolated_island_pads

    doc = parse_string(_BOARD)
    with pytest.raises(ModuleNotFoundError) as excinfo:
        force_solid_on_isolated_island_pads(doc, {"test-zone"})
    assert "kicad-tools[geometry]" in str(excinfo.value)


def test_force_solid_empty_zoneset_is_still_noop(shapely_absent):
    """An empty zone set short-circuits before the guard (genuinely nothing to do)."""
    from kicad_tools.zones.fill_clearance import force_solid_on_isolated_island_pads

    doc = parse_string(_BOARD)
    # No zones requested -> legitimately 0, never touches shapely.
    assert force_solid_on_isolated_island_pads(doc, set()) == 0


# ---------------------------------------------------------------------------
# DRC clearance rules: degrade with actionable message, not raw ModuleNotFound
# ---------------------------------------------------------------------------


def test_segment_zone_clearance_rule_actionable_without_shapely(shapely_absent):
    """The DRC rule degrades with the actionable hint, not a raw ModuleNotFoundError.

    ``require_shapely`` fires at the very top of ``check()`` before any PCB
    geometry is touched, so a bare MagicMock PCB is sufficient -- the point
    is that the rule never reaches a bare ``import shapely`` that would
    surface the opaque "No module named 'shapely'" message.
    """
    from unittest.mock import MagicMock

    from kicad_tools.validate.rules.clearance import SegmentZoneClearanceRule

    rules = MagicMock()
    rules.min_clearance_mm = 0.127
    with pytest.raises(ModuleNotFoundError) as excinfo:
        SegmentZoneClearanceRule().check(MagicMock(), rules)
    msg = str(excinfo.value)
    assert "kicad-tools[geometry]" in msg
    assert msg != "No module named 'shapely'"


def test_via_zone_clearance_rule_actionable_without_shapely(shapely_absent):
    from unittest.mock import MagicMock

    from kicad_tools.validate.rules.clearance import ViaZoneClearanceRule

    rules = MagicMock()
    rules.min_clearance_mm = 0.127
    with pytest.raises(ModuleNotFoundError) as excinfo:
        ViaZoneClearanceRule().check(MagicMock(), rules)
    msg = str(excinfo.value)
    assert "kicad-tools[geometry]" in msg
    assert msg != "No module named 'shapely'"


# ---------------------------------------------------------------------------
# Packaging contract: shapely is a CORE dependency (Option A)
# ---------------------------------------------------------------------------


def _load_pyproject() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    with open(root / "pyproject.toml", "rb") as fh:
        data: dict[str, Any] = tomllib.load(fh)
    return data


def test_shapely_is_core_dependency():
    """shapely>=2.0 must be in [project] dependencies (Option A)."""
    data = _load_pyproject()
    core = data["project"]["dependencies"]
    shapely_pins = [d for d in core if d.replace(" ", "").lower().startswith("shapely")]
    assert shapely_pins, f"shapely missing from core dependencies: {core}"
    assert any(">=2.0" in pin for pin in shapely_pins), shapely_pins


def test_shapely_present_in_all_extra():
    """shapely is also listed explicitly in the [all] extra."""
    data = _load_pyproject()
    all_extra = data["project"]["optional-dependencies"]["all"]
    assert any(d.lower().startswith("shapely") for d in all_extra), all_extra


def test_shapely_not_duplicated_in_dev_extra():
    """[dev] no longer pins shapely (it is core now); no contradictory pins."""
    data = _load_pyproject()
    dev_extra = data["project"]["optional-dependencies"]["dev"]
    assert not any(d.lower().startswith("shapely") for d in dev_extra), dev_extra
