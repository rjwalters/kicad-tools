"""
GPU acceleration module for kicad-tools.

Provides GPU-accelerated implementations of computationally intensive
algorithms for routing, placement, and signal integrity analysis.
"""

from kicad_tools.acceleration.config import should_use_gpu

__all__ = ["should_use_gpu"]
