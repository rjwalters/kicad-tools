"""Tests for the #4674 machine-output sweep -- ``sch`` mutating family (batch 2).

Batch 1 (PR #4727, ``tests/test_format_json_sweep.py``) swept the grouped
families whose whole family is served by one inner module.  This batch sweeps
the 16 mutating ``kct sch`` leaves, which are unusual in that each lives in its
own inner module:

    add-bypass-cap  add-component  add-junction  add-label  add-no-connect
    add-pull-resistor  add-wire  disconnect  insert-inline  reconnect-pin
    replace  set-footprint  set-label-direction  set-reference
    set-symbol-property  set-value

They all route through :mod:`kicad_tools.cli.sch_json`, which emits one
deterministic change-summary document and suppresses the prose report.

Covered here, mirroring the batch-1 conventions:

* Outer-parser surface: every swept leaf declares ``--format`` with a ``json``
  choice, and the real outer parser accepts ``--format json``.
* Shim forwarding: ``commands/schematic.py`` reaches the inner surface for both
  shim shapes -- argv re-serialization (12 leaves) and direct ``run_*`` keyword
  calls (4 leaves) -- and does *not* forward on the text default.
* Emission: hermetic ``--dry-run`` paths emit a single valid JSON document on
  stdout, byte-identical across two runs (determinism), with structure
  assertions rather than byte-golden payloads.
* Error paths emit ``{"error": ...}`` documents with unchanged exit codes.
* Text mode is unchanged (still prose, still not JSON).
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.parser import create_parser

FIXTURE_SCH = Path("tests/fixtures/simple_rc.kicad_sch")

# ---------------------------------------------------------------------------
# Outer-parser surface guard
# ---------------------------------------------------------------------------

# Every sch leaf swept by this batch, as (leaf name, minimal extra argv after
# the schematic positional).
SWEPT_SCH_SURFACES: dict[str, list[str]] = {
    "add-bypass-cap": ["--ref", "U1", "--pin", "1"],
    "add-component": ["--lib-id", "Device:R", "--at", "100", "100"],
    "add-junction": ["--at", "100", "100"],
    "add-label": ["--type", "global", "--name", "NET1", "--at", "100", "100"],
    "add-no-connect": ["--auto"],
    "add-pull-resistor": ["--ref", "U1", "--pin", "1", "--direction", "up", "--value", "10k"],
    "add-wire": ["--from", "100", "100", "--to", "120", "100"],
    "disconnect": ["--ref", "U1", "--pin", "1"],
    "insert-inline": ["--lib-id", "Device:R", "--near", "100", "40"],
    "reconnect-pin": ["--ref", "U1", "--pin", "1", "--to-net", "GND"],
    "replace": ["U1", "Device:C_Small"],
    "set-footprint": ["--ref", "U1", "--footprint", "Device:R_0402"],
    "set-label-direction": ["--name", "NET1", "--shape", "input"],
    "set-reference": ["--ref", "U1", "--new-ref", "U9"],
    "set-symbol-property": ["--ref", "U1", "--property", "dnp", "--value", "yes"],
    "set-value": ["--ref", "U1", "--value", "10k"],
}

# The 12 leaves whose shim re-serializes an argv for an inner ``main()``,
# mapped to the inner module path the shim imports.
ARGV_SHIM_LEAVES: dict[str, str] = {
    "add-bypass-cap": "kicad_tools.cli.sch_add_bypass_cap.main",
    "add-component": "kicad_tools.cli.sch_add_component.main",
    "add-junction": "kicad_tools.cli.sch_add_junction.main",
    "add-label": "kicad_tools.cli.sch_add_label.main",
    "add-no-connect": "kicad_tools.cli.sch_add_no_connect.main",
    "add-pull-resistor": "kicad_tools.cli.sch_add_pull_resistor.main",
    "add-wire": "kicad_tools.cli.sch_add_wire.main",
    "disconnect": "kicad_tools.cli.sch_disconnect.main",
    "insert-inline": "kicad_tools.cli.sch_insert_inline.main",
    "reconnect-pin": "kicad_tools.cli.sch_reconnect_pin.main",
    "replace": "kicad_tools.cli.sch_replace_symbol.main",
    "set-label-direction": "kicad_tools.cli.sch_set_label_direction.main",
}

# The 4 leaves whose shim calls a ``run_*`` helper with keyword arguments.
DIRECT_CALL_LEAVES: dict[str, str] = {
    "set-footprint": "kicad_tools.cli.sch_set_footprint.run_set_footprint",
    "set-reference": "kicad_tools.cli.sch_set_reference.run_set_reference",
    "set-symbol-property": "kicad_tools.cli.sch_set_symbol_property.run_set_symbol_property",
    "set-value": "kicad_tools.cli.sch_set_value.run_set_value",
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


def _argv(leaf: str, schematic: str | Path) -> list[str]:
    return ["sch", leaf, str(schematic), *SWEPT_SCH_SURFACES[leaf]]


class TestOuterSurface:
    def test_batch_covers_sixteen_leaves(self):
        assert len(SWEPT_SCH_SURFACES) == 16
        assert set(ARGV_SHIM_LEAVES) | set(DIRECT_CALL_LEAVES) == set(SWEPT_SCH_SURFACES)

    @pytest.mark.parametrize("leaf", sorted(SWEPT_SCH_SURFACES))
    def test_leaf_has_format_json_choice(self, leaves, leaf):
        """Each swept leaf declares --format with a json choice."""
        parser = leaves.get(f"sch {leaf}")
        assert parser is not None, f"outer parser has no leaf 'sch {leaf}'"
        for action in parser._actions:
            if "--format" in action.option_strings:
                assert action.choices and "json" in action.choices, (
                    f"kct sch {leaf} has --format without a 'json' choice "
                    f"(choices={action.choices}); regresses #4674"
                )
                break
        else:
            pytest.fail(f"kct sch {leaf} lost its --format flag (regresses #4674)")

    @pytest.mark.parametrize("leaf", sorted(SWEPT_SCH_SURFACES))
    def test_parse_accepts_format_json(self, leaf):
        """`kct sch <leaf> ... --format json` parses on the real outer parser."""
        args = create_parser().parse_args([*_argv(leaf, "b.kicad_sch"), "--format", "json"])
        assert args.format == "json"

    @pytest.mark.parametrize("leaf", sorted(SWEPT_SCH_SURFACES))
    def test_format_defaults_to_text(self, leaf):
        args = create_parser().parse_args(_argv(leaf, "b.kicad_sch"))
        assert args.format == "text"


# ---------------------------------------------------------------------------
# Shim forwarding (commands/schematic.py must reach the inner surface)
# ---------------------------------------------------------------------------


class TestShimForwarding:
    """The shim's own existence check runs first, so point it at a real file."""

    @pytest.mark.parametrize("leaf", sorted(ARGV_SHIM_LEAVES))
    def test_argv_shim_forwards_format(self, leaf):
        from kicad_tools.cli.commands.schematic import run_sch_command

        args = create_parser().parse_args([*_argv(leaf, FIXTURE_SCH), "--format", "json"])
        with patch(ARGV_SHIM_LEAVES[leaf], return_value=0) as inner:
            assert run_sch_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert "--format" in sub_argv, f"sch shim dropped --format for {leaf}: {sub_argv}"
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    @pytest.mark.parametrize("leaf", sorted(ARGV_SHIM_LEAVES))
    def test_argv_shim_omits_format_for_text_default(self, leaf):
        """Default (text) invocations must not forward --format (behaviour pin)."""
        from kicad_tools.cli.commands.schematic import run_sch_command

        args = create_parser().parse_args(_argv(leaf, FIXTURE_SCH))
        with patch(ARGV_SHIM_LEAVES[leaf], return_value=0) as inner:
            assert run_sch_command(args) == 0
        assert "--format" not in inner.call_args[0][0]

    @pytest.mark.parametrize("leaf", sorted(DIRECT_CALL_LEAVES))
    def test_direct_call_shim_threads_output_format(self, leaf):
        from kicad_tools.cli.commands.schematic import run_sch_command

        args = create_parser().parse_args([*_argv(leaf, FIXTURE_SCH), "--format", "json"])
        with patch(DIRECT_CALL_LEAVES[leaf], return_value=0) as inner:
            assert run_sch_command(args) == 0
        assert inner.call_args.kwargs["output_format"] == "json"

    @pytest.mark.parametrize("leaf", sorted(DIRECT_CALL_LEAVES))
    def test_direct_call_shim_defaults_to_text(self, leaf):
        from kicad_tools.cli.commands.schematic import run_sch_command

        args = create_parser().parse_args(_argv(leaf, FIXTURE_SCH))
        with patch(DIRECT_CALL_LEAVES[leaf], return_value=0) as inner:
            assert run_sch_command(args) == 0
        assert inner.call_args.kwargs["output_format"] == "text"


# ---------------------------------------------------------------------------
# Emission (hermetic dry-run paths through the real CLI dispatch)
# ---------------------------------------------------------------------------


@pytest.fixture()
def sch(tmp_path) -> Path:
    """A writable copy of the simple RC fixture (R1, C1, six wires)."""
    target = tmp_path / "simple_rc.kicad_sch"
    shutil.copy2(FIXTURE_SCH, target)
    return target


def _run(argv, capsys) -> tuple[int, str]:
    from kicad_tools.cli.commands.schematic import run_sch_command

    rc = run_sch_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


# (leaf, extra argv) invocations that succeed hermetically against the fixture.
HERMETIC_DRY_RUNS: dict[str, list[str]] = {
    "add-junction": ["--at", "100", "100", "--dry-run"],
    "add-wire": ["--from", "100", "100", "--to", "120", "100", "--dry-run"],
    "add-label": ["--type", "global", "--name", "NET1", "--at", "100", "100", "--dry-run"],
    "add-no-connect": ["--auto", "--dry-run"],
    "disconnect": ["--ref", "R1", "--pin", "1", "--dry-run"],
    "reconnect-pin": ["--ref", "R1", "--pin", "1", "--to-net", "GND", "--dry-run"],
    "replace": ["R1", "Device:C_Small", "--dry-run"],
    "set-label-direction": ["--name", "NET1", "--shape", "input", "--dry-run"],
    "set-reference": ["--ref", "R1", "--new-ref", "R9", "--dry-run"],
    "set-symbol-property": ["--ref", "R1", "--property", "dnp", "--value", "yes", "--dry-run"],
    "set-value": ["--ref", "R1", "--value", "10k", "--dry-run"],
    "set-footprint": ["--ref", "R1", "--footprint", "Device:R_0402", "--dry-run", "--no-validate"],
}


class TestEmission:
    @pytest.mark.parametrize("leaf", sorted(HERMETIC_DRY_RUNS))
    def test_single_document_with_envelope(self, leaf, sch, capsys):
        rc, out = _run(
            ["sch", leaf, str(sch), *HERMETIC_DRY_RUNS[leaf], "--format", "json"], capsys
        )
        assert rc == 0, out
        payload = json.loads(out)  # exactly one JSON document
        assert payload["command"] == leaf
        assert payload["schematic"] == str(sch)
        assert payload["dry_run"] is True
        assert payload["success"] is True

    @pytest.mark.parametrize("leaf", sorted(HERMETIC_DRY_RUNS))
    def test_deterministic(self, leaf, sch, capsys):
        argv = ["sch", leaf, str(sch), *HERMETIC_DRY_RUNS[leaf], "--format", "json"]
        _, first = _run(argv, capsys)
        _, second = _run(argv, capsys)
        assert first == second, f"kct sch {leaf} --format json is not deterministic"

    @pytest.mark.parametrize("leaf", sorted(HERMETIC_DRY_RUNS))
    def test_text_default_is_not_json(self, leaf, sch, capsys):
        rc, out = _run(["sch", leaf, str(sch), *HERMETIC_DRY_RUNS[leaf]], capsys)
        assert rc == 0, out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)

    @pytest.mark.parametrize("leaf", sorted(HERMETIC_DRY_RUNS))
    def test_dry_run_leaves_the_schematic_untouched(self, leaf, sch, capsys):
        before = sch.read_bytes()
        _run(["sch", leaf, str(sch), *HERMETIC_DRY_RUNS[leaf], "--format", "json"], capsys)
        assert sch.read_bytes() == before


class TestPayloadStructure:
    """Spot-check the per-command change summaries (not byte-golden)."""

    def test_add_junction(self, sch, capsys):
        _, out = _run(
            [
                "sch",
                "add-junction",
                str(sch),
                "--at",
                "100",
                "100",
                "--dry-run",
                "--format",
                "json",
            ],
            capsys,
        )
        payload = json.loads(out)
        assert payload["junctions_added"] == 0  # dry run
        assert len(payload["position"]) == 2

    def test_add_wire(self, sch, capsys):
        _, out = _run(
            [
                "sch",
                "add-wire",
                str(sch),
                "--from",
                "100",
                "100",
                "--to",
                "120",
                "100",
                "--dry-run",
                "--format",
                "json",
            ],
            capsys,
        )
        payload = json.loads(out)
        assert payload["wires_added"] == 0
        assert [a["kind"] for a in payload["planned"]] == ["wire"]

    def test_set_value(self, sch, capsys):
        _, out = _run(
            ["sch", "set-value", str(sch), "--ref", "R1", "--value", "10k", "--dry-run"]
            + ["--format", "json"],
            capsys,
        )
        payload = json.loads(out)
        assert payload["changed"] == 1
        assert payload["not_found"] == []
        assert payload["files_modified"] == []

    def test_set_footprint(self, sch, capsys):
        _, out = _run(
            [
                "sch",
                "set-footprint",
                str(sch),
                "--ref",
                "R1",
                "--footprint",
                "Device:R_0402",
                "--dry-run",
                "--no-validate",
                "--format",
                "json",
            ],
            capsys,
        )
        payload = json.loads(out)
        assert payload["batch_mode"] is False
        assert payload["assignments"] == [{"reference": "R1", "footprint": "Device:R_0402"}]
        assert payload["changed"] == 1

    def test_set_reference(self, sch, capsys):
        _, out = _run(
            ["sch", "set-reference", str(sch), "--ref", "R1", "--new-ref", "R9", "--dry-run"]
            + ["--format", "json"],
            capsys,
        )
        payload = json.loads(out)
        assert payload["renames"] == [{"from": "R1", "to": "R9"}]

    def test_set_symbol_property(self, sch, capsys):
        _, out = _run(
            [
                "sch",
                "set-symbol-property",
                str(sch),
                "--ref",
                "R1",
                "--property",
                "dnp",
                "--value",
                "yes",
                "--dry-run",
                "--format",
                "json",
            ],
            capsys,
        )
        payload = json.loads(out)
        assert payload["reference"] == "R1"
        assert payload["property"] == "dnp"
        assert payload["value"] == "yes"
        assert payload["applied"] is False

    def test_replace(self, sch, capsys):
        _, out = _run(
            ["sch", "replace", str(sch), "R1", "Device:C_Small", "--dry-run", "--format", "json"],
            capsys,
        )
        payload = json.loads(out)
        assert payload["reference"] == "R1"
        assert payload["old_lib_id"] == "Device:R"
        assert payload["new_lib_id"] == "Device:C_Small"
        assert isinstance(payload["changes"], list)

    def test_reconnect_pin(self, sch, capsys):
        _, out = _run(
            ["sch", "reconnect-pin", str(sch), "--ref", "R1", "--pin", "1", "--to-net", "GND"]
            + ["--dry-run", "--format", "json"],
            capsys,
        )
        payload = json.loads(out)
        assert payload["to_net"] == "GND"
        assert payload["reconnected"] is False
        assert payload["pin"]["reference"] == "R1"

    def test_disconnect(self, sch, capsys):
        _, out = _run(
            ["sch", "disconnect", str(sch), "--ref", "R1", "--pin", "1", "--dry-run"]
            + ["--format", "json"],
            capsys,
        )
        payload = json.loads(out)
        assert payload["wires_to_remove"] >= 1
        assert payload["wires_removed"] == 0  # dry run


class TestErrorDocuments:
    def test_missing_schematic_is_json_error(self, tmp_path, capsys):
        """The shim's own existence guard honours --format json."""
        rc, out = _run(
            ["sch", "add-junction", str(tmp_path / "nope.kicad_sch")]
            + ["--at", "1", "1", "--format", "json"],
            capsys,
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert "error" in payload
        assert payload["command"] == "add-junction"

    def test_missing_schematic_text_mode_stays_prose(self, tmp_path, capsys):
        rc, out = _run(
            ["sch", "add-junction", str(tmp_path / "nope.kicad_sch"), "--at", "1", "1"], capsys
        )
        assert rc == 1
        assert out == ""  # prose goes to stderr in text mode

    def test_unresolvable_pin_is_json_error(self, sch, capsys):
        rc, out = _run(
            ["sch", "disconnect", str(sch), "--ref", "ZZ99", "--pin", "1", "--format", "json"],
            capsys,
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert "ZZ99" in payload["error"]

    def test_unknown_reference_is_json_error(self, sch, capsys):
        rc, out = _run(
            ["sch", "set-reference", str(sch), "--ref", "ZZ99", "--new-ref", "ZZ1"]
            + ["--dry-run", "--format", "json"],
            capsys,
        )
        assert rc == 1
        assert "error" in json.loads(out)

    def test_error_exit_code_matches_text_mode(self, sch, capsys):
        argv = ["sch", "disconnect", str(sch), "--ref", "ZZ99", "--pin", "1"]
        text_rc, _ = _run(argv, capsys)
        json_rc, _ = _run([*argv, "--format", "json"], capsys)
        assert text_rc == json_rc == 1


class TestApplyPathEmitsSummary:
    """A non-dry-run write still produces exactly one document."""

    def test_set_value_applied(self, sch, capsys):
        rc, out = _run(
            ["sch", "set-value", str(sch), "--ref", "R1", "--value", "22k", "--format", "json"],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["dry_run"] is False
        assert payload["changed"] == 1
        assert payload["files_modified"] == [str(sch)]
        assert '"22k"' in sch.read_text()
