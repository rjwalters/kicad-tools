"""Helpers for the canonical machine-output idiom (``--format json``).

``kct`` standardizes machine-readable output on a single spelling::

    kct <command> --format json

A handful of older commands grew a boolean ``--json`` flag instead.  Those
spellings are kept working forever as **legacy aliases** -- they are never
removed or repurposed -- but new machine-output surfaces must use
``--format`` with a ``json`` choice.  See ``docs/reference/machine-output.md``
for the full design note, the audited inventory, and the exemption list.

This module is the shared machinery for commands that carry both spellings:

* :func:`add_format_flag` / :func:`add_legacy_json_flag` -- declare the pair
  of arguments with consistent help text.
* :func:`normalize_format_alias` -- reconcile the two spellings after
  ``parse_args`` so downstream code can keep checking a single attribute.
* :func:`wants_json` -- one-shot predicate for code that prefers not to
  mutate the namespace.

Precedence rule: either spelling requesting JSON wins.  The two flags cannot
conflict, because ``--json`` only ever means ``--format json``.
"""

from __future__ import annotations

import argparse

FORMAT_TEXT = "text"
FORMAT_JSON = "json"


def add_format_flag(
    parser: argparse.ArgumentParser,
    *,
    choices: tuple[str, ...] = (FORMAT_TEXT, FORMAT_JSON),
    default: str = FORMAT_TEXT,
    dest: str = "format",
    help_text: str = "Output format (default: %(default)s)",
) -> argparse.Action:
    """Add the canonical ``--format`` choice flag to *parser*."""
    return parser.add_argument(
        "--format",
        choices=list(choices),
        default=default,
        dest=dest,
        help=help_text,
    )


def add_legacy_json_flag(
    parser: argparse.ArgumentParser,
    *,
    dest: str = "json",
    help_text: str = "Output as JSON (same as --format json)",
) -> argparse.Action:
    """Add the legacy ``--json`` boolean alias to *parser*.

    Only for commands that already shipped ``--json``; new commands should
    add :func:`add_format_flag` alone.
    """
    return parser.add_argument(
        "--json",
        action="store_true",
        dest=dest,
        help=help_text,
    )


def wants_json(
    args: argparse.Namespace,
    *,
    format_attr: str = "format",
    json_attr: str = "json",
) -> bool:
    """Return ``True`` when JSON output was requested via either spelling."""
    if getattr(args, json_attr, False):
        return True
    return getattr(args, format_attr, None) == FORMAT_JSON


def normalize_format_alias(
    args: argparse.Namespace,
    *,
    format_attr: str = "format",
    json_attr: str = "json",
) -> argparse.Namespace:
    """Reconcile the legacy ``--json`` boolean with the canonical ``--format``.

    After this call the two attributes agree: if either spelling requested
    JSON, ``args.<json_attr>`` is ``True`` and ``args.<format_attr>`` is
    ``"json"``.  Downstream code may keep checking whichever attribute it
    historically used.  Returns *args* for convenience.
    """
    if getattr(args, format_attr, None) == FORMAT_JSON:
        setattr(args, json_attr, True)
    elif getattr(args, json_attr, False):
        setattr(args, format_attr, FORMAT_JSON)
    return args
