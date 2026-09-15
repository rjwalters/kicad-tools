"""Validation imports must not activate unrelated DRC/router subsystems."""

import subprocess
import sys
import textwrap

import pytest


def run_fresh(code):
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "statement",
    [
        "import kicad_tools.validate",
        "import kicad_tools.validate.rules",
        "from kicad_tools.validate import DRCResults",
        "from kicad_tools.validate.rules import DRCRule",
        "from kicad_tools.validate.rules import check_all_silkscreen",
        "import kicad_tools.validate.rules.silkscreen",
    ],
)
def test_lightweight_import_does_not_load_checker_or_router(statement):
    run_fresh(f"""
        import sys
        {statement}
        forbidden = (
            'kicad_tools.validate.checker',
            'kicad_tools.validate.rules.via_in_pad',
            'kicad_tools.router',
            'pydantic',
        )
        assert not set(forbidden).intersection(sys.modules), set(forbidden).intersection(sys.modules)
    """)


@pytest.mark.parametrize("package", ["kicad_tools.validate", "kicad_tools.validate.rules"])
def test_discovery_does_not_resolve_exports_and_unknown_names_raise(package):
    run_fresh(f"""
        import importlib, sys
        package = importlib.import_module({package!r})
        before = set(sys.modules)
        assert set(package.__all__) <= set(dir(package))
        assert set(sys.modules) == before
        try:
            package.not_a_validation_export
        except AttributeError as exc:
            assert 'not_a_validation_export' in str(exc)
        else:
            raise AssertionError('missing AttributeError')
    """)


# Public import contract before package exports became lazy.
PUBLIC_EXPORTS = {
    "kicad_tools.validate": {
        "DRCChecker": ".checker",
        "ConnectivityIssue": ".connectivity",
        "ConnectivityResult": ".connectivity",
        "ConnectivityValidator": ".connectivity",
        "ConsistencyIssue": ".consistency",
        "ConsistencyResult": ".consistency",
        "LVSMatch": ".consistency",
        "LVSResult": ".consistency",
        "SchematicPCBChecker": ".consistency",
        "BaseViolation": ".models",
        "DRCResult": ".models",
        "Location": ".models",
        "Severity": ".models",
        "ValidationResult": ".models",
        "ViolationCategory": ".models",
        "NetlistValidator": ".netlist",
        "SyncIssue": ".netlist",
        "SyncResult": ".netlist",
        "BOMPlacementVerifier": ".placement",
        "PlacementResult": ".placement",
        "PlacementStatus": ".placement",
        "DRCResults": ".violations",
        "DRCViolation": ".violations",
    },
    "kicad_tools.validate.rules": {
        "DRC_TOLERANCE": ".base",
        "DRCRule": ".base",
        "ClearanceRule": ".clearance",
        "SegmentZoneClearanceRule": ".clearance",
        "ViaZoneClearanceRule": ".clearance",
        "ConnectorEdgeAccessRule": ".connector_access",
        "DanglingCopperRule": ".dangling_copper",
        "DiffPairClearanceIntraRule": ".diffpair_clearance_intra",
        "DiffPairLengthSkewRule": ".diffpair_length_skew",
        "DiffPairRoutingContinuityRule": ".diffpair_routing_continuity",
        "DimensionRules": ".dimensions",
        "EdgeClearanceRule": ".edge",
        "ImpedanceRule": ".impedance",
        "NetImpedanceSpec": ".impedance",
        "MatchGroupLengthSkewRule": ".match_group_length_skew",
        "check_schematic_fields": ".schematic_fields",
        "check_all_silkscreen": ".silkscreen",
        "check_silk_edge_clearance": ".silkscreen",
        "check_silk_over_copper": ".silkscreen",
        "check_silkscreen_line_width": ".silkscreen",
        "check_silkscreen_over_pads": ".silkscreen",
        "check_silkscreen_text_height": ".silkscreen",
        "SinglePadNetRule": ".single_pad_net",
        "SolderMaskPadRules": ".solder_mask",
        "ViaInPadRule": ".via_in_pad",
        "IsolatedCopperRule": ".zone_fill",
        "ZoneFillRule": ".zone_fill",
    },
}


@pytest.mark.parametrize("package", PUBLIC_EXPORTS)
def test_all_public_exports_keep_identity_and_star_import(package):
    expected = PUBLIC_EXPORTS[package]
    run_fresh(f"""
        import importlib
        package = importlib.import_module({package!r})
        expected = {expected!r}
        assert set(package.__all__) == set(expected)
        namespace = {{}}
        exec('from {package} import *', namespace)
        for name, module in expected.items():
            direct = getattr(importlib.import_module(module, package.__name__), name)
            assert getattr(package, name) is direct
            assert namespace[name] is direct
            assert package.__dict__[name] is direct
    """)


def test_lazy_checker_executes_every_enabled_rule_with_real_results():
    run_fresh("""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker
        from kicad_tools.validate.checker import DRCChecker as DirectChecker
        assert DRCChecker is DirectChecker
        pcb = PCB.create(width=10, height=10, layers=2)
        checker = DRCChecker(pcb, layers=2, warn_on_inactive_skew_rules=False)
        calls = []
        for name in checker.CHECK_ALL_METHODS:
            original = getattr(checker, name)
            def record(*args, _name=name, _original=original, **kwargs):
                result = _original(*args, **kwargs)
                calls.append((_name, result))
                return result
            setattr(checker, name, record)
        result = checker.check_all()
        expected = [n for n in checker.CHECK_ALL_METHODS if n != 'check_mask_to_copper']
        assert [n for n, _ in calls] == expected
        assert result.rules_checked == sum(r.rules_checked for _, r in calls)
        assert result.rules_checked > 0
        assert result.violations == [v for _, r in calls for v in r.violations]
        assert 'check_via_in_pad' in [n for n, _ in calls]
    """)
