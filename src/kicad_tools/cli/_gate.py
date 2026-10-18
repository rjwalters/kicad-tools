"""Helpers shared by the sign-off commands' exit gates (Issue #7230).

View filters (``--net``, ``--type``, ``--category``, ``--errors-only``) change
what is *shown*, never the verdict: a command snapshots its findings before
applying them, gates on the snapshot, and discloses what the filters hid.
"""

from __future__ import annotations

from typing import Any, Iterable


def hidden_by_view_filter(
    gate_violations: Iterable[Any], shown: Iterable[Any]
) -> dict[str, int] | None:
    """Errors/warnings in ``gate_violations`` that a view filter left out of ``shown``.

    Generic over DRC/ERC violations and sync issues: only ``.is_error`` and
    object identity are used.  Returns ``None`` when nothing was hidden, so a
    filter that matches everything (or no filter at all) produces exactly
    today's output.
    """
    shown_ids = {id(v) for v in shown}
    hidden = [v for v in gate_violations if id(v) not in shown_ids]
    if not hidden:
        return None
    errors = sum(1 for v in hidden if v.is_error)
    return {"errors": errors, "warnings": len(hidden) - errors}
