"""Smoke test for the softstart rev B schematic generator (Issue #3343 P2).

This test verifies that ``boards/external/softstart/generate_design.py``
successfully produces a schematic file with rev B topology and structure:

- Custom UCC27211 symbol resolved (no symbol lookup failures)
- All P1 blocks instantiated (BackToBackFETPair, UCC27211GateDriver,
  PrechargeSubsystem)
- MCU upgraded to STM32G031K8Tx LQFP-32
- Rev B nets present (BUS_LINE, SRC_POS/NEG, GATE_*_A/B, V_BUS_DVDT,
  V_BANK_POS/NEG_SENSE, OC_TRIP, PRECHARGE_POS/NEG, VGATE)
- All new rev B components present (LM7812, MCP6001, LM393, INA180A3,
  4× IRFB4110, 2× UCC27211, 2× AO3400, 2× 2N7002, 4× SMBJ18A, 4× 10k bleeders)

The schematic is generated to a tmp dir and parsed to count symbols / nets.

ERC clean is NOT asserted here — P2 ships with 2 documented residual
errors (see PR description for issue #3343 P2).
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
RECIPE_PATH = REPO_ROOT / "boards" / "external" / "softstart" / "generate_design.py"


@pytest.fixture(scope="module")
def generated_schematic(tmp_path_factory):
    """Run generate_design.py and return path to the produced schematic."""
    output_dir = tmp_path_factory.mktemp("softstart_revb_p2")
    result = subprocess.run(
        [sys.executable, str(RECIPE_PATH), str(output_dir)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode not in (0, 1):
        # 0 = ERC pass, 1 = ERC fail; both are acceptable for the smoke test
        # since we're verifying generation succeeded, not ERC clean.
        pytest.fail(
            f"Recipe failed with return code {result.returncode}\n"
            f"stdout: {result.stdout[:2000]}\n"
            f"stderr: {result.stderr[:2000]}"
        )
    sch_path = output_dir / "softstart.kicad_sch"
    assert sch_path.exists(), f"Recipe didn't produce {sch_path}"
    return sch_path


def test_schematic_file_exists(generated_schematic: Path):
    """The recipe produced a non-empty schematic file."""
    assert generated_schematic.stat().st_size > 10000


def test_custom_ucc27211_symbol_referenced(generated_schematic: Path):
    """The schematic includes the custom UCC27211 symbol from softstart_custom lib."""
    content = generated_schematic.read_text()
    assert "softstart_custom:UCC27211" in content


def test_lqfp32_mcu_present(generated_schematic: Path):
    """The schematic upgraded the MCU to STM32G031K8Tx (LQFP-32)."""
    content = generated_schematic.read_text()
    assert "MCU_ST_STM32G0:STM32G031K8Tx" in content
    # Old TSSOP-20 part should be gone
    assert "STM32G031F6Px" not in content


def test_revb_revision_field(generated_schematic: Path):
    """The schematic title block declares revision B."""
    content = generated_schematic.read_text()
    # KiCad emits `(rev "B")` in the title_block
    assert '(rev "B")' in content


def test_new_revb_blocks_present(generated_schematic: Path):
    """All P1 block types are instantiated by the rev B recipe."""
    content = generated_schematic.read_text()
    # Back-to-back FET pairs (Q1A, Q1B, Q2A, Q2B all present)
    for ref in ("Q1A", "Q1B", "Q2A", "Q2B"):
        assert f'"{ref}"' in content, f"Missing FET reference {ref}"
    # UCC27211 gate drivers (U5 = positive, U6 = negative)
    assert content.count("softstart_custom:UCC27211") >= 2
    # Precharge subsystems (Q5+R20 = positive bank, Q6+R21 = negative)
    for ref in ("Q5", "Q6", "R20", "R21"):
        assert f'"{ref}"' in content, f"Missing precharge reference {ref}"
    # Gate failsafe 2N7002s
    for ref in ("Q7", "Q8"):
        assert f'"{ref}"' in content, f"Missing failsafe reference {ref}"
    # LM393 OC comparator
    assert "Comparator:LM393" in content
    # MCP6001 bus envelope buffer
    assert "Amplifier_Operational:MCP6001" in content
    # LM7812 12V regulator
    assert "Regulator_Linear:LM7812" in content
    # INA180A3 (rev B, 100 V/V gain)
    assert "Amplifier_Current:INA180A3" in content
    # SMBJ18A TVS clamps (4 instances on gate protection)
    assert content.count("D_TVS1") >= 1
    assert content.count("D_TVS4") >= 1


def test_revb_nets_present(generated_schematic: Path):
    """Rev B nets are labelled in the schematic."""
    content = generated_schematic.read_text()
    expected_nets = [
        "BUS_LINE",
        "SRC_POS",
        "SRC_NEG",
        "GATE_POS_A",
        "GATE_POS_B",
        "GATE_NEG_A",
        "GATE_NEG_B",
        "V_BUS_DVDT",
        "V_BANK_POS_SENSE",
        "V_BANK_NEG_SENSE",
        "OC_TRIP",
        "PRECHARGE_POS",
        "PRECHARGE_NEG",
        "VGATE",
    ]
    for net in expected_nets:
        assert f'"{net}"' in content, f"Missing net label {net}"


def test_kelvin_source_labels_present(generated_schematic: Path):
    """SRC_POS / SRC_NEG Kelvin source nets are explicitly labelled.

    These are load-bearing for rev B's UCC27211 Kelvin-source topology —
    the driver's VSS pin must tie to the BackToBackFETPair's common-source
    node, not to power GND.  The label is the visible artifact of this
    intent in the netlist.
    """
    content = generated_schematic.read_text()
    # Each Kelvin net should appear in multiple labels (FET sources +
    # driver VSS + driver bypass cap GND).
    assert content.count('"SRC_POS"') >= 2
    assert content.count('"SRC_NEG"') >= 2


def test_no_rev_a_discharge_topology(generated_schematic: Path):
    """The rev A single-FET discharge nets are gone."""
    content = generated_schematic.read_text()
    # Rev A used GATE_POS / GATE_NEG (single net per FET); rev B uses
    # GATE_POS_A/B and GATE_NEG_A/B (4 nets, one per FET in back-to-back pair).
    # Direct string match for the rev-A net names.
    for old_net in ('"GATE_POS"', '"GATE_NEG"'):
        assert old_net not in content, f"Rev A net {old_net} should be gone from rev B schematic"


def test_recipe_imports_p1_classes():
    """The recipe imports the P1 blocks (PR #3344)."""
    text = RECIPE_PATH.read_text()
    for cls in ("BackToBackFETPair", "UCC27211GateDriver", "PrechargeSubsystem"):
        assert cls in text, f"Recipe doesn't import {cls}"


def test_recipe_registers_custom_symbol_lib():
    """The recipe calls Schematic(local_symbol_libs=...) for softstart_custom."""
    text = RECIPE_PATH.read_text()
    assert "local_symbol_libs" in text
    assert "softstart_custom.kicad_sym" in text
