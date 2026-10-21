"""Generate and attach project-local 3D bodies for footprints with no stock body (Issue #7685).

Four demo-board footprints reference a STEP file that exists in neither the
KiCad 10 libraries nor upstream ``kicad-packages3D`` (or have no model at all):

* board 03 ``J2``  ``Connector_JST:JST_PH_S5B-PH-SM4-TB_1x05-1MP_P2.00mm_Horizontal``
* board 09 ``J1``  ``Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12``
* board 09 ``RSH1`` ``Resistor_SMD:R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm``
* board 09 ``L1``  ``board09:SRP7050TA``

Source tier (ii) of ``docs/research/3d-model-coverage.md``: a body *we* generate
(box-and-terminal VRML, written below from nominal datasheet envelopes, so the
files are ours to commit; no vendor or EasyEDA geometry is used).  Each body is
authored directly in its footprint's local frame, so the ``(model ...)`` node
carries an identity offset/rotation.

The script writes ``<board>/output/3dshapes/*.wrl`` plus the
``local_models.json`` manifest (``lib_id -> file``) and attaches them with
``kicad_tools.pcb.models3d.attach_local_models``, which ``ensure_models`` and the
board generators also call, so a regeneration keeps the bodies.  Run from the repository root::

    uv run python scripts/boards/generate_local_3d_bodies_7685.py

``${KIPRJMOD}`` is the directory of the ``.kicad_pcb``; the readiness
fill-saved/refilled copies carry no ``3dshapes`` and are produced by
``kct readiness``, which only reads copper.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.pcb.models3d import LOCAL_MODELS_MANIFEST, attach_local_models

REPO = Path(__file__).resolve().parents[2]

# KiCad reads a VRML unit as 2.54 mm (0.1 in); author in mm and divide.
_UNIT_MM = 2.54

Box = tuple[float, float, float, float, float, float, tuple[float, float, float]]
# (x0, x1, y0, y1, z0, z1, rgb) in footprint-local mm; +y is board-down.


@dataclass
class Body:
    reference: str
    lib_id: str
    filename: str
    description: str
    boxes: list[Box] = field(default_factory=list)


_BLACK = (0.10, 0.10, 0.11)
_METAL = (0.62, 0.63, 0.66)
_WHITE = (0.93, 0.92, 0.86)
_GREY = (0.35, 0.35, 0.38)
_FERRITE = (0.16, 0.16, 0.18)
_ALUMINA = (0.80, 0.78, 0.72)

BODIES: dict[str, list[Body]] = {
    "boards/03-usb-joystick/output": [
        Body(
            "J2",
            "Connector_JST:JST_PH_S5B-PH-SM4-TB_1x05-1MP_P2.00mm_Horizontal",
            "JST_PH_S5B-PH-SM4-TB.wrl",
            "JST PH S5B-PH-SM4-TB side-entry 5-pos shrouded header, nominal envelope",
            [
                # housing: 12.0 wide, mating opening toward +y, 6.0 tall
                (-6.0, 6.0, -0.7, 4.8, 0.0, 6.0, _WHITE),
                # hold-down tabs on the mounting pads
                (-7.1, -5.6, 1.2, 4.6, 0.0, 0.2, _METAL),
                (5.6, 7.1, 1.2, 4.6, 0.0, 0.2, _METAL),
            ],
        ),
    ],
    "boards/09-usbc-pd-power/output": [
        Body(
            "J1",
            "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
            "USB_C_Receptacle_HRO_TYPE-C-31-M-12.wrl",
            "HRO TYPE-C-31-M-12 USB-C receptacle, nominal 8.94 x 7.35 x 3.26 mm shell",
            [
                (-4.47, 4.47, -4.8, 2.55, 0.0, 3.26, _METAL),
                # recessed mating tongue cavity read as a dark insert
                (-3.6, 3.6, 2.0, 2.56, 0.5, 2.76, _BLACK),
            ],
        ),
        Body(
            "RSH1",
            "Resistor_SMD:R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm",
            "R_Shunt_Vishay_WSK2512.wrl",
            "Vishay WSK2512 4-terminal current-sense resistor, nominal 6.35 x 3.2 mm",
            [
                (-2.2, 2.2, -1.6, 1.6, 0.0, 0.7, _ALUMINA),
                (-3.175, -2.2, -1.6, 1.6, 0.0, 0.9, _METAL),
                (2.2, 3.175, -1.6, 1.6, 0.0, 0.9, _METAL),
            ],
        ),
        Body(
            "L1",
            "board09:SRP7050TA",
            "SRP7050TA.wrl",
            "Bourns SRP7050TA shielded power inductor, nominal 7.0 x 7.0 x 5.0 mm",
            [
                (-3.5, 3.5, -3.5, 3.5, 0.3, 5.0, _FERRITE),
                (-3.5, -2.2, -3.0, 3.0, 0.0, 0.3, _METAL),
                (2.2, 3.5, -3.0, 3.0, 0.0, 0.3, _METAL),
            ],
        ),
    ],
}

#: PCBs (relative to the repo root) that receive the model nodes.
TARGET_PCBS: dict[str, list[str]] = {
    "boards/03-usb-joystick/output": [
        "boards/03-usb-joystick/output/usb_joystick.kicad_pcb",
        "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb",
    ],
    "boards/09-usbc-pd-power/output": [
        "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb",
    ],
}


def _fmt(v: float) -> str:
    return f"{v:.5f}".rstrip("0").rstrip(".") or "0"


def render_wrl(body: Body) -> str:
    """VRML 2.0 text; footprint +y (down) maps to model -y (KiCad's Y-up frame)."""
    out = [
        "#VRML V2.0 utf8",
        f"# {body.description}",
        "# Generated by scripts/boards/generate_local_3d_bodies_7685.py (Issue #7685).",
        "# Original work of the kicad-tools project; nominal envelope, not vendor geometry.",
    ]
    for x0, x1, y0, y1, z0, z1, (r, g, b) in body.boxes:
        cx, cy, cz = (x0 + x1) / 2, -(y0 + y1) / 2, (z0 + z1) / 2
        sx, sy, sz = x1 - x0, y1 - y0, z1 - z0
        out += [
            "Transform {",
            f"  translation {_fmt(cx / _UNIT_MM)} {_fmt(cy / _UNIT_MM)} {_fmt(cz / _UNIT_MM)}",
            "  children Shape {",
            f"    appearance Appearance {{ material Material {{ diffuseColor {r} {g} {b} "
            "specularColor 0.3 0.3 0.3 shininess 0.4 } }",
            f"    geometry Box {{ size {_fmt(sx / _UNIT_MM)} {_fmt(sy / _UNIT_MM)} "
            f"{_fmt(sz / _UNIT_MM)} }}",
            "  }",
            "}",
        ]
    return "\n".join(out) + "\n"


def main() -> int:
    for outdir, bodies in BODIES.items():
        shapes = REPO / outdir / "3dshapes"
        shapes.mkdir(parents=True, exist_ok=True)
        for body in bodies:
            (shapes / body.filename).write_text(render_wrl(body))
            print(f"wrote {shapes.relative_to(REPO) / body.filename}")
        (shapes / LOCAL_MODELS_MANIFEST.split("/")[-1]).write_text(
            json.dumps({b.lib_id: b.filename for b in bodies}, indent=2) + "\n"
        )
        for rel in TARGET_PCBS[outdir]:
            path = REPO / rel
            changed = attach_local_models(path)
            print(f"{rel}: attached {changed or 'nothing (already present)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
