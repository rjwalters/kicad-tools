"""Issue #8826: copper-weight provenance and stale-waiver cause in kct check."""

from kicad_tools.cli.check_cmd import (
    _annotate_copper_stale_cause,
    _copper_weights_provenance,
    _format_copper_weights,
)


def test_provenance_sources():
    w = _copper_weights_provenance(2.0, 0.5, 2.0, None, 1.0, 0.5, disagreement=True)
    assert w["outer"] == {"oz": 2.0, "source": "flag"}
    assert w["inner"] == {"oz": 0.5, "source": "stackup"}
    assert w["flag_overrides_stackup"] is True
    assert _format_copper_weights(w) == "outer 2oz (flag), inner 0.5oz (stackup)"


def test_provenance_profile_when_no_stackup():
    w = _copper_weights_provenance(None, None, None, None, None, None, disagreement=False)
    assert w["outer"]["source"] == "profile"
    assert "profile default" in _format_copper_weights(w)


def test_stale_cause_annotated_only_for_ampacity_with_override():
    w = _copper_weights_provenance(2.0, 2.0, 2.0, 2.0, 1.0, 0.5, disagreement=True)
    dicts = [
        {"rule_id": "ampacity", "stale_waiver_cause": "evidence_changed"},
        {"rule_id": "clearance", "stale_waiver_cause": "evidence_changed"},
        {"rule_id": "ampacity", "stale_waiver_cause": "outdated_evidence_version"},
    ]
    _annotate_copper_stale_cause(dicts, w)
    assert dicts[0]["stale_waiver_cause"] == "copper_flag_overrides_stackup"
    assert dicts[1]["stale_waiver_cause"] == "evidence_changed"
    assert dicts[2]["stale_waiver_cause"] == "outdated_evidence_version"


def test_no_annotation_without_override():
    w = _copper_weights_provenance(1.0, 0.5, 1.0, None, 1.0, 0.5, disagreement=False)
    dicts = [{"rule_id": "ampacity", "stale_waiver_cause": "evidence_changed"}]
    _annotate_copper_stale_cause(dicts, w)
    assert dicts[0]["stale_waiver_cause"] == "evidence_changed"
