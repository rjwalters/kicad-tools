"""Explicit V-score line tagging shared by ``kct panel`` and the exporter (#6165)."""

from __future__ import annotations

import uuid

# ``kct panel`` tags every score line it draws: the fourth group of the line's
# uuid is this constant.  A random v4 uuid's fourth group starts with 8-b, so
# ``5c0e`` never occurs by chance and the tag survives KiCad load/save (#6165).
VSCORE_UUID_MARKER = "5c0e"


def make_vscore_uuid() -> str:
    """A fresh uuid carrying :data:`VSCORE_UUID_MARKER`."""
    parts = str(uuid.uuid4()).split("-")
    parts[3] = VSCORE_UUID_MARKER
    return "-".join(parts)
