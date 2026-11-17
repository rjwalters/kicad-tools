"""Platform-aware GPU acceleration install command suggestions."""

from __future__ import annotations

import platform


def suggest_install_command() -> str:
    """Suggest the right pip install command for GPU acceleration on this platform.

    Returns:
        A pip install command string appropriate for the current platform.
    """
    system = platform.system()
    machine = platform.machine()

    if system == "Darwin":
        if machine == "arm64":
            return "pip install kicad-tools[metal]"
        return "pip install kicad-tools  # No GPU acceleration for Intel Mac"
    elif system == "Linux":
        return "pip install kicad-tools[cuda]"
    else:
        # Windows or other
        return "pip install kicad-tools[cuda]  # Requires NVIDIA GPU"
