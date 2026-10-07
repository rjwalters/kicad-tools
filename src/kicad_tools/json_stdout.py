"""Keep ``--format json`` stdout a single JSON document (issue #5938).

Agents and MCP callers parse ``kct <command> --format json`` stdout with a
strict ``json.loads``.  Most commands achieve that by printing progress and
diagnostics to stderr directly.  A few long-running drivers (``kct route``)
print hundreds of progress lines from deep inside the router, where threading
the output format through every call site is impractical.

For those, :func:`prose_to_stderr` swaps ``sys.stdout`` for ``sys.stderr``
for the duration of the work, so every ordinary ``print`` lands on stderr,
while the emitters of the machine-readable document write to
:func:`json_stdout` -- the real stdout saved when the guard was entered::

    with prose_to_stderr(args.format == "json"):
        run_the_command()          # print(...) -> stderr

    # inside, the JSON emitter does:
    print(json.dumps(doc), file=json_stdout())

Outside any guard, :func:`json_stdout` is simply ``sys.stdout``, so emitters
written this way behave identically when the guard is not active.
"""

from __future__ import annotations

import contextlib
import io
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

__all__ = ["JsonStream", "json_stdout", "prose_to_stderr"]


class JsonStream:
    """The real stdout, recording whether a JSON document was written to it."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self.written = False

    def write(self, text: str) -> int:
        if text:
            self.written = True
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()

    @contextmanager
    def capture(self) -> Iterator[io.StringIO]:
        """Hold the JSON document back instead of writing it (issue #6054).

        A post-processing step (``kct route --lint-gate``) may need to amend
        the document after the emitter ran.  ``written`` is still recorded,
        so emitters that check it behave as usual; the caller writes the
        (amended) buffer to the real stream itself.
        """
        real = self._stream
        buffer = io.StringIO()
        self._stream = buffer
        try:
            yield buffer
        finally:
            self._stream = real

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


# Stack of the real stdout streams saved by active guards (guards may nest).
_saved_stdouts: list[JsonStream] = []


@contextmanager
def prose_to_stderr(enabled: bool = True) -> Iterator[JsonStream | None]:
    """Route ordinary ``print`` output to stderr while *enabled*.

    Yields the :class:`JsonStream` wrapping the real stdout (``None`` when
    disabled) so a caller can tell whether any JSON was emitted.  No-op when
    *enabled* is false, so call sites can pass ``args.format == "json"``
    unconditionally.
    """
    if not enabled:
        yield None
        return
    real_stdout = sys.stdout
    stream = JsonStream(real_stdout)
    _saved_stdouts.append(stream)
    sys.stdout = sys.stderr
    try:
        yield stream
    finally:
        with contextlib.suppress(Exception):  # best-effort flush of stderr
            sys.stdout.flush()
        sys.stdout = real_stdout
        _saved_stdouts.pop()


def json_stdout() -> TextIO:
    """Return the stream a JSON document must be written to.

    Inside :func:`prose_to_stderr` this is the real stdout saved on entry;
    otherwise it is the current ``sys.stdout``.
    """
    if _saved_stdouts:
        return _saved_stdouts[-1]  # type: ignore[return-value]
    return sys.stdout
