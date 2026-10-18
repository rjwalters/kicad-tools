"""kct must not crash on non-encodable help text under a legacy console encoding (Issue #6894)."""

import io
import sys

import pytest

from kicad_tools.cli import main


def test_route_help_survives_cp1252_stdout(monkeypatch):
    raw = io.BytesIO()
    cp1252 = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", cp1252)

    with pytest.raises(SystemExit) as exc:
        main(["route", "--help"])

    assert exc.value.code == 0
    cp1252.flush()
    assert raw.getvalue()
