"""
GPU acceleration detection and management.

This module provides utilities for detecting available GPU acceleration
backends and suggesting appropriate installation commands.
"""

from kicad_tools.acceleration.detection import (
    GPUBackend,
    GPUInfo,
    detect_gpu,
    get_available_backends,
    suggest_install_command,
)

__all__ = [
    "GPUBackend",
    "GPUInfo",
    "detect_gpu",
    "get_available_backends",
    "suggest_install_command",
]
