"""CLI tests for ``kct check --only single_pad_net``.

The original test fixture used board 05 (BLDC motor controller) which
emitted a stub PCB without its STM32G4 MCU.  Commit ``e0459593`` placed
a real STM32G431K8Tx on the board, eliminating its stub single-pad
nets.  Rather than wait for a board that exhibits single-pad-net
defects again, this test now constructs a synthetic on-disk PCB with
exactly one named-signal singleton, then drives ``kct check`` end to
end.
"""

from __future__ import annotations

import json
from pathlib import Path


_MINIMAL_PCB = """(kicad_pcb (version 20240108) (generator "test_fixture")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (42 "Eco1.User" user "User.Eco1")
    (43 "Eco2.User" user "User.Eco2")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "UART_TX")
  (footprint "Package:TEST" (layer "F.Cu")
    (at 100 100)
    (property "Reference" "U8" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000001"))
    (property "Value" "TEST" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000002"))
    (pad "10" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "UART_TX") (uuid "00000000-0000-0000-0000-000000000003"))
  )
)
"""


def _write_synthetic_pcb(tmp_path: Path) -> Path:
    """Write a minimal PCB to ``tmp_path`` with one single-pad signal net."""
    pcb_path = tmp_path / "synthetic_singleton.kicad_pcb"
    pcb_path.write_text(_MINIMAL_PCB)
    return pcb_path


class TestCheckSinglePadNetCli:
    """End-to-end CLI tests using a synthetic PCB."""

    def test_only_single_pad_net_reports_errors(self, capsys, tmp_path: Path) -> None:
        """``kct check --only single_pad_net`` exits 2 with errors."""
        from kicad_tools.cli.check_cmd import main

        pcb_path = _write_synthetic_pcb(tmp_path)
        rc = main([str(pcb_path), "--only", "single_pad_net"])
        assert rc == 2  # Errors found.

        captured = capsys.readouterr()
        # Some single-pad-net signal should be flagged on the synthetic board.
        assert "single_pad_net" in captured.out
        # The output should list at least one offending net.
        assert "Net '" in captured.out
        # UART_TX is a "real" named signal and must be reported as an error.
        assert "UART_TX" in captured.out

    def test_skip_single_pad_net_excludes_rule(self, capsys, tmp_path: Path) -> None:
        """``kct check --skip single_pad_net`` does not include this rule_id."""
        from kicad_tools.cli.check_cmd import main

        pcb_path = _write_synthetic_pcb(tmp_path)
        # We don't care about the exit code (other rules may fire on
        # this synthetic board); only that the single_pad_net rule is
        # excluded.
        main([str(pcb_path), "--skip", "single_pad_net"])

        captured = capsys.readouterr()
        assert "single_pad_net" not in captured.out

    def test_json_output_resolves_type(self, capsys, tmp_path: Path) -> None:
        """JSON output round-trips rule_id -> type without 'unknown'."""
        from kicad_tools.cli.check_cmd import main

        pcb_path = _write_synthetic_pcb(tmp_path)
        rc = main(
            [
                str(pcb_path),
                "--only",
                "single_pad_net",
                "--format",
                "json",
            ]
        )
        assert rc == 2

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert data["summary"]["errors"] >= 1
        assert data["summary"]["passed"] is False

        violations = data["violations"]
        assert len(violations) >= 1
        for v in violations:
            assert v["rule_id"] == "single_pad_net"
            # Critical: must NOT resolve to 'unknown' -- verifies the
            # ViolationType enum + alias entry are wired correctly.
            assert v["type"] == "single_pad_net"
            # The synthetic fixture's UART_TX net is a "real defect",
            # so it must be reported at error severity.
            if v["nets"] == ["UART_TX"]:
                assert v["severity"] == "error"

    def test_info_severity_in_json_for_explicit_nc(
        self, capsys, tmp_path: Path
    ) -> None:
        """An ``unconnected-(REF-PIN-PadN)`` singleton is reported at info severity.

        Issue #2613: KiCad-emitted explicit NCs are advisory only.
        """
        from kicad_tools.cli.check_cmd import main

        # Build a synthetic PCB that has exactly one ``unconnected-...``
        # net.  The rule should classify it as info (not error) and
        # therefore the CLI should exit 0 (no errors, infos don't
        # affect exit code).
        pcb_content = _MINIMAL_PCB.replace(
            '(net 1 "UART_TX")',
            '(net 1 "unconnected-(U8-NC-Pad10)")',
        ).replace(
            '(net 1 "UART_TX")',
            '(net 1 "unconnected-(U8-NC-Pad10)")',
            # second occurrence inside the pad definition is replaced too
        )
        pcb_path = tmp_path / "synthetic_nc.kicad_pcb"
        pcb_path.write_text(pcb_content)

        rc = main([str(pcb_path), "--only", "single_pad_net", "--format", "json"])
        # Exit 0: no errors, infos do not affect exit code.
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Confirm the violation is reported at info severity.
        infos = [v for v in data["violations"] if v["severity"] == "info"]
        assert len(infos) >= 1
        assert any("explicit no-connect" in v["message"].lower() for v in infos)
