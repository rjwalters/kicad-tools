"""silk_over_copper: a blind/buried via's mask aperture exists only where it reaches (Issue #9116).

Sibling of #9076 (drill path): ``_iter_via_apertures`` yielded an aperture for
every untented side regardless of ``via_type``/``layers``, so an untented
``F.Cu``--``In1.Cu`` blind via manufactured a back-side copper/mask opening
that does not exist, and ``silk_over_copper`` could flag back silk over it.
The gate reuses :func:`_via_silk_sides` (#9104) -- the single via-side
definition (#9054).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers.base import DesignRules  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.silkscreen import (  # noqa: E402
    _iter_via_apertures,
    check_silk_over_copper,
)

# Board-default untenting: every via contributes mask apertures unless the
# side gate (this issue) stops a side the via never reaches.
_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)
          (36 "B.SilkS" user) (37 "F.SilkS" user)
          (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0) (tenting (front no) (back no)))
  (net 0 "")
  (net 1 "N1")
"""


def _rules() -> DesignRules:
    return DesignRules(
        min_trace_width_mm=0.0,
        min_clearance_mm=0.0,
        min_via_drill_mm=0.0,
        min_via_diameter_mm=0.0,
        min_annular_ring_mm=0.0,
    )


def _typed_via(
    via_type: str | None,
    layers: tuple[str, ...],
    x: float = 40.0,
    y: float = 40.0,
) -> str:
    keyword = f"{via_type} " if via_type else ""
    ls = " ".join(f'"{layer}"' for layer in layers)
    return f'  (via {keyword}(at {x} {y}) (size 0.6) (drill 0.3) (layers {ls})(net 1 "N1"))\n'


def _silk_at(side: str, x: float = 40.0, y: float = 40.0) -> str:
    mirror = " (justify mirror)" if side == "B" else ""
    return (
        f'  (gr_text "SILK TEXT" (at {x} {y}) (layer "{side}.SilkS")'
        f" (effects (font (size 1 1) (thickness 0.15)){mirror}))\n"
    )


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_HEAD + body + ")\n")
    return path


def _copper_layers(path: Path) -> list[str]:
    res = check_silk_over_copper(PCB.load(path), _rules())
    assert all(v.rule_id == "silk_over_copper" for v in res.violations)
    assert all(v.items[1].startswith("via") for v in res.violations)
    return sorted(v.layer for v in res.violations)


@pytest.mark.parametrize("via_type", ["blind", "micro"])
def test_front_blind_via_flags_front_silk_only(tmp_path: Path, via_type: str) -> None:
    body = _silk_at("F") + _silk_at("B") + _typed_via(via_type, ("F.Cu", "In1.Cu"))
    path = _write(tmp_path, body)
    assert _copper_layers(path) == ["F.SilkS"]


def test_back_blind_via_flags_back_silk_only(tmp_path: Path) -> None:
    body = _silk_at("F") + _silk_at("B") + _typed_via("blind", ("In1.Cu", "B.Cu"))
    path = _write(tmp_path, body)
    assert _copper_layers(path) == ["B.SilkS"]


def test_buried_via_flags_neither_side(tmp_path: Path) -> None:
    body = _silk_at("F") + _silk_at("B") + _typed_via("buried", ("In1.Cu", "In2.Cu"))
    path = _write(tmp_path, body)
    assert _copper_layers(path) == []


def test_through_via_still_flags_both_sides(tmp_path: Path) -> None:
    body = _silk_at("F") + _silk_at("B") + _typed_via(None, ("F.Cu", "B.Cu"))
    path = _write(tmp_path, body)
    assert _copper_layers(path) == ["B.SilkS", "F.SilkS"]


def test_keywordless_via_with_blind_looking_layers_is_through(tmp_path: Path) -> None:
    # #6493: KiCad reads a keyword-less partial layer pair as a through via,
    # so the aperture exists on both sides whatever the pair says.
    body = _silk_at("F") + _silk_at("B") + _typed_via(None, ("F.Cu", "In1.Cu"))
    path = _write(tmp_path, body)
    assert _copper_layers(path) == ["B.SilkS", "F.SilkS"]


def test_iter_via_apertures_yields_reached_sides_only(tmp_path: Path) -> None:
    body = (
        _typed_via(None, ("F.Cu", "B.Cu"), x=20, y=20)
        + _typed_via("blind", ("F.Cu", "In1.Cu"), x=40, y=20)
        + _typed_via("blind", ("In1.Cu", "B.Cu"), x=60, y=20)
        + _typed_via("micro", ("F.Cu", "In1.Cu"), x=80, y=20)
        + _typed_via("buried", ("In1.Cu", "In2.Cu"), x=100, y=20)
    )
    path = _write(tmp_path, body)
    got: dict[str, set[str]] = {}
    for side, _geom, label in _iter_via_apertures(PCB.load(path), 0.0):
        got.setdefault(label, set()).add(side)
    assert got == {
        "via [N1] at (20.000, 20.000)": {"F", "B"},
        "via [N1] at (40.000, 20.000)": {"F"},
        "via [N1] at (60.000, 20.000)": {"B"},
        "via [N1] at (80.000, 20.000)": {"F"},
    }
