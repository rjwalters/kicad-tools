"""Run with KiCad 10.0.5's pcbnew Python to regenerate oracle.json.

Each pattern has an isolated Probe class: membership cannot be masked by
another assignment or by precedence. This script does not load/save projects.
"""

import json

import pcbnew

version = pcbnew.GetBuildVersion()
if version != "10.0.5":
    raise SystemExit(f"Expected KiCad 10.0.5, got {version}")
patterns = [
    "",
    "USB",
    "USB_*",
    "USB?",
    "USB*",
    "*",
    "?",
    "*USB*",
    "a*b?",
    "USB_D",
    "usb_*",
    "A-B",
    "A B",
    "USB.1",
    r"USB\*",
    "USB[0-9]",
    "^USB$",
    "/USB/",
    "a**",
]
nets = [
    "é",
    "😀",
    "\n\n",
    "USB\nX",
    "Net-(U1-Pad1)",
    "/sheet/USB",
    "USB_é",
    "USB_😀",
    "USB_\n",
    "USB\n",
    "\n",
    "",
    "USB",
    "USB_D",
    "USB__",
    "USB_",
    "usb_D",
    "XUSB",
    "US",
    "USBB",
    "USB1",
    "USB.1",
    "USB*",
    r"USB\x",
    "USB[0-9]",
    "A-B",
    "A B",
    "ab",
    "aaab",
    "a",
    "b",
    "XYZ",
    "/USB/",
]
cases = []
for pattern in patterns:
    settings = pcbnew.NET_SETTINGS(None, "net_settings")
    settings.SetNetclass("Probe", pcbnew.NETCLASS("Probe"))
    settings.SetNetclassPatternAssignment(pattern, "Probe")
    for net in nets:
        cases.append(
            {
                "pattern": pattern,
                "net": net,
                "matches": settings.GetEffectiveNetClass(net).GetName() == "Probe",
            }
        )
print(json.dumps({"kicad_version": version, "source_tag": "10.0.5", "cases": cases}, indent=2))
