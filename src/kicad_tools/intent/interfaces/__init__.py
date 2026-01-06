"""
Interface specifications for common hardware interfaces.

This package contains implementations of InterfaceSpec for various hardware
interfaces like USB, SPI, I2C, etc. Each interface spec defines:

- Required net configuration
- Constraint derivation rules
- Intent-aware validation messages

Available interface specifications:
    - USB: usb2_low_speed, usb2_full_speed, usb2_high_speed, usb3_gen1, usb3_gen2
"""

# Import interface modules to trigger registration
from . import usb

__all__ = ["usb"]
