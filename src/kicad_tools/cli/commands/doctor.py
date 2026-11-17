"""``kct doctor`` command handler.

Thin CLI glue over :mod:`kicad_tools.doctor`. Two check groups run:

1. **version-record drift** (issue #4347): compare the installed package
   version against the version records the installer stamps into a consumer
   repo.
2. **environment preflight** (issue #4542): runtime-prerequisite probes
   (native router backend, ``kicad-cli``, Python/shapely, PATH-shadowed
   ``kct``), each fail-soft with ``ok`` / ``warn`` / ``fail`` semantics.

JSON output is additive: the top-level dict keeps the exact version-drift
shape and gains an ``"environment"`` key (see the :mod:`kicad_tools.doctor`
module docstring for the full shape).
"""

import json
from pathlib import Path

__all__ = ["run_doctor_command"]


def run_doctor_command(args) -> int:
    """Handle the ``doctor`` command.

    Advisory by default (always exits 0 so it can be run informationally). With
    ``--strict`` it exits 1 when any version record has drifted **or** any
    environment preflight reports ``fail`` (``warn`` never affects the exit
    code) -- mirroring ``build-native --check`` so it is gateable in CI /
    pre-commit hooks.
    """
    from kicad_tools import __version__
    from kicad_tools.doctor import (
        check_environment,
        check_version_drift,
        environment_to_dict,
        render_environment_text,
        render_text,
        report_to_dict,
    )

    root = Path(getattr(args, "doctor_root", None) or ".")
    output_format = getattr(args, "doctor_format", "text")
    strict = getattr(args, "doctor_strict", False)

    drift_report = check_version_drift(root, __version__)
    environment_report = check_environment(installed_version=__version__)

    if output_format == "json":
        payload = report_to_dict(drift_report)
        payload["environment"] = environment_to_dict(environment_report)
        print(json.dumps(payload, indent=2))
    else:
        print(render_text(drift_report))
        print()
        print(render_environment_text(environment_report))

    if strict and (drift_report.has_drift or environment_report.has_fail):
        return 1
    return 0
