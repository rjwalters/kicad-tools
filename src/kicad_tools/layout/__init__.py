"""
Layout preservation and component addressing for KiCad designs.

This module provides hierarchical address-based component matching
to preserve layout when regenerating PCB from schematic changes.
"""

from .addressing import AddressRegistry
from .types import ComponentAddress

__all__ = [
    "AddressRegistry",
    "ComponentAddress",
]
