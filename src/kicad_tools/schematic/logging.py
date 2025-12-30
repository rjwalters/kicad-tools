"""
KiCad Schematic Logging Configuration

Agent-focused logging utilities for debugging schematic operations.
"""

import logging

# Create a logger for the KiCad helper module
_logger = logging.getLogger("kicad_sch_helper")
_logger.addHandler(logging.NullHandler())  # Default: no output


def enable_verbose(level: str = "INFO", format: str = None) -> None:
    """Enable verbose logging for debugging.

    This helps agents understand what operations are being performed
    and diagnose issues with schematic generation.

    Args:
        level: Logging level - "DEBUG", "INFO", "WARNING", "ERROR"
        format: Optional custom format string

    Example:
        # Enable verbose output before problematic operations
        enable_verbose("DEBUG")

        sch = Schematic("Test")
        sch.add_symbol("Device:C", 100, 100, "C1")  # Will log operation

        # Disable when done
        disable_verbose()
    """
    _logger.setLevel(getattr(logging, level.upper()))

    # Remove existing handlers
    for handler in _logger.handlers[:]:
        if not isinstance(handler, logging.NullHandler):
            _logger.removeHandler(handler)

    # Add console handler
    handler = logging.StreamHandler()
    handler.setLevel(getattr(logging, level.upper()))

    if format is None:
        format = "[%(levelname)s] %(message)s"

    handler.setFormatter(logging.Formatter(format))
    _logger.addHandler(handler)


def disable_verbose() -> None:
    """Disable verbose logging."""
    _logger.setLevel(logging.WARNING)
    for handler in _logger.handlers[:]:
        if not isinstance(handler, logging.NullHandler):
            _logger.removeHandler(handler)


def _log_debug(msg: str) -> None:
    """Log a debug message."""
    _logger.debug(msg)


def _log_info(msg: str) -> None:
    """Log an info message."""
    _logger.info(msg)


def _log_warning(msg: str) -> None:
    """Log a warning message."""
    _logger.warning(msg)
