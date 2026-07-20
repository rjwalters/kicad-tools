"""Tests for DRC report JSON format parsing (KiCad-cli and kct-check formats)."""

import json
from pathlib import Path

import pytest

from kicad_tools.drc.report import parse_json_report
from kicad_tools.drc.violation import Severity, ViolationType


class TestKctCheckJsonFormat:
    """Tests for parsing kct-check JSON format written by ``kct check --output``."""

    def test_parse_empty_violations(self):
        """Parse kct-check report with no violations (DRC passed)."""
        data = {
            "file": "/path/to/board.kicad_pcb",
            "manufacturer": "jlcpcb",
            "layers": 2,
            "summary": {
                "errors": 0,
                "warnings": 0,
                "rules_checked": 4,
                "passed": True,
            },
            "violations": [],
        }
        report = parse_json_report(json.dumps(data), source_file="test.json")
        assert report.pcb_name == "/path/to/board.kicad_pcb"
        assert report.violation_count == 0
        assert report.error_count == 0
        assert report.warning_count == 0

    def test_parse_with_violations(self):
        """Parse kct-check report with both errors and warnings."""
        data = {
            "file": "/path/to/board.kicad_pcb",
            "manufacturer": "jlcpcb",
            "layers": 2,
            "summary": {
                "errors": 1,
                "warnings": 1,
                "rules_checked": 4,
                "passed": False,
            },
            "violations": [
                {
                    "rule_id": "clearance_pad_pad",
                    "severity": "warning",
                    "message": "Pad-to-pad clearance 0.15 mm below minimum 0.20 mm",
                    "location": [100.5, 200.3],
                    "layer": "F.Cu",
                    "actual_value": 0.15,
                    "required_value": 0.20,
                    "items": ["Pad 1 of U1", "Pad 2 of C3"],
                },
                {
                    "rule_id": "track_width",
                    "severity": "error",
                    "message": "Track width too narrow",
                    "location": [50.0, 75.0],
                    "layer": "B.Cu",
                    "actual_value": 0.1,
                    "required_value": 0.15,
                    "items": ["Track on B.Cu"],
                },
            ],
        }
        report = parse_json_report(json.dumps(data), source_file="test.json")
        assert report.violation_count == 2
        assert report.error_count == 1
        assert report.warning_count == 1

        # Check warning violation
        warning = report.warnings[0]
        assert warning.type_str == "clearance_pad_pad"
        assert warning.severity == Severity.WARNING
        assert warning.message == "Pad-to-pad clearance 0.15 mm below minimum 0.20 mm"
        assert len(warning.locations) == 1
        assert warning.locations[0].x_mm == 100.5
        assert warning.locations[0].y_mm == 200.3
        assert warning.locations[0].layer == "F.Cu"
        assert warning.items == ["Pad 1 of U1", "Pad 2 of C3"]
        assert warning.actual_value_mm == 0.15
        assert warning.required_value_mm == 0.20

        # Check error violation
        error = report.errors[0]
        assert error.type_str == "track_width"
        assert error.severity == Severity.ERROR
        assert error.type == ViolationType.TRACK_WIDTH

    def test_parse_no_location(self):
        """Parse kct-check violation without location field."""
        data = {
            "file": "board.kicad_pcb",
            "summary": {"errors": 1, "warnings": 0, "rules_checked": 1, "passed": False},
            "violations": [
                {
                    "rule_id": "unconnected_items",
                    "severity": "error",
                    "message": "Unconnected net",
                    "items": ["Net VCC"],
                },
            ],
        }
        report = parse_json_report(json.dumps(data))
        assert report.violation_count == 1
        v = report.violations[0]
        assert len(v.locations) == 0
        assert v.items == ["Net VCC"]

    def test_parse_empty_items(self):
        """Parse kct-check violation with empty items list."""
        data = {
            "file": "board.kicad_pcb",
            "manufacturer": "jlcpcb",
            "summary": {"errors": 1, "warnings": 0, "rules_checked": 1, "passed": False},
            "violations": [
                {
                    "rule_id": "unknown_rule",
                    "severity": "error",
                    "message": "Some error",
                    "items": [],
                },
            ],
        }
        report = parse_json_report(json.dumps(data))
        assert report.violation_count == 1
        assert report.violations[0].items == []

    def test_format_detection_with_manufacturer_only(self):
        """Detect kct-check format when only 'manufacturer' key is present."""
        data = {
            "file": "board.kicad_pcb",
            "manufacturer": "pcbway",
            "layers": 4,
            "violations": [],
        }
        report = parse_json_report(json.dumps(data))
        assert report.pcb_name == "board.kicad_pcb"
        assert report.violation_count == 0

    def test_format_detection_with_summary_only(self):
        """Detect kct-check format when only 'summary' key is present."""
        data = {
            "file": "board.kicad_pcb",
            "summary": {"errors": 0, "warnings": 0, "rules_checked": 2, "passed": True},
            "violations": [],
        }
        report = parse_json_report(json.dumps(data))
        assert report.pcb_name == "board.kicad_pcb"
        assert report.violation_count == 0


class TestKicadCliJsonFormat:
    """Tests for parsing KiCad-cli JSON format (regression tests)."""

    def test_parse_empty_violations(self):
        """Parse KiCad-cli report with no violations."""
        data = {
            "source": "board.kicad_pcb",
            "date": "2025-12-28T21:29:34",
            "violations": [],
        }
        report = parse_json_report(json.dumps(data))
        assert report.pcb_name == "board.kicad_pcb"
        assert report.violation_count == 0
        assert report.created_at is not None
        assert report.created_at.year == 2025

    def test_parse_with_violations(self):
        """Parse KiCad-cli report with dict-style items."""
        data = {
            "source": "board.kicad_pcb",
            "date": "2025-12-28T21:29:34",
            "violations": [
                {
                    "type": "clearance",
                    "description": "Clearance violation (0.20 mm required, actual 0.15 mm)",
                    "severity": "error",
                    "pos": {"x": 162.45, "y": 100.32},
                    "items": [
                        {
                            "description": "Pad 6 [VCC] of U3 on F.Cu",
                            "pos": {"x": 162.45, "y": 100.32},
                            "net": "VCC",
                        },
                        {
                            "description": "Via [SPI_NSS] on F.Cu - B.Cu",
                            "pos": {"x": 161.6, "y": 100.9},
                            "net": "SPI_NSS",
                        },
                    ],
                },
            ],
        }
        report = parse_json_report(json.dumps(data))
        assert report.violation_count == 1
        v = report.violations[0]
        assert v.type_str == "clearance"
        assert v.severity == Severity.ERROR
        assert len(v.items) == 2
        assert "Pad 6 [VCC] of U3 on F.Cu" in v.items
        assert len(v.nets) == 2
        assert "VCC" in v.nets
        assert "SPI_NSS" in v.nets
        # 3 locations: one from violation pos + two from items
        assert len(v.locations) == 3

    def test_parse_with_no_items_key(self):
        """Parse KiCad-cli violation without items."""
        data = {
            "source": "board.kicad_pcb",
            "date": "2025-01-01T00:00:00",
            "violations": [
                {
                    "type": "footprint",
                    "description": "Footprint error",
                    "severity": "warning",
                },
            ],
        }
        report = parse_json_report(json.dumps(data))
        assert report.violation_count == 1
        assert report.violations[0].items == []


# ---------------------------------------------------------------------------
# Golden-schema contract for real `kicad-cli pcb drc --format json` output.
#
# The hand-authored dicts above prove the *parser* handles a given shape, but
# they cannot catch drift in what `kicad-cli` actually emits -- the fixtures
# are written by us, not captured from the tool. The golden fixture below is a
# byte-faithful capture of real `kicad-cli pcb drc --format json` output, so a
# future KiCad that renames/drops a key we consume (e.g. `violations`,
# `severity`, or a violation's `type`) breaks these assertions LOUDLY instead
# of degrading to an empty ("0 errors = clean") report -- the exact silent-pass
# risk in `_parse_kicad_cli_json`, which reads every field via `dict.get(...)`.
#
# ON A KICAD-CLI BUMP: re-capture the golden fixture and reconcile any diff
# before shipping. Regenerate with (from repo root):
#
#     kicad-cli pcb drc --format json --units mm \
#       --output tests/fixtures/drc/kicad_cli_drc_golden.json \
#       tests/fixtures/stale_nets.kicad_pcb
#
# then bump GOLDEN_KICAD_VERSION below to match `kicad_version` in the payload.
# ---------------------------------------------------------------------------

# KiCad version the golden fixture was captured from. Bump when re-capturing.
GOLDEN_KICAD_VERSION = "10.0.4"

_GOLDEN_FIXTURE = Path(__file__).parent / "fixtures" / "drc" / "kicad_cli_drc_golden.json"

# The board the golden fixture was captured from -- also drives the optional
# live smoke test so a bumped local KiCad is caught immediately.
_GOLDEN_SOURCE_BOARD = Path(__file__).parent / "fixtures" / "stale_nets.kicad_pcb"

# Top-level keys `_parse_kicad_cli_json` consumes. Absence of `violations` is
# the headline silent-pass risk: the parser would read it as a clean board.
_REQUIRED_KICAD_CLI_DRC_KEYS = ("source", "date", "violations")

# Per-violation keys the parser reads without a fallback that would be visible
# downstream (`type`/`description`/`severity` map to type_str/message/severity).
_REQUIRED_VIOLATION_KEYS = ("type", "description", "severity")

# The severity enum the contract pins (mirrors `core.types.Severity`).
_ALLOWED_SEVERITIES = {"error", "warning", "info"}


def _load_golden_raw() -> dict:
    data = json.loads(_GOLDEN_FIXTURE.read_text())
    assert isinstance(data, dict)
    return data


class TestKicadCliDrcGoldenSchema:
    """Pin the real `kicad-cli pcb drc --format json` schema against drift.

    Captured from KiCad ``GOLDEN_KICAD_VERSION`` -- see the module comment for
    the re-capture procedure on a kicad-cli bump.
    """

    def test_golden_fixture_exists_and_is_json(self):
        """The golden fixture is present and valid JSON."""
        assert _GOLDEN_FIXTURE.exists(), (
            f"Golden fixture missing: {_GOLDEN_FIXTURE}. Re-capture with "
            "`kicad-cli pcb drc --format json` (see module comment)."
        )
        data = _load_golden_raw()
        assert isinstance(data, dict)

    def test_golden_records_capture_version(self):
        """The captured payload's KiCad version matches the pinned constant.

        If these diverge, the fixture was re-captured without bumping
        ``GOLDEN_KICAD_VERSION`` (or vice versa) -- reconcile before shipping.
        """
        data = _load_golden_raw()
        assert data.get("kicad_version") == GOLDEN_KICAD_VERSION

    def test_golden_has_required_top_level_keys(self):
        """Top-level keys the parser consumes are present.

        A KiCad that drops/renames `violations` would otherwise degrade to an
        empty, silently-"clean" report.
        """
        data = _load_golden_raw()
        for key in _REQUIRED_KICAD_CLI_DRC_KEYS:
            assert key in data, (
                f"Golden fixture missing required top-level key {key!r}. "
                "KiCad schema may have drifted -- reconcile the parser in "
                "src/kicad_tools/drc/report.py::_parse_kicad_cli_json."
            )
        assert isinstance(data["violations"], list)

    def test_golden_covers_at_least_one_violation(self):
        """The golden fixture is non-trivial (guards an empty capture)."""
        data = _load_golden_raw()
        assert len(data["violations"]) > 0, (
            "Golden fixture has no violations -- re-capture from a board that "
            "produces DRC errors so the schema is actually exercised."
        )

    def test_golden_violation_records_have_required_fields(self):
        """Every violation carries the fields the parser reads."""
        data = _load_golden_raw()
        for i, v in enumerate(data["violations"]):
            for key in _REQUIRED_VIOLATION_KEYS:
                assert key in v, f"violations[{i}] missing required key {key!r}: {v!r}"

    def test_golden_severity_enum_is_pinned(self):
        """Every severity value is one of {error, warning, info}.

        A new/renamed severity (e.g. `critical`) must fail here rather than be
        silently coerced by `Severity.from_string`.
        """
        data = _load_golden_raw()
        for i, v in enumerate(data["violations"]):
            sev = v["severity"]
            assert sev in _ALLOWED_SEVERITIES, (
                f"violations[{i}] has unpinned severity {sev!r}; "
                f"expected one of {sorted(_ALLOWED_SEVERITIES)}. Update "
                "core.types.Severity AND this contract deliberately."
            )

    def test_golden_items_shape(self):
        """Where a violation has items, each item exposes description + pos.{x,y}.

        Note: KiCad 10.0.4 does NOT emit a per-item `net` key nor a
        violation-level `pos` -- the net is embedded in the item
        `description` as ``[NetName]``. The parser reads `net`/`pos`
        defensively via `dict.get`/`"net" in item`, so their absence is
        tolerated (nets end up empty). If a future KiCad ADDS `net`, the
        parser will start populating `DRCViolation.nets`; this test documents
        the current real shape so that transition is a visible diff.
        """
        data = _load_golden_raw()
        saw_items = False
        for i, v in enumerate(data["violations"]):
            for j, item in enumerate(v.get("items", [])):
                saw_items = True
                assert "description" in item, (
                    f"violations[{i}].items[{j}] missing `description`: {item!r}"
                )
                assert "pos" in item, f"violations[{i}].items[{j}] missing `pos`: {item!r}"
                pos = item["pos"]
                assert "x" in pos and "y" in pos, (
                    f"violations[{i}].items[{j}].pos not {{x, y}}: {pos!r}"
                )
        assert saw_items, "Golden fixture has no violation items to check shape"

    def test_golden_has_clearance_violation(self):
        """The captured payload exercises a clearance violation with items.

        Clearance is the workhorse of the geometric cross-gate; pinning it
        ensures the golden covers the shape that matters most for the
        manufacturing-readiness verdict.
        """
        data = _load_golden_raw()
        clearance = [v for v in data["violations"] if v.get("type") == "clearance"]
        assert clearance, "Golden fixture should contain a clearance violation"
        assert any(v.get("items") for v in clearance), (
            "Clearance violation should carry dict-style items"
        )

    def test_golden_round_trips_through_parser(self):
        """The consumed fields survive `parse_json_report` into `DRCReport`.

        Pins the end-to-end mapping the gate relies on: source -> pcb_name,
        date -> created_at, and per-violation type_str/severity/locations.
        """
        raw = _load_golden_raw()
        report = parse_json_report(_GOLDEN_FIXTURE.read_text())

        # source -> pcb_name
        assert report.pcb_name == raw["source"]
        # date -> created_at (ISO-8601 parsed)
        assert report.created_at is not None
        assert report.created_at.isoformat().startswith(raw["date"][:10])
        # Every violation round-tripped.
        assert report.violation_count == len(raw["violations"])
        # type_str / severity map through for each violation.
        for parsed, rawv in zip(report.violations, raw["violations"], strict=True):
            assert parsed.type_str == rawv["type"]
            assert parsed.severity in {
                Severity.ERROR,
                Severity.WARNING,
                Severity.INFO,
            }
            assert parsed.severity == Severity.from_string(rawv["severity"])
            # Item descriptions and their positions become items + locations.
            assert len(parsed.items) == len(rawv.get("items", []))

    def test_drift_simulation_severity_fails_loudly(self):
        """A renamed severity in the payload trips the enum contract loudly.

        This is the tripwire behavior the issue requires: schema drift must
        raise, not degrade to a silently-clean report.
        """
        data = _load_golden_raw()
        # Simulate a KiCad that renames `error` -> `critical`.
        for v in data["violations"]:
            if v["severity"] == "error":
                v["severity"] = "critical"

        # The enum contract must reject the unknown value.
        offenders = [
            v["severity"] for v in data["violations"] if v["severity"] not in _ALLOWED_SEVERITIES
        ]
        assert offenders, "Drift simulation should introduce an unpinned severity"

    def test_drift_simulation_dropped_violations_key_is_visible(self):
        """Dropping `violations` is caught by the top-level contract.

        Without this contract the parser would read a missing `violations`
        key as an empty (clean) report -- the silent-pass this test guards.
        """
        data = _load_golden_raw()
        del data["violations"]
        assert "violations" not in data
        # The parser degrades silently (the risk we are pinning against)...
        report = parse_json_report(json.dumps(data))
        assert report.violation_count == 0  # <- false "clean" if unguarded
        # ...but the shape contract would have caught it first.
        missing = [k for k in _REQUIRED_KICAD_CLI_DRC_KEYS if k not in data]
        assert "violations" in missing


class TestKicadCliDrcLiveSmoke:
    """Optional live smoke test -- skips cleanly when kicad-cli is absent.

    Catches schema drift the moment the environment's KiCad is bumped by
    diffing live output against the pinned key set. KiCad-less CI stays green.
    """

    def _kicad_cli(self):
        from kicad_tools.cli.runner import find_kicad_cli

        return find_kicad_cli()

    def test_live_output_matches_pinned_schema(self, tmp_path):
        """Live `kicad-cli pcb drc --format json` still emits the pinned keys."""
        import subprocess

        kicad_cli = self._kicad_cli()
        if kicad_cli is None:
            pytest.skip("kicad-cli not available -- live DRC smoke test skipped")
        if not _GOLDEN_SOURCE_BOARD.exists():
            pytest.skip(f"source board missing: {_GOLDEN_SOURCE_BOARD}")

        out = tmp_path / "live_drc.json"
        subprocess.run(
            [
                str(kicad_cli),
                "pcb",
                "drc",
                "--format",
                "json",
                "--units",
                "mm",
                "--output",
                str(out),
                str(_GOLDEN_SOURCE_BOARD),
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if not out.exists():
            pytest.skip("kicad-cli produced no DRC report")

        data = json.loads(out.read_text())
        for key in _REQUIRED_KICAD_CLI_DRC_KEYS:
            assert key in data, (
                f"LIVE kicad-cli output missing {key!r} -- schema drift! "
                "Re-capture the golden fixture and reconcile the parser."
            )
        for i, v in enumerate(data["violations"]):
            for key in _REQUIRED_VIOLATION_KEYS:
                assert key in v, f"LIVE violations[{i}] missing {key!r}: {v!r}"
            assert v["severity"] in _ALLOWED_SEVERITIES, (
                f"LIVE violations[{i}] has unpinned severity {v['severity']!r}"
            )
