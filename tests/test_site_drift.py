"""Tests for scripts/ci/site_drift.py (issue #7664): parsers, comparisons, never-raise."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ci" / "site_drift.py"
_spec = importlib.util.spec_from_file_location("site_drift", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

FOOTER = (
    '<p>Site commit:<a href="x"><code data-a>5c0f5b99</code></a><span>·</span>'
    "Project version: <code data-a>v0.20.0</code></p>"
    '<p>Site built <time datetime="2026-09-12T18:47:58.854Z">x</time>.</p>'
)
CARDS = (
    '<a class="card" href="/00-simple-led" data-status="ready" data-x>'
    '<a class="card" href="/02-charlieplex-led" data-status="unverified" data-x>'
)


def test_parse_footer():
    assert mod.parse_footer(FOOTER) == {
        "commit": "5c0f5b99",
        "version": "0.20.0",
        "built": "2026-09-12T18:47:58.854Z",
    }


def test_parse_footer_unparseable_is_finding_not_crash():
    assert mod.parse_footer("<html></html>") == {}
    assert "unparseable" in mod.compare_footer({}, "v0.24.0", "abc")[0]


def test_version_and_commit_drift():
    out = mod.compare_footer({"commit": "5c0f5b99", "version": "0.20.0"}, "v0.24.0", "ffff0000aaaa")
    assert len(out) == 2 and "v0.24.0" in out[0]


def test_no_drift():
    assert (
        mod.compare_footer({"commit": "ffff0000", "version": "0.24.0"}, "v0.24.0", "ffff0000aaaa")
        == []
    )


def test_parse_cards_and_checked():
    assert mod.parse_cards(CARDS) == {"00-simple-led": "ready", "02-charlieplex-led": "unverified"}
    assert (
        mod.parse_checked('<p>Checked <time datetime="2026-10-19T20:30:00Z">')
        == "2026-10-19T20:30:00Z"
    )
    assert mod.parse_checked("nothing") is None


def test_board_readiness_mismatch_and_date():
    r = {"status": "ready", "checked_at": "2026-10-19T20:30:00Z"}
    out = mod.compare_board("b", r, "unverified", "2026-09-01T00:00:00Z")
    assert len(out) == 2
    assert mod.compare_board("b", r, "ready", "2026-10-19T20:30:00Z") == []


def test_board_without_readiness_json_skipped():
    assert mod.compare_board("b", None, "unverified", None) == []


def test_fetch_failure_reported_not_raised():
    body, err = mod.fetch("http://127.0.0.1:9/", timeout=1)
    assert body is None and err


def test_run_unreachable_site(tmp_path):
    out = mod.run("http://127.0.0.1:9", tmp_path)
    assert out and "unreachable" in out[0]


def test_stage_assets_prefers_routed(tmp_path):
    out = tmp_path / "boards" / "b1" / "output"
    out.mkdir(parents=True)
    (out / "a.kicad_pcb").write_text("raw")
    (out / "a_routed.kicad_pcb").write_text("routed")
    (out / "manufacturing.zip").write_bytes(b"z")
    dest = tmp_path / "stage"
    assert mod.stage_assets(tmp_path / "boards", dest) == 2
    assert (dest / "b1" / "board.kicad_pcb").read_text() == "routed"


def test_equivalent_timestamp_spellings_are_not_drift():
    r = {"status": "ready", "checked_at": "2026-10-19T20:30:00Z"}
    assert mod.compare_board("b", r, "ready", "2026-10-19T20:30:00+00:00") == []
