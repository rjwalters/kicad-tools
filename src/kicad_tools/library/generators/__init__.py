"""
Parametric footprint generators for common package types.

These generators create KiCad footprints following IPC-7351 naming conventions.
"""

from .soic import create_soic
from .qfp import create_qfp
from .qfn import create_qfn
from .sot import create_sot
from .chip import create_chip
from .through_hole import create_dip, create_pin_header

__all__ = [
    "create_soic",
    "create_qfp",
    "create_qfn",
    "create_sot",
    "create_chip",
    "create_dip",
    "create_pin_header",
]
