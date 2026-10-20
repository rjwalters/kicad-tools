"""Synthetic gates must never consume or mutate assembled release artifacts."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/ci" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("board", "filename"),
    [
        ("06-diffpair-test", "diffpair_test_routed.kicad_pcb"),
        ("07-matchgroup-test", "matchgroup_test_routed.kicad_pcb"),
    ],
)
def test_synthetic_baselines_never_apply_to_active_release(monkeypatch, board, filename):
    """Issue #7367: neither key carries a count; the fixture is identity-allowed."""
    monkeypatch.chdir(ROOT)
    helper = load("board_recipe_artifacts")
    gate = load("check_diffpair_coverage")
    allowances = gate.load_allowlist(ROOT / ".github/routed-drc-tolerance.yml")
    root = ROOT / "boards" / board
    active = helper.recipe_baseline_key(root / "output" / filename)
    fixture = helper.recipe_baseline_key(root / "regression-fixture" / filename)
    regenerated = helper.recipe_baseline_key(root / "regression-output" / filename)
    assert regenerated == fixture != active
    assert fixture not in allowances
    assert allowances.get(active, 0) == 0


def _v(rule, identity):
    return {"rule_id": rule, "severity": "error", "items": identity.split("/"), "nets": []}


def test_pair_improvement_cannot_hide_new_physical_error():
    """A fixed pair frees no slot: a new via_in_pad or a new pair both fail (#7367)."""
    gate = load("check_diffpair_coverage")
    identity = load("identity_allowance")
    key = "boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb"
    found = [_v(rule, ident) for rule, ident in gate.DIFFPAIR_FIXTURE_ALLOWED_FINDINGS]
    fixed = next(v for v in found if v["rule_id"] == "diffpair_length_skew")
    found.remove(fixed)
    found += [_v("via_in_pad", "USB3_TX1+"), _v("diffpair_length_skew", "NEW+/NEW-")]
    errors: dict[str, int] = {}
    for v in found:
        errors[v["rule_id"]] = errors.get(v["rule_id"], 0) + 1
    ids = identity.error_identities(found)
    assert gate.unexpected_baseline_errors(key, errors, ids) == {
        "via_in_pad": 1,
        "diffpair_length_skew": 1,
    }
    assert [s[1] for s in gate.stale_allowances(key, ids)] == ["/".join(sorted(fixed["items"]))]


BOARD06_KEY = "boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb"
# Identities of the residual CI seed-42 re-route findings after the #7344
# gap-tightening pass (#7349) and the #7296 mask-dam floor, not of the committed
# PCB: the gate judges the re-route.  The last one, USB3_TX2-, was a #7575
# false positive (its only in-window partner copper was a 0.02 mm TX2+ stub
# inside U2.F2) and is cleared by the coupled-length rule, so none remain.
BOARD06_IMPEDANCE_NETS: set[str] = set()
BOARD06_UNCOUPLED_PAIR = "USB2_D+/USB2_D-"


def _skew(gate):
    """The listed skew pairs (#7367): present in every ``found`` below."""
    return {
        "diffpair_length_skew": {
            n for r, n in gate.DIFFPAIR_FIXTURE_ALLOWED_FINDINGS if r == "diffpair_length_skew"
        }
    }


def test_board06_impedance_allowance_is_keyed_by_net_with_issue():
    """Issue #7359: identity + issue number, no count cap left."""
    gate = load("check_diffpair_coverage")
    assert not hasattr(gate, "DIFFPAIR_FIXTURE_RULE_CAPS")
    assert {n for (r, n) in gate.DIFFPAIR_FIXTURE_ALLOWED_FINDINGS if r == "impedance"} == (
        BOARD06_IMPEDANCE_NETS
    )
    assert {
        n for (r, n) in gate.DIFFPAIR_FIXTURE_ALLOWED_FINDINGS if r == "diffpair_uncoupled_length"
    } == {BOARD06_UNCOUPLED_PAIR}
    assert {
        i
        for (r, _), i in gate.DIFFPAIR_FIXTURE_ALLOWED_FINDINGS.items()
        if r != "diffpair_length_skew"
    } == {7401}


def test_board06_listed_impedance_findings_pass():
    gate = load("check_diffpair_coverage")
    found = {
        "impedance": set(BOARD06_IMPEDANCE_NETS),
        "diffpair_uncoupled_length": {BOARD06_UNCOUPLED_PAIR},
        **_skew(gate),
    }
    errors = {"diffpair_uncoupled_length": 1}
    assert gate.unexpected_baseline_errors(BOARD06_KEY, errors, found) == {}
    assert gate.stale_allowances(BOARD06_KEY, found) == []


def test_board06_new_uncoupled_pair_fails():
    """Only USB2_D (#7401) may exceed the uncoupled limit; another pair is new."""
    gate = load("check_diffpair_coverage")
    found = {"diffpair_uncoupled_length": {BOARD06_UNCOUPLED_PAIR, "USB3_RX1+/USB3_RX1-"}}
    errors = {"diffpair_uncoupled_length": 2}
    assert gate.unexpected_baseline_errors(BOARD06_KEY, errors, found) == {
        "diffpair_uncoupled_length": 1
    }


def test_board06_new_impedance_net_fails():
    """USB3_TX2+ was cleared by #7349: it cannot fail again unnoticed (#7270 Q3)."""
    gate = load("check_diffpair_coverage")
    found = {
        "impedance": set(BOARD06_IMPEDANCE_NETS) | {"USB3_TX2+"},
        "diffpair_uncoupled_length": {BOARD06_UNCOUPLED_PAIR},
        **_skew(gate),
    }
    errors = {"impedance": 2, "diffpair_uncoupled_length": 1}
    assert gate.unexpected_baseline_errors(BOARD06_KEY, errors, found) == {"impedance": 1}
    assert gate.stale_allowances(BOARD06_KEY, found) == []


def test_board06_cleared_impedance_finding_cannot_return():
    """Issue #7575: USB3_TX2-'s in-pad-stub finding is gone; it cannot come back."""
    gate = load("check_diffpair_coverage")
    found = {
        "impedance": {"USB3_TX2-"},
        "diffpair_uncoupled_length": {BOARD06_UNCOUPLED_PAIR},
        **_skew(gate),
    }
    errors = {"impedance": 1, "diffpair_uncoupled_length": 1}
    assert gate.unexpected_baseline_errors(BOARD06_KEY, errors, found) == {"impedance": 1}
    assert gate.stale_allowances(BOARD06_KEY, found) == []


def test_board06_fixed_uncoupled_finding_makes_allowance_stale():
    """Issue #7401: once USB2_D routes coupled its entry must be deleted."""
    gate = load("check_diffpair_coverage")
    found = {"impedance": set(BOARD06_IMPEDANCE_NETS), **_skew(gate)}
    assert gate.stale_allowances(BOARD06_KEY, found) == [
        ("diffpair_uncoupled_length", BOARD06_UNCOUPLED_PAIR, 7401)
    ]


def test_board06_impedance_without_identities_is_not_trusted():
    gate = load("check_diffpair_coverage")
    assert gate.unexpected_baseline_errors(BOARD06_KEY, {"impedance": 3}) == {"impedance": 3}


def test_same_net_two_segments_use_one_identity_slot():
    identity = load("identity_allowance")
    v = {"rule_id": "impedance", "severity": "error", "items": ["USB2_D-"], "nets": []}
    assert identity.error_identities([v, dict(v)]) == {"impedance": {"USB2_D-"}}
    warn = dict(v, severity="warning")
    assert identity.error_identities([warn]) == {}


def test_board06_power_clearance_errors_are_not_accepted():
    """Issue #6772: the #6763 allowance for 3 Power-class clearance errors is gone."""
    gate = load("check_diffpair_coverage")
    key = "boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb"
    assert not hasattr(gate, "ACCEPTED_NON_PAIR_ERRORS")
    errors = {"netclass_clearance_copper": 1}
    assert gate.unexpected_baseline_errors(key, errors) == {"netclass_clearance_copper": 1}
    errors["netclass_clearance_copper"] = 0
    assert gate.unexpected_baseline_errors(key, errors) == {}


def test_matchgroup_improvement_cannot_hide_new_physical_error():
    """Skew/continuity are judged by identity too; connectivity stays advisory (#7367)."""
    gate = load("check_matchgroup_coverage")
    identity = load("identity_allowance")
    key = "boards/07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb"
    found = [_v(rule, ident) for rule, ident in gate.board_allowance(key)]
    found += [_v("connectivity", "TMDS_D0_N"), _v("via_in_pad", "DQ3")]
    found.append(_v("diffpair_routing_continuity", "TMDS_D2_N/TMDS_D2_P"))
    errors: dict[str, int] = {}
    for v in found:
        errors[v["rule_id"]] = errors.get(v["rule_id"], 0) + 1
    ids = identity.error_identities(found)
    assert gate.unexpected_baseline_errors(key, errors, ids) == {
        "via_in_pad": 1,
        "diffpair_routing_continuity": 1,
    }
    found = found[:-2]
    errors = {"connectivity": 1}
    for rule, _ in gate.board_allowance(key):
        errors[rule] = errors.get(rule, 0) + 1
    assert gate.unexpected_baseline_errors(key, errors, identity.error_identities(found)) == {}


@pytest.mark.parametrize(
    ("number", "board", "gate"),
    [("06", "06-diffpair-test", "diffpair"), ("07", "07-matchgroup-test", "matchgroup")],
)
def test_e2e_stages_matching_synthetic_bundle(number, board, gate):
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"][f"board-{number}-end-to-end"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step.get("name", "").startswith("Independent synthetic routing gate")
    )
    assert f"cp -a /tmp/board{number}-ci/. boards/{board}/regression-output/" in command
    assert f"check_{gate}_coverage.py" in command
    assert "--skip-route" in command
    assert f"boards/{board}/output/" not in command


@pytest.mark.parametrize("gate_name", ["check_diffpair_coverage", "check_matchgroup_coverage"])
def test_recipe_uses_its_own_pcb_and_sidecar(tmp_path, monkeypatch, gate_name):
    gate = load(gate_name)
    board = tmp_path / "demo"
    (board / "output").mkdir(parents=True)
    fixture = board / "regression-fixture"
    fixture.mkdir()
    (board / "generate_design.py").write_text("# fixture recipe")
    for folder, identity in [(board / "output", "assembled"), (fixture, "synthetic")]:
        (folder / "demo_routed.kicad_pcb").write_text(identity)
        (folder / "net_class_map.json").write_text(identity)
    assert gate.find_routed_pcb(board).parent == fixture
    commands = []

    def run(command, **kwargs):
        from types import SimpleNamespace

        commands.append(command)
        return SimpleNamespace(returncode=0)

    class FakePopen:
        """Stand-in for ``subprocess.Popen`` (Issue #5617's streaming re-route)."""

        def __init__(self, command, **kwargs):
            commands.append(command)
            self.stdout = iter(())
            self.returncode = 0

        def wait(self):
            return self.returncode

    monkeypatch.setattr(gate.subprocess, "run", run)
    monkeypatch.setattr(gate.subprocess, "Popen", FakePopen)
    assert gate.re_route_board(board, 42)
    assert commands[0][2] == str(board / "regression-output")
    routed = gate.find_routed_pcb(board)
    assert routed.parent == board / "regression-output"
    assert routed.read_text() == "synthetic"
    assert (board / "output/demo_routed.kicad_pcb").read_text() == "assembled"
    assert (board / "output/net_class_map.json").read_text() == "assembled"
    if hasattr(gate, "find_net_class_map_sidecar"):
        assert gate.find_net_class_map_sidecar(board, routed).parent == routed.parent
