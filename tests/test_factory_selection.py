"""Factory selections must be explicit, exact and content-bound."""

from dataclasses import replace

import pytest

from kicad_tools.export.factory_selection import (
    ExpectedPart,
    SelectionMapping,
    compare_factory_selection,
)

PLAN = [ExpectedPart("R1", "C1", "RES-1"), ExpectedPart("Y1", "C2", "TCXO-EXACT")]
MAPPING = SelectionMapping("Refs", "Selected", ("yes",), ("no",), "Catalog", "MPN")
HEADER = "Refs,Selected,Catalog,MPN\n"
GOOD = HEADER + "R1,yes,C1,RES-1\nY1,yes,C2,TCXO-EXACT\n"


def compare(text=GOOD, **kwargs):
    return compare_factory_selection(
        kwargs.pop("expected", PLAN),
        text.encode("utf-8"),
        kwargs.pop("mapping", MAPPING),
        source_kind=kwargs.pop("source_kind", "factory_selected_csv"),
        **kwargs,
    )


def test_exact_match_is_comparison_only():
    report = compare()
    assert report["status"] == "compared" and report["comparison_matches"]
    assert not report["factory_matched"] and not report["release_eligible"]
    assert {r["reference"] for r in report["coverage"]} == {"R1", "Y1"}
    assert all(r["matches"] for r in report["coverage"])
    assert report["source_authentication"] == "caller_declared"


@pytest.mark.parametrize(
    "text,issue",
    [
        (HEADER + "R1,yes,C1,RES-1\n", "Missing factory"),
        (GOOD.replace("Y1,yes", "Y1,no"), "unselected"),
        (GOOD + "R1,yes,C1,RES-1\n", "Duplicate"),
        (GOOD + "C1,yes,C8,CAP\n", "unexpected"),
        (GOOD.replace("C2", "C99"), "catalog_id mismatch"),
        (GOOD.replace("TCXO-EXACT", "TCXO-SUBSTITUTE"), "mpn mismatch"),
        (GOOD.replace("RES-1", "res-1"), "mpn mismatch"),
        (GOOD.replace("TCXO-EXACT", ""), "mpn mismatch"),
    ],
)
def test_mismatches_never_pass(text, issue):
    report = compare(text)
    assert report["status"] == "mismatch"
    assert not report["comparison_matches"]
    assert any(issue in entry for entry in report["issues"])
    assert len(report["coverage"]) == len(PLAN)


@pytest.mark.parametrize("first", [True, False])
def test_conflicting_duplicate_rows_in_both_orders(first):
    bad = "Y1,yes,C99,SUBSTITUTE\n"
    rows = GOOD[len(HEADER) :]
    report = compare(HEADER + (bad + rows if first else rows + bad))
    assert not report["comparison_matches"]
    assert any("Duplicate" in issue for issue in report["issues"])


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Refs,Selected,MPN\n",
        "Refs,Selected,Catalog,Catalog,MPN\n",
        GOOD.replace("Y1,yes", "Y1,maybe"),
        GOOD.replace("Y1,yes", "Y1,"),
        GOOD.replace("Y1,yes", "Y1,YES"),
        GOOD + "R2,yes\n",
        GOOD + "R2,yes,C3,X,extra\n",
        HEADER + '"R1,yes,C1,RES-1\n',
        GOOD.replace("R1,yes", '"R1-R4",yes'),
        GOOD.replace("R1,yes", '"R1,",yes'),
    ],
)
def test_malformed_csv_retains_expected_coverage(text):
    report = compare(text)
    assert report["status"] == "incomplete"
    assert not report["comparison_matches"]
    assert len(report["coverage"]) == len(PLAN)


@pytest.mark.parametrize("separator", [",", ";"])
def test_bom_quoted_grouped_refs_and_identity(separator):
    mapping = replace(MAPPING, group_separator=separator)
    expected = [ExpectedPart("R1", "C1", "RES,PART"), ExpectedPart("R2", "C1", "RES,PART")]
    report = compare(
        "\ufeff" + HEADER + f'"R1{separator} R2",yes,C1,"RES,PART"\n',
        mapping=mapping,
        expected=expected,
    )
    assert report["comparison_matches"]


def test_duplicate_in_same_group_rejected():
    report = compare(HEADER + '"R1,R1",yes,C1,RES-1\nY1,yes,C2,TCXO-EXACT\n')
    assert any("Duplicate" in issue for issue in report["issues"])


@pytest.mark.parametrize("source", ["generated_expected_csv", "", "unknown"])
def test_expected_or_unknown_source_kind_cannot_pass(source):
    assert compare(source_kind=source)["status"] == "incomplete"
    assert not compare(source_kind=source)["comparison_matches"]


def test_hashes_bind_bytes_plan_and_policy_independently():
    original = compare()
    reordered = compare(expected=list(reversed(PLAN)))
    assert original["expected_plan_sha256"] == reordered["expected_plan_sha256"]
    changed_csv = compare(GOOD.replace("\n", "\r\n"))
    assert changed_csv["comparison_matches"]
    assert original["factory_csv_sha256"] != changed_csv["factory_csv_sha256"]
    changed_plan = compare(expected=[PLAN[0], replace(PLAN[1], mpn="OTHER")])
    assert original["expected_plan_sha256"] != changed_plan["expected_plan_sha256"]
    changed_policy = compare(mapping=replace(MAPPING, selected_values=("yes", "checked")))
    assert original["comparison_policy_sha256"] != changed_policy["comparison_policy_sha256"]


@pytest.mark.parametrize("identity,field", [("mpn", "catalog_id"), ("catalog_id", "mpn")])
def test_explicit_single_identity_policy(identity, field):
    mapping = replace(MAPPING, identity=identity, **{field: None})
    expected = [replace(part, **{field: ""}) for part in PLAN]
    assert compare(mapping=mapping, expected=expected)["comparison_matches"]


@pytest.mark.parametrize(
    "mapping",
    [
        replace(MAPPING, selected_values=()),
        replace(MAPPING, unselected_values=("yes",)),
        replace(MAPPING, selected_values=("yes", "yes")),
        replace(MAPPING, selected="Refs"),
        replace(MAPPING, mpn=None),
        replace(MAPPING, group_separator="|"),
        replace(MAPPING, identity="fuzzy"),
    ],
)
def test_invalid_mapping_rejected(mapping):
    with pytest.raises(ValueError):
        compare(mapping=mapping)


@pytest.mark.parametrize(
    "expected",
    [[], [PLAN[0], PLAN[0]], [ExpectedPart("R1", "", "X")], [ExpectedPart("R1-R4", "C1", "X")]],
)
def test_invalid_plan_rejected(expected):
    with pytest.raises(ValueError):
        compare(expected=expected)


def test_non_utf8_reports_incomplete():
    result = compare_factory_selection(PLAN, b"\xff", MAPPING, source_kind="factory_selected_csv")
    assert result["status"] == "incomplete" and len(result["coverage"]) == 2


@pytest.mark.parametrize("field", ["selected_values", "unselected_values"])
@pytest.mark.parametrize(
    "tokens",
    ["yes", b"yes", bytearray(b"yes"), None, 1, True, {"yes": True}, {"yes"}, [["yes"]], [None]],
)
def test_invalid_token_containers_fail_before_membership(field, tokens):
    mapping = replace(MAPPING, **{field: tokens})
    with pytest.raises(ValueError):
        compare(mapping=mapping)


def test_missing_tuple_comma_cannot_select_single_character():
    mapping = SelectionMapping(
        "Ref", "Selected", "yes", "no", catalog_id="ID", identity="catalog_id"
    )
    with pytest.raises(ValueError):
        compare_factory_selection(
            [ExpectedPart("R1", "C1")],
            b"Ref,Selected,ID\nR1,y,C1\n",
            mapping,
            source_kind="factory_selected_csv",
        )


@pytest.mark.parametrize("container", [tuple, list])
def test_valid_token_sequences_require_whole_token(container):
    mapping = replace(
        MAPPING, selected_values=container(["yes"]), unselected_values=container(["no"])
    )
    assert compare(mapping=mapping)["comparison_matches"]
    report = compare(GOOD.replace("R1,yes", "R1,y"), mapping=mapping)
    assert report["status"] == "incomplete"
    assert not report["comparison_matches"]
