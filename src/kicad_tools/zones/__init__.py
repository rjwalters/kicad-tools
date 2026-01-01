"""
Zone generation module for KiCad PCB copper pours.

Provides high-level API for generating copper pour zones on PCBs:
- Ground planes (GND)
- Power planes (+3.3V, +5V, VCC, etc.)
- Custom zones with arbitrary nets

Example::

    from kicad_tools.zones import ZoneGenerator

    gen = ZoneGenerator.from_pcb("board.kicad_pcb")

    # Add ground plane on bottom layer
    gen.add_zone(
        net="GND",
        layer="B.Cu",
        priority=1,
        thermal_relief=True,
    )

    # Add power plane on top layer
    gen.add_zone(
        net="+3.3V",
        layer="F.Cu",
        priority=0,
    )

    # Save changes
    gen.save("board_with_zones.kicad_pcb")
"""

from .generator import ZoneConfig, ZoneGenerator, parse_power_nets

__all__ = [
    "ZoneGenerator",
    "ZoneConfig",
    "parse_power_nets",
]
