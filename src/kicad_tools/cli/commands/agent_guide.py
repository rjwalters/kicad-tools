"""``kct agent-guide``: print the harness-neutral agent primer (issue #5960).

The primer is package data (``kicad_tools/agent_skills/AGENT_GUIDE.md``);
:mod:`kicad_tools.agent_skills.guide` renders it. Text output is the Markdown
itself. ``--format json`` emits one document on stdout::

    {"version": "...", "harness": "codex" | null,
     "sections": [{"title": "...", "body": "..."}, ...]}
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argparse import Namespace

__all__ = ["run_agent_guide_command"]


def run_agent_guide_command(args: Namespace) -> int:
    """Handle ``kct agent-guide``."""
    from kicad_tools import __version__
    from kicad_tools.agent_skills.guide import guide_sections, render_guide

    harness = getattr(args, "agent_guide_harness", None)
    fmt = getattr(args, "agent_guide_format", "text")
    markdown = render_guide(harness)
    if fmt == "json":
        payload = {
            "version": __version__,
            "harness": harness,
            "sections": guide_sections(markdown),
        }
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(markdown)
    return 0
