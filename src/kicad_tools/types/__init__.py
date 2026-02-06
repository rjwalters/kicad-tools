"""Reusable type definitions for kicad-tools.

This package provides foundational types used across the kicad-tools
codebase, starting with interval arithmetic for parametric constraints.
"""

from __future__ import annotations

from .interval import Interval, UnitError

__all__ = ["Interval", "UnitError"]
