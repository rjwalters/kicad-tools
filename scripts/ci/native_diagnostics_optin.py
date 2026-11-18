"""Decide whether a pull request opted into native-workload diagnostics.

Issue #9120. The opt-in used to be ``contains(github.event.pull_request.body,
'<!-- kct:native-diagnostics -->')`` -- an unanchored substring test, so a PR
that merely *wrote about* the marker (quoting it in prose to explain why a run
had no observer artifacts) turned the audit gate on for itself, and could not
clear the failure: ``on: pull_request`` does not re-run on a body edit.

GitHub expressions have no regex, so the test lives here. The marker counts
only when it is the **whole of a line** (trailing blanks and a CRLF tolerated)
that is **not inside a fenced code block**:

* inline in a sentence, in a ``> `` quote, in a list item or inside backticks
  -> not an opt-in (prose cannot trip it);
* alone on a line -> opt-in;
* alone on a line *inside* a ``` / ``~~~`` fence -> not an opt-in. A fenced
  block is how a body quotes the marker verbatim, so honouring it would
  reintroduce the defect this script exists to remove.

The PR body is read from the event JSON file (``GITHUB_EVENT_PATH``), never
interpolated into a shell command, and is not echoed. Only ``true``/``false``
is emitted. ``push`` and every other event stay ``false``.

Usage in a workflow step (writes ``KCT_NATIVE_DIAGNOSTICS=...`` to
``$GITHUB_ENV`` when set, otherwise to stdout)::

    python3 scripts/ci/native_diagnostics_optin.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

MARKER = "<!-- kct:native-diagnostics -->"
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def body_has_marker(body: str | None) -> bool:
    """True when ``MARKER`` is alone on a line outside any fenced code block."""
    if not body:
        return False
    fence: str | None = None  # the opening run, e.g. "```" or "~~~~"
    for line in body.splitlines():
        opened = _FENCE.match(line)
        if fence is None:
            if opened:
                fence = opened.group(1)
                continue
        elif opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
            fence = None
            continue
        if fence is None and line.rstrip(" \t") == MARKER:
            return True
    return False


def opted_in(event_name: str | None, event: Any) -> bool:
    """Opt-in verdict for one workflow event (``pull_request`` only)."""
    if event_name != "pull_request" or not isinstance(event, dict):
        return False
    pull_request = event.get("pull_request")
    body = pull_request.get("body") if isinstance(pull_request, dict) else None
    return body_has_marker(body if isinstance(body, str) else None)


def main(environ: dict[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    event: Any = None
    path = env.get("GITHUB_EVENT_PATH")
    if path:
        try:
            event = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            event = None  # unreadable event -> inert, never a failure
    verdict = "true" if opted_in(env.get("GITHUB_EVENT_NAME"), event) else "false"
    line = f"KCT_NATIVE_DIAGNOSTICS={verdict}"
    target = env.get("GITHUB_ENV")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
