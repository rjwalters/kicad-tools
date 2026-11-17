"""
Interface specifications for common hardware interfaces.

This package contains implementations of InterfaceSpec for various hardware
interfaces like USB, SPI, I2C, etc. Each interface spec defines:

- Required net configuration
- Constraint derivation rules
- Intent-aware validation messages

Available interface specifications:
    - USB: usb2_low_speed, usb2_full_speed, usb2_high_speed, usb3_gen1, usb3_gen2
    - SPI: spi_standard, spi_fast, spi_high_speed
    - I2C: i2c_standard, i2c_fast, i2c_fast_plus
    - Power: power_rail
"""

# Import interface modules to trigger registration
from . import i2c, power, spi, usb

__all__ = ["i2c", "power", "spi", "usb"]
