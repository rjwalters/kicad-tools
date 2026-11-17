"""Cross-library 3D-model substitutions for footprints with no direct match.

Some board generators emit synthetic or generic footprint lib ids that do
**not** correspond to any installed KiCad ``.kicad_mod`` — and, unlike the
same-library variant fallback in ``models3d._find_variant_mod``, the visual
equivalent lives in a *different* library under a *different* name.  Two
naming-convention gaps drive this:

* **Renamed libraries.**  ``Connector_FFC`` was renamed to
  ``Connector_FFC-FPC`` upstream; the generic ``FFC_4P_0.5mm`` /
  ``FFC_6P_1.0mm`` names never shipped as real footprints.
* **Vendor-suffixed names only.**  ``Connector_USB`` and
  ``Connector_Video`` ship only vendor-specific names
  (``USB_C_Receptacle_GCT_...``, ``HDMI_A_Amphenol_...``), so a generic
  ``USB_C_Receptacle_USB2.0`` / ``HDMI_A_Receptacle`` lib id resolves to
  nothing even though a body-compatible part is installed.
* **Near-neighbour JEDEC package.**  ``BGA-49_5.0x5.0mm_Layout7x7_P0.5mm``
  has no exact match, but ``VFBGA-49_5.0x5.0mm_Layout7x7_P0.65mm`` is the
  same ball count and outline — close enough for a render body.

This is an **explicit, curated** ``lib_id -> lib_id`` table, not a fuzzy
search: each entry is a hand-verified visual equivalent whose installed
``.kicad_mod`` carries a ``(model ...)`` node.  The resolver consults it
only after both exact-match and same-library variant matching have failed,
so it can never redirect an already-resolvable footprint.

The substituted body is *render metadata only* — no copper, pad, or DRC
geometry is affected (the patch stays a pure ``(model ...)`` insertion).
The substitute is deliberately chosen for the right *outline/pin family*,
not pin-for-pin electrical identity.

Footprints with **no** installed or open substitute at all (e.g.
``Module:Joystick_Analog``, ``Connector_PCIE:PCIE_Mini_Edge``) are
intentionally *absent* from this table: they need a vendor-sourced STEP
file that must be collected by a human/operator, and are tracked in a
separate follow-up issue.
"""

from __future__ import annotations

__all__ = ["MODEL_SUBSTITUTIONS", "substitute_lib_id"]


# Curated generic/synthetic lib id -> installed body-compatible lib id.
# Every value is verified to exist in the standard KiCad libraries with a
# (model ...) node at authoring time (see issue #4014).
MODEL_SUBSTITUTIONS: dict[str, str] = {
    # Connector_FFC was renamed Connector_FFC-FPC; pick the pin-count- and
    # pitch-matched vendor part.
    "Connector_FFC:FFC_4P_0.5mm": (
        "Connector_FFC-FPC:Amphenol_F32Q-1A7x1-11004_1x04-1MP_P0.5mm_Horizontal"
    ),
    "Connector_FFC:FFC_6P_1.0mm": ("Connector_FFC-FPC:TE_84952-6_1x06-1MP_P1.0mm_Horizontal"),
    # Connector_USB ships only vendor-suffixed USB-C names; this GCT part is
    # already the body board 03 resolves against for the same lib id.
    "Connector_USB:USB_C_Receptacle_USB2.0": (
        "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal"
    ),
    # Connector_Video ships only vendor-suffixed HDMI names.
    "Connector_Video:HDMI_A_Receptacle": (
        "Connector_Video:HDMI_A_Amphenol_10029449-x01xLF_Horizontal"
    ),
    # Same 49-ball 5.0x5.0 7x7 outline; nearest installed pitch (0.65mm).
    "Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm": (
        "Package_BGA:VFBGA-49_5.0x5.0mm_Layout7x7_P0.65mm"
    ),
}


def substitute_lib_id(lib_id: str) -> str | None:
    """Return the substitute lib id for *lib_id*, or ``None`` if none exists.

    Consulted by the model resolver only after exact-match and same-library
    variant matching have both failed.
    """
    return MODEL_SUBSTITUTIONS.get(lib_id)
