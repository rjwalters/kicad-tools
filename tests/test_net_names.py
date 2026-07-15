"""Unit tests for the hierarchical net-name normalizer (Issue #4149).

Pure tests for :mod:`kicad_tools.router.net_names` — no CLI plumbing.
Covers the suffix normalizer, the collision-detecting index, single-key
resolution (exact / unique-suffix / ambiguous / absent), the nearest-name
hint, and the aggregate sidecar resolver.
"""

from __future__ import annotations

from kicad_tools.router.net_names import (
    NetKeyResolution,
    build_net_name_index,
    nearest_net_names,
    net_name_suffix,
    resolve_net_class_map_keys,
    resolve_net_key,
)


class TestNetNameSuffix:
    def test_root_sheet_prefix_stripped(self):
        assert net_name_suffix("/FUSED_LINE") == "FUSED_LINE"

    def test_nested_sheet_takes_last_segment(self):
        assert net_name_suffix("/A/B/FUSED_LINE") == "FUSED_LINE"

    def test_bare_name_unchanged(self):
        assert net_name_suffix("GND") == "GND"

    def test_power_symbol_with_plus_unchanged(self):
        assert net_name_suffix("+3.3V") == "+3.3V"


class TestBuildNetNameIndex:
    def test_prefixed_and_bare_grouped_by_suffix(self):
        index = build_net_name_index(["/FUSED_LINE", "GND", "+3.3V"])
        assert index["FUSED_LINE"] == ["/FUSED_LINE"]
        assert index["GND"] == ["GND"]
        assert index["+3.3V"] == ["+3.3V"]

    def test_collision_lists_both_candidates(self):
        index = build_net_name_index(["/A", "A"])
        assert index["A"] == ["/A", "A"]

    def test_nested_collision(self):
        index = build_net_name_index(["/X/A", "/Y/A"])
        assert index["A"] == ["/X/A", "/Y/A"]

    def test_duplicate_raw_names_collapse(self):
        index = build_net_name_index(["/A", "/A"])
        assert index["A"] == ["/A"]


class TestResolveNetKey:
    def test_bare_key_matches_prefixed_net(self):
        index = build_net_name_index(["/FUSED_LINE"])
        res = resolve_net_key("FUSED_LINE", index)
        assert res.matched == "/FUSED_LINE"
        assert not res.is_ambiguous

    def test_exact_bare_match_unchanged(self):
        index = build_net_name_index(["GND", "/FUSED_LINE"])
        res = resolve_net_key("GND", index)
        assert res.matched == "GND"

    def test_user_supplied_prefix_exact_match(self):
        index = build_net_name_index(["/FUSED_LINE"])
        res = resolve_net_key("/FUSED_LINE", index)
        assert res.matched == "/FUSED_LINE"

    def test_nested_prefix_bare_key(self):
        index = build_net_name_index(["/A/B/FUSED_LINE"])
        res = resolve_net_key("FUSED_LINE", index)
        assert res.matched == "/A/B/FUSED_LINE"

    def test_ambiguous_key_matches_none(self):
        index = build_net_name_index(["/A", "A"])
        res = resolve_net_key("A", index)
        assert res.matched is None
        assert res.is_ambiguous
        assert set(res.ambiguous) == {"/A", "A"}

    def test_exact_wins_over_ambiguity(self):
        # When the key is itself a raw board net, exact match wins even
        # though the suffix collides — a fully-qualified key is never
        # treated as ambiguous.
        index = build_net_name_index(["/A", "A"])
        res = resolve_net_key("/A", index)
        assert res.matched == "/A"
        assert not res.is_ambiguous

    def test_no_match(self):
        index = build_net_name_index(["/FUSED_LINE"])
        res = resolve_net_key("FUSED_LIN", index)
        assert res.matched is None
        assert not res.is_ambiguous

    def test_resolution_dataclass_defaults(self):
        res = NetKeyResolution(key="X")
        assert res.matched is None
        assert res.ambiguous == ()
        assert not res.is_ambiguous


class TestNearestNetNames:
    def test_suffix_hint_for_prefixed_net(self):
        hits = nearest_net_names("FUSED_LINE", ["/FUSED_LINE", "GND"])
        assert "/FUSED_LINE" in hits

    def test_typo_prefix_containment(self):
        hits = nearest_net_names("FUSED_LIN", ["/FUSED_LINE", "GND"])
        assert "/FUSED_LINE" in hits

    def test_no_similar_returns_empty(self):
        hits = nearest_net_names("ZZZZ", ["GND", "+3.3V"])
        assert hits == []

    def test_respects_limit(self):
        board = ["/A_1", "/A_2", "/A_3", "/A_4"]
        hits = nearest_net_names("A", board, limit=2)
        assert len(hits) == 2


class TestResolveNetClassMapKeys:
    def test_mixed_prefixed_and_bare(self):
        board = ["/FUSED_LINE", "/PGND", "GND", "+3.3V"]
        res = resolve_net_class_map_keys(["FUSED_LINE", "PGND", "GND"], board)
        assert res.resolved == {
            "/FUSED_LINE": "FUSED_LINE",
            "/PGND": "PGND",
            "GND": "GND",
        }
        assert res.unmatched == []
        assert res.ambiguous == {}

    def test_unmatched_key(self):
        board = ["/FUSED_LINE", "GND"]
        res = resolve_net_class_map_keys(["FUSED_LIN"], board)
        assert res.resolved == {}
        assert res.unmatched == ["FUSED_LIN"]

    def test_ambiguous_key_partitioned(self):
        board = ["/A", "A", "GND"]
        res = resolve_net_class_map_keys(["A", "GND"], board)
        # 'A' collides; 'GND' resolves.
        assert res.resolved == {"GND": "GND"}
        assert "A" in res.ambiguous
        assert set(res.ambiguous["A"]) == {"/A", "A"}
        assert res.unmatched == []

    def test_total_counts_all_buckets(self):
        board = ["/FUSED_LINE", "/A", "A"]
        res = resolve_net_class_map_keys(["FUSED_LINE", "TYPO", "A"], board)
        assert res.total == 3
        assert len(res.resolved) == 1
        assert len(res.unmatched) == 1
        assert len(res.ambiguous) == 1
