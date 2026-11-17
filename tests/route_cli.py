"""Helpers for driving ``kct route`` from tests (issue #7017).

``kct route`` hands everything after ``route`` straight to
``route_cmd.main``; ``route_cmd._route_parser`` is its only parser.  Tests
that used to build the old outer ``kct route`` namespace, or ran the deleted
Namespace-to-argv forwarding shim, use these instead.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from unittest.mock import patch


def route_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse ``kct route`` *argv* (no leading ``route``) with the real parser."""
    from kicad_tools.cli.route_cmd import _route_parser

    return _route_parser().parse_args(list(argv))


def kct_route(
    argv: Sequence[str], global_flags: Sequence[str] = ()
) -> tuple[list[str], argparse.Namespace]:
    """Run ``kct [global_flags] route <argv>`` in-process with the router stubbed.

    Returns the argv ``route_cmd.main`` received and that argv parsed by the
    real route parser -- i.e. exactly what the router would have run with.
    """
    from kicad_tools.cli import main

    with patch("kicad_tools.cli.route_cmd.main", return_value=0) as fake:
        assert main([*global_flags, "route", *argv]) == 0
    assert fake.call_count == 1
    forwarded = list(fake.call_args[0][0])
    return forwarded, route_args(forwarded)
