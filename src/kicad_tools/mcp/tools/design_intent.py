"""MCP tool for reading persisted project design intent.

A project's ``.kct`` specification file can carry two distinct kinds of
project-level record that are not derived from any live session:

* ``intent.constraints`` -- standing design guidance the project author wrote
  down (e.g. "must fit a 2-layer JLCPCB stackup"). These are *current*
  project-level guidance, not session-scoped electrical constraints (that is
  what ``list_intents`` returns) and not machine-recorded routing rationale.
* ``decisions`` -- an append-only historical log of material design choices,
  each with a topic, the choice made, and its rationale (that is what
  ``get_decision_history`` returns for *session* decisions; this reads the
  project's own persisted log instead).

``get_design_intent`` surfaces both, read-only, for an explicitly named spec
file. It never infers an "active" project from the server's working directory
or from session state, and it never writes to the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kicad_tools.spec.parser import load_spec


def get_design_intent(spec_path: str) -> dict[str, Any]:
    """Read the standing constraints and decision history from a .kct file.

    Args:
        spec_path: Path to the ``.kct`` project specification file to read.
            The caller must supply the exact path -- no active project is
            inferred.

    Returns:
        Dict with:
            - ``spec_path``: absolute path of the file that was read
            - ``summary``: the project's ``intent.summary``, or ``None`` if
              the spec has no ``intent`` block
            - ``constraints``: list of standing-guidance strings from
              ``intent.constraints``. Always a list -- an absent ``intent``
              block or a null/absent ``constraints`` field both normalize to
              ``[]``.
            - ``decisions``: list of historical decision records (each with
              ``topic``, ``choice``, ``rationale``, and any of ``date``,
              ``phase``, ``alternatives``, ``author`` that are present), in
              the same order they appear in the file. Always a list -- a
              null/absent ``decisions`` field normalizes to ``[]``. Dates are
              serialized as ISO-8601 strings.

    Raises:
        FileNotFoundError: If ``spec_path`` does not exist.
        IsADirectoryError: If ``spec_path`` is a directory.
        ValueError: If the file is not valid YAML, is empty, or does not
            contain a YAML mapping.
        pydantic.ValidationError: If the content does not match the ``.kct``
            schema (e.g. a present ``intent`` block missing its required
            ``summary`` field).
        OSError: If the file cannot otherwise be read (e.g. permissions).
    """
    path = Path(spec_path)

    if path.is_dir():
        raise IsADirectoryError(f"Spec path is a directory, not a file: {spec_path}")

    spec = load_spec(path)

    intent = spec.intent
    summary = intent.summary if intent is not None else None
    constraints = list(intent.constraints) if intent is not None and intent.constraints else []

    decisions = [
        decision.model_dump(exclude_none=True, mode="json") for decision in (spec.decisions or [])
    ]

    return {
        "spec_path": str(path.absolute()),
        "summary": summary,
        "constraints": constraints,
        "decisions": decisions,
    }
