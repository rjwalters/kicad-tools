"""Tests for the #4674 machine-output sweep (mixed-family long-tail batch).

Issue #4674 (mechanical follow-up to #4543) adds the canonical
``--format json`` idiom to the prose-only subcommands.  Batch 1 swept the
grouped families (``tests/test_format_json_sweep.py``) and batch 2 the 16
mutating ``sch`` leaves (``tests/test_format_json_sweep_sch.py``).

**This batch finishes the four families whose siblings already spoke JSON
but which each still had prose-only holdouts** -- ten surfaces:

* ``datasheet`` -- ``cache``, ``convert``, ``download``
* ``lib``       -- ``create-symbol-lib``, ``create-footprint-lib``,
                   ``generate-footprint`` (the three "not yet implemented"
                   placeholders: under JSON the *non-implementation* is the
                   payload, with the exit code unchanged at 2)
* ``parts``     -- ``cache``, ``sync-catalog``
* ``pcb``       -- ``export-dsn``, ``import-ses``

Same conventions as the two sibling modules (a separate file per batch so
concurrent batches never conflict on a shared ``SWEPT_SURFACES`` literal):

* Outer-parser surface: every swept leaf accepts ``--format`` with a
  ``json`` choice.
* Shim forwarding: the outer ``--format json`` reaches the inner parser
  argv for the argv-reserializing families (``datasheet``, ``parts``) --
  the drift bug class ``tests/test_cli_parser_drift.py`` exists for -- and
  the default (text) invocation must NOT forward it.
* Emission: a single valid JSON document on stdout, byte-identical across
  two runs on the same input, with structure assertions rather than
  byte-golden payloads; ``{"error": ...}`` documents on failure with the
  exit codes unchanged.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.parser import create_parser

FIXTURES = Path(__file__).parent / "fixtures"

# Every subcommand swept by this batch, as (command path, minimal extra argv).
SWEPT_SURFACES: dict[str, list[str]] = {
    "datasheet cache": [],
    "datasheet convert": ["ds.pdf"],
    "datasheet download": ["STM32F103"],
    "lib create-footprint-lib": ["out.pretty"],
    "lib create-symbol-lib": ["out.kicad_sym"],
    "lib generate-footprint": ["out.pretty", "soic"],
    "parts cache": [],
    "parts sync-catalog": [],
    "pcb export-dsn": ["b.kicad_pcb"],
    "pcb import-ses": ["b.kicad_pcb", "routes.ses"],
}


def _iter_leaves(parser, path=()):
    subactions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subactions:
        yield path, parser
        return
    for subaction in subactions:
        for name, sub in subaction.choices.items():
            yield from _iter_leaves(sub, (*path, name))


@pytest.fixture(scope="module")
def leaves():
    return {" ".join(path): leaf for path, leaf in _iter_leaves(create_parser())}


# ---------------------------------------------------------------------------
# Outer-parser surface guard
# ---------------------------------------------------------------------------


class TestOuterSurface:
    @pytest.mark.parametrize("command", sorted(SWEPT_SURFACES))
    def test_leaf_has_format_json_choice(self, leaves, command):
        """Each swept leaf declares --format with a json choice."""
        leaf = leaves.get(command)
        assert leaf is not None, f"outer parser has no leaf {command!r}"
        for action in leaf._actions:
            if "--format" in action.option_strings:
                assert action.choices and "json" in action.choices, (
                    f"kct {command} has --format without a 'json' choice "
                    f"(choices={action.choices}); regresses #4674"
                )
                break
        else:
            pytest.fail(f"kct {command} lost its --format flag (regresses #4674)")

    @pytest.mark.parametrize("command", sorted(SWEPT_SURFACES))
    def test_parse_accepts_format_json(self, command):
        """`kct <command> ... --format json` parses on the real outer parser."""
        argv = [*command.split(), *SWEPT_SURFACES[command], "--format", "json"]
        args = create_parser().parse_args(argv)
        assert args.format == "json"

    @pytest.mark.parametrize("command", sorted(SWEPT_SURFACES))
    def test_default_format_is_text(self, command):
        """The default stays text, so no existing invocation changes shape."""
        args = create_parser().parse_args([*command.split(), *SWEPT_SURFACES[command]])
        assert args.format == "text"


# ---------------------------------------------------------------------------
# Shim forwarding (outer --format json must reach the inner parser argv)
# ---------------------------------------------------------------------------


class TestShimForwarding:
    @pytest.mark.parametrize(
        "command",
        [c for c in sorted(SWEPT_SURFACES) if c.startswith("datasheet ")],
    )
    def test_datasheet_shim_forwards_format(self, command):
        from kicad_tools.cli.commands.datasheet import run_datasheet_command

        argv = [*command.split(), *SWEPT_SURFACES[command], "--format", "json"]
        args = create_parser().parse_args(argv)
        with patch("kicad_tools.cli.datasheet_cmd.main", return_value=0) as inner:
            assert run_datasheet_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert "--format" in sub_argv, f"datasheet shim dropped --format for {command}: {sub_argv}"
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    @pytest.mark.parametrize(
        "command",
        [c for c in sorted(SWEPT_SURFACES) if c.startswith("parts ")],
    )
    def test_parts_shim_forwards_format(self, command):
        from kicad_tools.cli.commands.parts import run_parts_command

        argv = [*command.split(), *SWEPT_SURFACES[command], "--format", "json"]
        args = create_parser().parse_args(argv)
        with patch("kicad_tools.cli.parts_cmd.main", return_value=0) as inner:
            assert run_parts_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert "--format" in sub_argv, f"parts shim dropped --format for {command}: {sub_argv}"
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    def test_shims_omit_format_for_text_default(self):
        """Default (text) invocations must not forward --format (behaviour pin)."""
        from kicad_tools.cli.commands.datasheet import run_datasheet_command
        from kicad_tools.cli.commands.parts import run_parts_command

        args = create_parser().parse_args(["datasheet", "cache"])
        with patch("kicad_tools.cli.datasheet_cmd.main", return_value=0) as inner:
            run_datasheet_command(args)
        assert "--format" not in inner.call_args[0][0]

        args = create_parser().parse_args(["parts", "sync-catalog"])
        with patch("kicad_tools.cli.parts_cmd.main", return_value=0) as inner:
            run_parts_command(args)
        assert "--format" not in inner.call_args[0][0]

    def test_parts_cache_inner_accepts_format_on_either_side(self):
        """The inner ``parts cache`` subparser tree must not eat --format.

        ``cache`` is the only swept surface whose inner parser has its own
        subparsers, so an action subparser with a plain ``text`` default
        would clobber the value parsed by the ``cache`` parser.  Both
        orderings have to resolve to json.
        """
        from kicad_tools.cli.parts_cmd import main as parts_main

        seen = []
        with patch("kicad_tools.parts.PartsCache", side_effect=AssertionError("unused")):
            for argv in (
                ["cache", "--format", "json", "stats"],
                ["cache", "stats", "--format", "json"],
            ):
                with patch("kicad_tools.cli.parts_cmd._cache", return_value=0) as handler:
                    parts_main(argv)
                seen.append(handler.call_args[0][0].format)
        assert seen == ["json", "json"]


# ---------------------------------------------------------------------------
# lib placeholders: the non-implementation is the payload
# ---------------------------------------------------------------------------


class TestLibPlaceholderEmission:
    def _run(self, argv, capsys):
        from kicad_tools.cli.commands.library import run_lib_command

        rc = run_lib_command(create_parser().parse_args(argv))
        return rc, capsys.readouterr().out

    @pytest.mark.parametrize(
        "command",
        [c for c in sorted(SWEPT_SURFACES) if c.startswith("lib ")],
    )
    def test_json_document_and_exit_code(self, command, capsys):
        argv = [*command.split(), *SWEPT_SURFACES[command], "--format", "json"]
        rc, out = self._run(argv, capsys)
        assert rc == 2, "the 'not implemented' exit code must not change"
        payload = json.loads(out)
        assert payload["command"] == command.split()[1]
        assert payload["implemented"] is False
        assert payload["success"] is False
        assert "not yet implemented" in payload["error"]
        assert payload["tracking_issue"].startswith("https://github.com/")

    def test_generate_footprint_reports_its_parameters(self, capsys):
        rc, out = self._run(
            [
                "lib",
                "generate-footprint",
                "out.pretty",
                "soic",
                "--pins",
                "8",
                "--pitch",
                "1.27",
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 2
        payload = json.loads(out)
        assert payload["library"] == "out.pretty"
        assert payload["type"] == "soic"
        # Unset options are omitted rather than emitted as nulls.
        assert payload["parameters"] == {"pins": 8, "pitch": 1.27}

    def test_json_is_deterministic(self, capsys):
        argv = ["lib", "create-symbol-lib", "out.kicad_sym", "--format", "json"]
        _, first = self._run(argv, capsys)
        _, second = self._run(argv, capsys)
        assert first == second

    def test_text_default_unchanged(self, capsys):
        rc, out = self._run(["lib", "create-symbol-lib", "out.kicad_sym"], capsys)
        assert rc == 2
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# datasheet emission
# ---------------------------------------------------------------------------


class _StubDatasheet:
    part_number = "STM32F103"
    manufacturer = "ST"
    local_path = Path("/cache/STM32F103.pdf")
    source_url = "https://example.invalid/ds.pdf"
    source = "stub"
    file_size = 2048
    file_size_mb = 0.001953125


class _StubDatasheetManager:
    """Minimal stand-in so the tests never touch the real user cache."""

    cleared = 0

    def __init__(self, *args, **kwargs):
        self.cache = self

    def download_by_part(self, part, output_dir=None, force=False):
        return _StubDatasheet()

    def cache_stats(self):
        return {
            "cache_dir": Path("/cache/datasheets"),
            "total_count": 2,
            "valid_count": 1,
            "expired_count": 1,
            "total_size_mb": 1.5,
            "ttl_days": 90,
            "sources": {"octopart": 1, "digikey": 1},
        }

    def clear_cache(self, older_than_days=None):
        return 7 if older_than_days else 9

    def clear_expired(self):
        return 3


@pytest.fixture
def stub_datasheet_manager():
    pytest.importorskip("kicad_tools.datasheet.manager")
    with patch("kicad_tools.datasheet.manager.DatasheetManager", _StubDatasheetManager):
        yield


def _run_datasheet(argv, capsys):
    from kicad_tools.cli.commands.datasheet import run_datasheet_command

    rc = run_datasheet_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


class TestDatasheetEmission:
    def test_cache_stats_document(self, stub_datasheet_manager, capsys):
        rc, out = _run_datasheet(["datasheet", "cache", "stats", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "cache"
        assert payload["action"] == "stats"
        assert payload["total_count"] == 2
        assert payload["ttl_days"] == 90
        # sources are sorted for determinism
        assert list(payload["sources"]) == sorted(payload["sources"])
        assert payload["success"] is True

    def test_cache_stats_is_deterministic(self, stub_datasheet_manager, capsys):
        argv = ["datasheet", "cache", "stats", "--format", "json"]
        _, first = _run_datasheet(argv, capsys)
        _, second = _run_datasheet(argv, capsys)
        assert first == second

    def test_cache_clear_reports_count(self, stub_datasheet_manager, capsys):
        rc, out = _run_datasheet(["datasheet", "cache", "clear", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload == {
            "action": "clear",
            "cleared": 9,
            "command": "cache",
            "older_than_days": None,
            "success": True,
        }

    def test_cache_clear_honours_older_than(self, stub_datasheet_manager, capsys):
        rc, out = _run_datasheet(
            ["datasheet", "cache", "clear", "--older-than", "30", "--format", "json"], capsys
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["older_than_days"] == 30
        assert payload["cleared"] == 7

    def test_cache_text_default_unchanged(self, stub_datasheet_manager, capsys):
        rc, out = _run_datasheet(["datasheet", "cache", "stats"], capsys)
        assert rc == 0
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)

    def test_download_document(self, stub_datasheet_manager, capsys):
        rc, out = _run_datasheet(["datasheet", "download", "STM32F103", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "download"
        assert payload["part"] == "STM32F103"
        assert payload["path"].endswith("STM32F103.pdf")
        assert payload["file_size_bytes"] == 2048
        assert payload["source"] == "stub"
        assert payload["success"] is True

    def test_download_failure_is_json_document(self, capsys):
        pytest.importorskip("kicad_tools.datasheet.manager")

        class _Boom(_StubDatasheetManager):
            def download_by_part(self, part, output_dir=None, force=False):
                raise RuntimeError("no source responded")

        with patch("kicad_tools.datasheet.manager.DatasheetManager", _Boom):
            rc, out = _run_datasheet(
                ["datasheet", "download", "NOPART", "--format", "json"], capsys
            )
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert "no source responded" in payload["error"]

    def test_convert_missing_pdf_is_json_document(self, tmp_path, capsys):
        rc, out = _run_datasheet(
            ["datasheet", "convert", str(tmp_path / "nope.pdf"), "--format", "json"], capsys
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "convert"
        assert payload["success"] is False
        # Either the PDF is missing or the optional parser dependency is --
        # both must arrive as one structured document, never as prose.
        assert payload["error"]

    def test_convert_carries_markdown_when_no_output_file(self, tmp_path, capsys):
        pytest.importorskip("kicad_tools.datasheet.parser")

        class _StubParser:
            def __init__(self, path):
                self.path = path

            def to_markdown(self, pages=None):
                return "# Title\n\nbody\n"

        pdf = tmp_path / "ds.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")

        with patch("kicad_tools.datasheet.parser.DatasheetParser", _StubParser):
            rc, out = _run_datasheet(["datasheet", "convert", str(pdf), "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["output"] is None
        assert payload["markdown"] == "# Title\n\nbody\n"

    def test_convert_reports_output_path_instead_of_markdown(self, tmp_path, capsys):
        pytest.importorskip("kicad_tools.datasheet.parser")

        class _StubParser:
            def __init__(self, path):
                self.path = path

            def to_markdown(self, pages=None):
                return "# Title\n"

        pdf = tmp_path / "ds.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        out_file = tmp_path / "ds.md"

        with patch("kicad_tools.datasheet.parser.DatasheetParser", _StubParser):
            rc, out = _run_datasheet(
                ["datasheet", "convert", str(pdf), "-o", str(out_file), "--format", "json"],
                capsys,
            )
        assert rc == 0
        payload = json.loads(out)
        assert payload["output"] == str(out_file)
        assert "markdown" not in payload
        assert out_file.read_text() == "# Title\n"


# ---------------------------------------------------------------------------
# parts emission
# ---------------------------------------------------------------------------


class _StubPartsCache:
    def __init__(self, *args, **kwargs):
        pass

    def stats(self):
        return {
            "db_path": Path("/cache/parts.db"),
            "total": 12,
            "valid": 10,
            "expired": 2,
            "ttl_days": 7,
            "oldest": "2026-01-01T00:00:00",
            "newest": "2026-01-02T00:00:00",
            "categories": {"resistor": 7, "capacitor": 5},
        }

    def clear(self):
        return 12

    def clear_expired(self):
        return 2


def _run_parts(argv, capsys):
    from kicad_tools.cli.commands.parts import run_parts_command

    rc = run_parts_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


class TestPartsEmission:
    @pytest.fixture(autouse=True)
    def _stub_cache(self):
        pytest.importorskip("kicad_tools.parts")
        with patch("kicad_tools.parts.PartsCache", _StubPartsCache):
            yield

    def test_cache_stats_document(self, capsys):
        rc, out = _run_parts(["parts", "cache", "stats", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "cache"
        assert payload["action"] == "stats"
        assert payload["total"] == 12
        assert list(payload["categories"]) == sorted(payload["categories"])
        assert payload["success"] is True

    def test_cache_stats_is_deterministic(self, capsys):
        argv = ["parts", "cache", "stats", "--format", "json"]
        _, first = _run_parts(argv, capsys)
        _, second = _run_parts(argv, capsys)
        assert first == second

    def test_cache_clear_reports_count(self, capsys):
        rc, out = _run_parts(["parts", "cache", "clear", "--format", "json"], capsys)
        assert rc == 0
        assert json.loads(out) == {
            "action": "clear",
            "cleared": 12,
            "command": "cache",
            "success": True,
        }

    def test_cache_text_default_unchanged(self, capsys):
        rc, out = _run_parts(["parts", "cache", "stats"], capsys)
        assert rc == 0
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)

    def test_sync_catalog_document(self, tmp_path, capsys):
        pytest.importorskip("kicad_tools.parts.jlcparts_catalog")
        dest = tmp_path / "catalog.sqlite3"
        calls = {}

        def _fake_sync(base_url, force, progress):
            calls["progress"] = progress
            return dest

        with patch("kicad_tools.parts.jlcparts_catalog.sync_catalog", _fake_sync):
            rc, out = _run_parts(["parts", "sync-catalog", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "sync-catalog"
        assert payload["path"] == str(dest)
        assert payload["force"] is False
        assert payload["success"] is True
        assert calls["progress"] is False, "progress chatter must not pollute the JSON document"

    def test_sync_catalog_failure_is_json_document(self, capsys):
        pytest.importorskip("kicad_tools.parts.jlcparts_catalog")

        def _boom(base_url, force, progress):
            raise RuntimeError("network down")

        with patch("kicad_tools.parts.jlcparts_catalog.sync_catalog", _boom):
            rc, out = _run_parts(["parts", "sync-catalog", "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert "network down" in payload["error"]


# ---------------------------------------------------------------------------
# pcb export-dsn / import-ses emission
# ---------------------------------------------------------------------------


def _run_pcb(argv, capsys):
    from kicad_tools.cli.commands.pcb import run_pcb_command

    rc = run_pcb_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


class TestPcbExchangeEmission:
    BOARD = FIXTURES / "routing-diagnostic.kicad_pcb"

    def test_export_dsn_document(self, tmp_path, capsys):
        out_file = tmp_path / "board.dsn"
        rc, out = _run_pcb(
            [
                "pcb",
                "export-dsn",
                str(self.BOARD),
                "-o",
                str(out_file),
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "export-dsn"
        assert payload["output"] == str(out_file)
        assert payload["layers"] >= 1
        assert payload["nets"] >= 1
        assert payload["components"] >= 1
        assert payload["success"] is True
        assert out_file.exists()

    def test_export_dsn_is_deterministic(self, tmp_path, capsys):
        argv = [
            "pcb",
            "export-dsn",
            str(self.BOARD),
            "-o",
            str(tmp_path / "board.dsn"),
            "--format",
            "json",
        ]
        _, first = _run_pcb(argv, capsys)
        _, second = _run_pcb(argv, capsys)
        assert first == second

    def test_export_dsn_text_default_unchanged(self, tmp_path, capsys):
        rc, out = _run_pcb(
            ["pcb", "export-dsn", str(self.BOARD), "-o", str(tmp_path / "b.dsn")], capsys
        )
        assert rc == 0
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)

    def test_missing_board_is_json_document(self, tmp_path, capsys):
        """The shim's shared file guard runs before any handler (#4674)."""
        rc, out = _run_pcb(
            ["pcb", "export-dsn", str(tmp_path / "nope.kicad_pcb"), "--format", "json"], capsys
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "export-dsn"
        assert payload["success"] is False
        assert "File not found" in payload["error"]

    def test_missing_board_text_default_unchanged(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        args = create_parser().parse_args(["pcb", "export-dsn", str(tmp_path / "nope.kicad_pcb")])
        assert run_pcb_command(args) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "File not found" in captured.err

    def test_import_ses_missing_ses_is_json_document(self, tmp_path, capsys):
        rc, out = _run_pcb(
            [
                "pcb",
                "import-ses",
                str(self.BOARD),
                str(tmp_path / "nope.ses"),
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "import-ses"
        assert payload["success"] is False
        assert "SES file not found" in payload["error"]

    def test_import_ses_document(self, tmp_path, capsys):
        """A successful import emits the wire/via counts as one document."""
        ses = tmp_path / "routes.ses"
        ses.write_text("(session routes.ses)\n")
        out_pcb = tmp_path / "routed.kicad_pcb"

        class _StubImporter:
            wires = [1, 2, 3]
            vias = [1]

            def __init__(self, path):
                self.path = path

            def parse(self):
                return None

            def merge_into(self, pcb, output=None):
                Path(output or pcb).write_text("stub")

        with patch("kicad_tools.export.ses.SESToKiCadImporter", _StubImporter):
            rc, out = _run_pcb(
                [
                    "pcb",
                    "import-ses",
                    str(self.BOARD),
                    str(ses),
                    "-o",
                    str(out_pcb),
                    "--format",
                    "json",
                ],
                capsys,
            )
        assert rc == 0
        payload = json.loads(out)
        assert payload["wires"] == 3
        assert payload["vias"] == 1
        assert payload["output"] == str(out_pcb)
        assert payload["success"] is True
