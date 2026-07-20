"""``kct doctor`` command handler.

Thin CLI glue over :mod:`kicad_tools.doctor`. The first (currently only) check
is version-record drift (issue #4347): compare the installed package version
against the version records the installer stamps into a consumer repo.
"""

import json
from pathlib import Path

__all__ = ["run_doctor_command"]


def run_doctor_command(args) -> int:
    """Handle the ``doctor`` command.

    Advisory by default (always exits 0 so it can be run informationally). With
    ``--strict`` it exits 1 when any version record has drifted -- mirroring
    ``build-native --check`` so it is gateable in CI / pre-commit hooks.
    """
    from kicad_tools import __version__
    from kicad_tools.doctor import (
        check_version_drift,
        render_text,
        report_to_dict,
    )

    root = Path(getattr(args, "doctor_root", None) or ".")
    output_format = getattr(args, "doctor_format", "text")
    strict = getattr(args, "doctor_strict", False)

    report = check_version_drift(root, __version__)

    if output_format == "json":
        print(json.dumps(report_to_dict(report), indent=2))
    else:
        print(render_text(report))

    if strict and report.has_drift:
        return 1
    return 0
