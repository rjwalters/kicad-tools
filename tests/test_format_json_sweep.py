"""Tests for the #4674 machine-output sweep (grouped-family batch).

Issue #4674 (mechanical follow-up to #4543): add the canonical
``--format json`` idiom to the prose-only subcommand families.  This batch
covers the grouped families:

* ``mfr``       -- list, info, rules, compare, export-dru, apply-rules,
                   validate (and retires the dead inner ``mfr rules --json``
                   surface documented in docs/reference/machine-output.md)
* ``spec``      -- init, validate, status, decide, check
* ``benchmark`` -- run, compare (new flag) + report (gains a ``json`` choice)
* ``zones``     -- add, batch, fill, hv-keepout
* ``placement`` -- align, distribute, fix, nudge, snap

Covers, per the #4543 idiom test conventions
(``tests/test_machine_output_idiom.py``):

* Outer-parser surface: every swept leaf accepts ``--format`` with a
  ``json`` choice (regression guard so a surface cannot silently drop out).
* Shim forwarding: the outer ``--format json`` reaches the inner parser
  argv for the three argv-reserializing families (mfr, zones, placement) --
  the drift bug class ``tests/test_cli_parser_drift.py`` exists for.
* Emission: hermetic surfaces emit a single valid JSON document on stdout,
  byte-identical across two runs on the same input (determinism), with
  structure assertions rather than byte-golden payloads.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

import pytest

from kicad_tools.cli.parser import create_parser

# ---------------------------------------------------------------------------
# Outer-parser surface guard
# ---------------------------------------------------------------------------

# Every subcommand swept by this batch, as (command path, minimal extra argv).
SWEPT_SURFACES: dict[str, list[str]] = {
    "mfr list": [],
    "mfr info": ["jlcpcb"],
    "mfr rules": ["jlcpcb"],
    "mfr compare": [],
    "mfr export-dru": ["jlcpcb"],
    "mfr apply-rules": ["proj.kicad_pro", "jlcpcb"],
    "mfr validate": ["b.kicad_pcb", "jlcpcb"],
    "spec init": ["Name"],
    "spec validate": ["p.kct"],
    "spec status": ["p.kct"],
    "spec decide": ["p.kct", "--topic", "t", "--choice", "c", "--rationale", "r"],
    "spec check": ["p.kct", "item"],
    "benchmark run": [],
    "benchmark compare": ["--baseline", "b.json"],
    "benchmark report": ["results.json"],
    "zones add": ["b.kicad_pcb", "--net", "GND", "--layer", "B.Cu"],
    "zones batch": ["b.kicad_pcb", "--power-nets", "GND:B.Cu"],
    "zones fill": ["b.kicad_pcb"],
    "zones hv-keepout": ["b.kicad_pcb", "--clearance", "1.0"],
    "placement align": ["b.kicad_pcb", "--components", "R1,R2"],
    "placement distribute": ["b.kicad_pcb", "--components", "R1,R2"],
    "placement fix": ["b.kicad_pcb"],
    "placement nudge": ["b.kicad_pcb"],
    "placement snap": ["b.kicad_pcb"],
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
        # spec uses a prefixed dest to match its family convention.
        attr = "spec_format" if command.startswith("spec ") else "format"
        assert getattr(args, attr) == "json"


# ---------------------------------------------------------------------------
# Shim forwarding (outer --format json must reach the inner parser argv)
# ---------------------------------------------------------------------------


class TestShimForwarding:
    @pytest.mark.parametrize(
        "command",
        [c for c in sorted(SWEPT_SURFACES) if c.startswith("mfr ")],
    )
    def test_mfr_shim_forwards_format(self, command):
        from kicad_tools.cli.commands.manufacturer import run_mfr_command

        argv = [*command.split(), *SWEPT_SURFACES[command], "--format", "json"]
        args = create_parser().parse_args(argv)
        with patch("kicad_tools.cli.mfr.main", return_value=0) as inner:
            assert run_mfr_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert "--format" in sub_argv, f"mfr shim dropped --format for {command}: {sub_argv}"
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    @pytest.mark.parametrize(
        "command",
        [c for c in sorted(SWEPT_SURFACES) if c.startswith("zones ")],
    )
    def test_zones_shim_forwards_format(self, command):
        from kicad_tools.cli.commands.routing import run_zones_command

        argv = [*command.split(), *SWEPT_SURFACES[command], "--format", "json"]
        args = create_parser().parse_args(argv)
        with patch("kicad_tools.cli.zones_cmd.main", return_value=0) as inner:
            assert run_zones_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert "--format" in sub_argv, f"zones shim dropped --format for {command}: {sub_argv}"
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    @pytest.mark.parametrize(
        "command",
        [c for c in sorted(SWEPT_SURFACES) if c.startswith("placement ")],
    )
    def test_placement_shim_forwards_format(self, command):
        from kicad_tools.cli.commands.placement import run_placement_command

        argv = [*command.split(), *SWEPT_SURFACES[command], "--format", "json"]
        args = create_parser().parse_args(argv)
        with patch("kicad_tools.cli.placement_cmd.main", return_value=0) as inner:
            assert run_placement_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert "--format" in sub_argv, f"placement shim dropped --format for {command}: {sub_argv}"
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    def test_shims_omit_format_for_text_default(self):
        """Default (text) invocations must not forward --format (behaviour pin)."""
        from kicad_tools.cli.commands.manufacturer import run_mfr_command
        from kicad_tools.cli.commands.routing import run_zones_command

        args = create_parser().parse_args(["mfr", "rules", "jlcpcb"])
        with patch("kicad_tools.cli.mfr.main", return_value=0) as inner:
            run_mfr_command(args)
        assert "--format" not in inner.call_args[0][0]

        args = create_parser().parse_args(["zones", "fill", "b.kicad_pcb"])
        with patch("kicad_tools.cli.zones_cmd.main", return_value=0) as inner:
            run_zones_command(args)
        assert "--format" not in inner.call_args[0][0]


# ---------------------------------------------------------------------------
# Inner mfr parser: dead --json surface retired (#4543 design note)
# ---------------------------------------------------------------------------


class TestMfrEmission:
    def _run(self, argv, capsys) -> str:
        from kicad_tools.cli.mfr import main as mfr_main

        assert mfr_main(argv) in (None, 0)
        return capsys.readouterr().out

    def test_inner_rules_json_flag_retired(self):
        """The dead inner-only ``mfr rules --json`` boolean is gone.

        It was unreachable through ``kct`` (the shim never forwarded it) and
        docs/reference/machine-output.md scheduled its retirement in favour
        of the canonical --format json.
        """
        from kicad_tools.cli.mfr import main as mfr_main

        with pytest.raises(SystemExit):
            mfr_main(["rules", "jlcpcb", "--json"])

    @pytest.mark.parametrize(
        "argv",
        [
            ["list"],
            ["info", "jlcpcb"],
            ["rules", "jlcpcb", "--layers", "4"],
            ["compare"],
        ],
        ids=lambda a: a[0],
    )
    def test_json_is_single_deterministic_document(self, argv, capsys):
        first = self._run([*argv, "--format", "json"], capsys)
        second = self._run([*argv, "--format", "json"], capsys)
        assert first == second, f"mfr {argv[0]} JSON output is not deterministic"
        json.loads(first)  # single valid JSON document

    def test_list_payload_structure(self, capsys):
        payload = json.loads(self._run(["list", "--format", "json"], capsys))
        assert payload["manufacturers"], "expected at least one manufacturer"
        ids = [m["id"] for m in payload["manufacturers"]]
        assert ids == sorted(ids), "manufacturers must be sorted for determinism"
        assert {"id", "name", "supports_assembly", "parts_library"} <= set(
            payload["manufacturers"][0]
        )

    def test_rules_payload_structure(self, capsys):
        payload = json.loads(self._run(["rules", "jlcpcb", "--format", "json"], capsys))
        assert payload["manufacturer"] == "jlcpcb"
        assert payload["layers"] == 2  # inner default
        assert "min_trace_width_mm" in payload["rules"]

    def test_error_is_json_document(self, capsys):
        from kicad_tools.cli.mfr import main as mfr_main

        with pytest.raises(SystemExit) as excinfo:
            mfr_main(["info", "nosuchmfr", "--format", "json"])
        assert excinfo.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert "error" in payload

    def test_text_default_is_not_json(self, capsys):
        out = self._run(["list"], capsys)
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# spec family emission (self-contained handlers)
# ---------------------------------------------------------------------------


class TestSpecEmission:
    def _run(self, argv, capsys) -> tuple[int, str]:
        from kicad_tools.cli.commands.spec import run_spec_command

        rc = run_spec_command(create_parser().parse_args(argv))
        return rc, capsys.readouterr().out

    def test_init_validate_status_decide_roundtrip(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        spec_file = tmp_path / "proj.kct"

        rc, out = self._run(
            ["spec", "init", "Widget", "-o", str(spec_file), "--format", "json"], capsys
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload == {
            "created": str(spec_file),
            "name": "Widget",
            "template": "minimal",
        }

        rc, out = self._run(["spec", "validate", str(spec_file), "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["valid"] is True
        assert payload["errors"] == []

        rc, out = self._run(["spec", "status", str(spec_file), "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["project"]["name"] == "Widget"
        assert "phases" in payload and "completion_percent" in payload

        # status must be deterministic across runs
        rc, out2 = self._run(["spec", "status", str(spec_file), "--format", "json"], capsys)
        assert out == out2

        rc, out = self._run(
            [
                "spec",
                "decide",
                str(spec_file),
                "--topic",
                "Buck",
                "--choice",
                "LM2596",
                "--rationale",
                "cheap",
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["recorded"] is True
        assert payload["topic"] == "Buck"

        # the recorded decision shows up in status
        rc, out = self._run(["spec", "status", str(spec_file), "--format", "json"], capsys)
        assert json.loads(out)["decisions"][-1]["choice"] == "LM2596"

    def test_init_existing_file_is_json_error(self, tmp_path, capsys):
        spec_file = tmp_path / "proj.kct"
        spec_file.write_text("existing")
        rc, out = self._run(
            ["spec", "init", "Widget", "-o", str(spec_file), "--format", "json"], capsys
        )
        assert rc == 1
        assert "error" in json.loads(out)

    def test_check_error_is_json(self, tmp_path, capsys):
        rc, out = self._run(
            ["spec", "check", str(tmp_path / "missing.kct"), "item", "--format", "json"],
            capsys,
        )
        assert rc == 1
        assert "error" in json.loads(out)

    def test_text_default_unchanged(self, tmp_path, capsys):
        spec_file = tmp_path / "p.kct"
        rc, out = self._run(["spec", "init", "Widget", "-o", str(spec_file)], capsys)
        assert rc == 0
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# benchmark report --format json (upgrade of the format-nojson holdout)
# ---------------------------------------------------------------------------


class TestBenchmarkReportJson:
    @pytest.fixture()
    def results_file(self, tmp_path):
        from kicad_tools.benchmark.result import BenchmarkResult

        results = {
            "results": [
                BenchmarkResult(
                    case_name="charlieplex",
                    strategy="basic",
                    nets_total=4,
                    nets_routed=3,
                ).to_dict(),
                BenchmarkResult(
                    case_name="charlieplex",
                    strategy="negotiated",
                    nets_total=4,
                    nets_routed=4,
                ).to_dict(),
            ]
        }
        path = tmp_path / "results.json"
        path.write_text(json.dumps(results))
        return path

    def _run(self, argv, capsys) -> tuple[int, str]:
        from kicad_tools.cli.commands.benchmark import run_benchmark_command

        rc = run_benchmark_command(create_parser().parse_args(argv))
        return rc, capsys.readouterr().out

    def test_report_json_payload(self, results_file, capsys):
        rc, out = self._run(["benchmark", "report", str(results_file), "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["input"] == str(results_file)
        rows = payload["cases"]["charlieplex"]
        assert [r["strategy"] for r in rows] == ["basic", "negotiated"]
        assert rows[0]["nets_routed"] == 3

    def test_report_json_deterministic(self, results_file, capsys):
        _, first = self._run(["benchmark", "report", str(results_file), "--format", "json"], capsys)
        _, second = self._run(
            ["benchmark", "report", str(results_file), "--format", "json"], capsys
        )
        assert first == second

    def test_report_missing_file_json_error(self, tmp_path, capsys):
        rc, out = self._run(
            ["benchmark", "report", str(tmp_path / "nope.json"), "--format", "json"], capsys
        )
        assert rc == 1
        assert "error" in json.loads(out)

    def test_report_text_default_unchanged(self, results_file, capsys):
        rc, out = self._run(["benchmark", "report", str(results_file)], capsys)
        assert rc == 0
        assert "Routing Benchmark Report" in out


class TestBenchmarkRunDifficultyError:
    """Bless the single-line difficulty error shared by text and JSON modes.

    #4727 merged the historical two-line text error (``Unknown difficulty:
    X`` + ``Valid options: ...``) into one ``_fail()`` message so both modes
    share it; #4736 pins that as the intended format.  The argparse layer
    already rejects bad ``--difficulty`` values via ``choices``, so this
    internal guard is only reachable by callers that build the args
    namespace directly -- drive it that way.
    """

    _MESSAGE = "Unknown difficulty: bogus (valid options: easy, medium, hard)"

    def _run(self, fmt: str, capsys) -> tuple[int, str]:
        from types import SimpleNamespace

        from kicad_tools.cli.commands.benchmark import run_benchmark_command

        args = SimpleNamespace(
            benchmark_command="run",
            difficulty="bogus",
            format=fmt,
        )
        rc = run_benchmark_command(args)
        return rc, capsys.readouterr().out

    def test_text_mode_single_line(self, capsys):
        rc, out = self._run("text", capsys)
        assert rc == 1
        assert out.strip().splitlines() == [self._MESSAGE]

    def test_json_mode_single_error_document(self, capsys):
        rc, out = self._run("json", capsys)
        assert rc == 1
        assert json.loads(out) == {"error": self._MESSAGE}


# ---------------------------------------------------------------------------
# zones family emission (hermetic paths)
# ---------------------------------------------------------------------------

FIXTURE_PCB = "tests/fixtures/projects/multilayer_zones.kicad_pcb"


class TestZonesEmission:
    def _run(self, argv, capsys) -> tuple[int, str]:
        from kicad_tools.cli.zones_cmd import main as zones_main

        rc = zones_main(argv)
        return rc, capsys.readouterr().out

    def test_add_dry_run_json(self, capsys):
        rc, out = self._run(
            [
                "add",
                FIXTURE_PCB,
                "--net",
                "GND",
                "--layer",
                "B.Cu",
                "--dry-run",
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["zone"] == {
            "net": "GND",
            "layer": "B.Cu",
            "priority": 0,
            "clearance_mm": 0.3,
            "boundary_points": payload["zone"]["boundary_points"],
        }
        assert payload["dry_run"] is True
        assert payload["saved"] is False

    def test_add_dry_run_json_deterministic(self, capsys):
        argv = [
            "add",
            FIXTURE_PCB,
            "--net",
            "GND",
            "--layer",
            "B.Cu",
            "--dry-run",
            "--format",
            "json",
        ]
        _, first = self._run(argv, capsys)
        _, second = self._run(argv, capsys)
        assert first == second

    def test_fill_dry_run_json(self, capsys, monkeypatch):
        import kicad_tools.cli.runner as runner_mod

        monkeypatch.setattr(runner_mod, "find_kicad_cli", lambda: "/usr/bin/kicad-cli")
        rc, out = self._run(["fill", FIXTURE_PCB, "--dry-run", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["filled"] is False
        assert payload["dry_run"] is True
        assert payload["pcb"] == FIXTURE_PCB

    def test_missing_file_is_json_error(self, capsys):
        rc, out = self._run(
            ["add", "no-such.kicad_pcb", "--net", "GND", "--layer", "B.Cu", "--format", "json"],
            capsys,
        )
        assert rc == 1
        assert "error" in json.loads(out)

    def test_add_text_default_unchanged(self, capsys):
        rc, out = self._run(
            ["add", FIXTURE_PCB, "--net", "GND", "--layer", "B.Cu", "--dry-run"], capsys
        )
        assert rc == 0
        assert "Zone created:" in out


# ---------------------------------------------------------------------------
# placement family emission (hermetic dry-run paths)
# ---------------------------------------------------------------------------


class TestPlacementEmission:
    def _run(self, argv, capsys) -> tuple[int, str]:
        from kicad_tools.cli.placement_cmd import main as placement_main

        rc = placement_main(argv)
        return rc, capsys.readouterr().out

    def test_snap_dry_run_json(self, capsys):
        rc, out = self._run(["snap", FIXTURE_PCB, "--dry-run", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["dry_run"] is True
        assert payload["grid_mm"] == 0.5
        assert payload["components"] > 0
        assert {"snapped", "updated", "pcb", "output"} <= set(payload)

    def test_align_dry_run_json(self, capsys):
        rc, out = self._run(
            ["align", FIXTURE_PCB, "--components", "R1,R2", "--dry-run", "--format", "json"],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["components"] == ["R1", "R2"]
        assert payload["axis"] == "row"
        assert "aligned" in payload

    def test_nudge_dry_run_json(self, capsys):
        rc, out = self._run(["nudge", FIXTURE_PCB, "--dry-run", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert {"success", "fixes_applied", "new_conflicts", "message"} <= set(payload)

    def test_fix_dry_run_json(self, capsys):
        rc, out = self._run(["fix", FIXTURE_PCB, "--dry-run", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert {"success", "passes", "initial_conflicts", "remaining_conflicts"} <= set(payload)

    def test_missing_file_is_json_error(self, capsys):
        rc, out = self._run(["snap", "no-such.kicad_pcb", "--format", "json"], capsys)
        assert rc == 1
        assert "error" in json.loads(out)
