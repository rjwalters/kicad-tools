"""Tests for the external-corpus manifest tooling (issue #4830, slice 2).

These cover the *offline* half of ``scripts/corpus/``: the manifest schema and
its validation, payload-cache integrity, outcome scoring, and the shared parse
taxonomy. Nothing here touches the network — the fetching scripts
(``build_manifest.py`` / ``check_manifest.py`` / ``probe_open_schematics.py``)
are deliberately not imported, in line with the issue's "no network in the
pytest suite, zero CI impact" constraint.

The manifest committed at ``scripts/corpus/manifests/open-schematics-sample.json``
is also asserted on directly: it is a repo artifact, and the property that
matters most about it — that it pins third-party CC-BY-4.0 payloads by URL +
hash and never embeds their bytes — is exactly the kind of thing a well-meaning
future edit could break silently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "scripts" / "corpus"

# The corpus tooling lives under scripts/, not in the installed package (same
# convention as tests/test_calibrate_fom.py for scripts/research/).
sys.path.insert(0, str(CORPUS_DIR))

from corpus_manifest import (  # noqa: E402  (sys.path manipulation above)
    DEFAULT_MANIFEST,
    FORBIDDEN_ENTRY_KEYS,
    MANIFEST_SCHEMA_VERSION,
    CorpusManifest,
    EntryResult,
    ManifestEntry,
    ManifestError,
    cache_path_for,
    compare_metrics,
    dump_manifest,
    load_manifest,
    render_scorecard,
    score_entries,
    sha256_of_text,
    validate_manifest_dict,
    verify_cached,
)
from parse_taxonomy import (  # noqa: E402
    FAIL_SEXPR_SYNTAX,
    FAILURE_CLASSES,
    HASH_MISMATCH,
    MISSING_PAYLOAD,
    OK,
    SKIP_EMPTY,
    SKIP_NON_KICAD,
    SKIP_OVERSIZE,
    classify_exception,
    declared_version,
    detect_format,
    parse_payload,
    signature_of,
)

MINIMAL_PCB = (
    "(kicad_pcb (version 20221018) (generator pcbnew)\n"
    "  (general (thickness 1.6))\n"
    "  (paper A4)\n"
    "  (layers (0 F.Cu signal) (31 B.Cu signal))\n"
    '  (net 0 "")\n'
    ")\n"
)


def _entry(**overrides: object) -> dict[str, object]:
    """A schema-valid manifest entry dict, with overrides applied."""
    base: dict[str, object] = {
        "id": "os-0000001-pcb0",
        "kind": "pcb",
        "row_offset": 1,
        "cell": "pcb_files",
        "slot": 0,
        "record_name": "someone/some-board",
        "source_url": "https://datasets-server.huggingface.co/rows?offset=1&length=1",
        "sha256": "a" * 64,
        "size_bytes": 1234,
        "detected_format": "kicad_pcb",
        "declared_version": "20221018",
        "expected_outcome": OK,
        "metrics": {"footprints": 3, "pads": 6},
        "notes": "",
    }
    base.update(overrides)
    return base


def _manifest_dict(entries: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_id": "test-manifest",
        "generated_utc": "2026-08-14T00:00:00+00:00",
        "generator": "scripts/corpus/build_manifest.py",
        "dataset": {
            "id": "bshada/open-schematics",
            "url": "https://huggingface.co/datasets/bshada/open-schematics",
            "license": "CC-BY-4.0",
            "attribution": "Contains data from the Open Schematics dataset by bshada.",
            "config": "default",
            "split": "train",
            "revision": "0" * 40,
            "row_count": 87931,
        },
        "selection": {"seed": 4830},
        "entries": entries if entries is not None else [_entry()],
    }


# ---------------------------------------------------------------------------
# The committed manifest
# ---------------------------------------------------------------------------
class TestCommittedManifest:
    def test_loads_and_validates(self) -> None:
        manifest = load_manifest(DEFAULT_MANIFEST)
        assert manifest.schema_version == MANIFEST_SCHEMA_VERSION
        assert manifest.dataset.id == "bshada/open-schematics"
        assert manifest.dataset.license == "CC-BY-4.0"
        assert manifest.dataset.attribution  # CC-BY requires attribution downstream
        assert len(manifest.entries) >= 20, "the curated sample should be ~20-50 entries"

    def test_entries_are_unique_and_addressable(self) -> None:
        manifest = load_manifest(DEFAULT_MANIFEST)
        ids = [e.id for e in manifest.entries]
        hashes = [e.sha256 for e in manifest.entries]
        assert len(set(ids)) == len(ids)
        assert len(set(hashes)) == len(hashes)
        for entry in manifest.entries:
            assert entry.source_url.startswith("https://")
            assert str(entry.row_offset) in entry.source_url
            assert entry.size_bytes > 0

    def test_sample_is_diverse(self) -> None:
        """A sample that is all one kind/era teaches us nothing the 9 boards don't."""
        manifest = load_manifest(DEFAULT_MANIFEST)
        assert manifest.by_kind("pcb"), "no PCB entries"
        assert manifest.by_kind("schematic"), "no schematic entries"
        versions = {e.declared_version for e in manifest.entries}
        assert len(versions) >= 5, f"only {len(versions)} distinct file-format versions"
        sizes = sorted(e.size_bytes for e in manifest.entries)
        assert sizes[-1] >= 10 * sizes[0], "no spread in payload size"

    def test_no_third_party_payload_bytes_are_committed(self) -> None:
        """The corpus stays referenced by URL: pointers only, never the files."""
        raw = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        for entry in raw["entries"]:
            assert not (FORBIDDEN_ENTRY_KEYS & set(entry))
            serialised = json.dumps(entry)
            assert "(kicad_pcb" not in serialised
            assert "(kicad_sch" not in serialised
            assert len(serialised) < 4096, "an entry this large probably embeds payload text"
        # Total size is a blunt but effective backstop against vendoring.
        assert DEFAULT_MANIFEST.stat().st_size < 512_000

    def test_payload_cache_is_gitignored(self) -> None:
        """Fetched payloads must never become committable."""
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "scripts/corpus/.cache/" in ignored

    def test_not_referenced_from_ci(self) -> None:
        """Zero CI impact: no workflow may invoke the corpus scripts."""
        workflows = ROOT / ".github" / "workflows"
        for path in workflows.glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            assert "scripts/corpus/" not in text, f"{path.name} references scripts/corpus/"


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------
class TestValidation:
    def test_valid_manifest_has_no_errors(self) -> None:
        assert validate_manifest_dict(_manifest_dict()) == []

    def test_rejects_non_object(self) -> None:
        assert validate_manifest_dict([1, 2, 3])

    @pytest.mark.parametrize("key", ["schema_version", "manifest_id", "dataset", "entries"])
    def test_rejects_missing_top_level_key(self, key: str) -> None:
        obj = _manifest_dict()
        del obj[key]
        errors = validate_manifest_dict(obj)
        assert any(key in err for err in errors)

    def test_rejects_future_schema_version(self) -> None:
        obj = _manifest_dict()
        obj["schema_version"] = MANIFEST_SCHEMA_VERSION + 1
        assert any("schema_version" in err for err in validate_manifest_dict(obj))

    def test_rejects_embedded_payload(self) -> None:
        for key in sorted(FORBIDDEN_ENTRY_KEYS):
            obj = _manifest_dict([_entry(**{key: "(kicad_pcb ...)"})])
            errors = validate_manifest_dict(obj)
            assert any("must not be committed" in err for err in errors), key

    def test_rejects_bad_hash(self) -> None:
        obj = _manifest_dict([_entry(sha256="nope")])
        assert any("sha256" in err for err in validate_manifest_dict(obj))

    def test_rejects_duplicate_id_and_hash(self) -> None:
        obj = _manifest_dict([_entry(), _entry(sha256="b" * 64)])
        assert any("duplicate id" in err for err in validate_manifest_dict(obj))
        obj = _manifest_dict([_entry(), _entry(id="os-0000002-pcb0")])
        assert any("duplicate sha256" in err for err in validate_manifest_dict(obj))

    def test_rejects_bad_kind_and_url(self) -> None:
        obj = _manifest_dict([_entry(kind="gerber", source_url="ftp://example.com")])
        errors = validate_manifest_dict(obj)
        assert any("'kind'" in err for err in errors)
        assert any("source_url" in err for err in errors)

    def test_rejects_non_pinnable_expected_outcome(self) -> None:
        """Skip/integrity classes are run-time states, not something to pin."""
        obj = _manifest_dict([_entry(expected_outcome=MISSING_PAYLOAD)])
        assert any("expected_outcome" in err for err in validate_manifest_dict(obj))
        obj = _manifest_dict([_entry(expected_outcome=FAIL_SEXPR_SYNTAX)])
        assert validate_manifest_dict(obj) == []

    def test_load_manifest_reports_useful_errors(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.json"
        with pytest.raises(ManifestError, match="not found"):
            load_manifest(missing)

        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with pytest.raises(ManifestError, match="not valid JSON"):
            load_manifest(broken)

        invalid = tmp_path / "invalid.json"
        invalid.write_text(json.dumps(_manifest_dict([_entry(sha256="x")])), encoding="utf-8")
        with pytest.raises(ManifestError, match="sha256"):
            load_manifest(invalid)

    def test_round_trip_is_stable(self, tmp_path: Path) -> None:
        manifest = CorpusManifest.from_dict(_manifest_dict())
        path = tmp_path / "m.json"
        dump_manifest(manifest, path)
        reloaded = load_manifest(path)
        assert reloaded.to_dict() == manifest.to_dict()
        assert path.read_text(encoding="utf-8").endswith("\n")

    def test_unknown_entry_keys_are_dropped_not_crashed(self) -> None:
        entry = ManifestEntry.from_dict(_entry(future_field="ignored"))
        assert entry.id == "os-0000001-pcb0"
        assert not hasattr(entry, "future_field")


# ---------------------------------------------------------------------------
# Payload cache integrity
# ---------------------------------------------------------------------------
class TestCacheIntegrity:
    def test_cache_path_is_hash_prefixed(self, tmp_path: Path) -> None:
        entry = ManifestEntry.from_dict(_entry(sha256=sha256_of_text(MINIMAL_PCB)))
        path = cache_path_for(entry, tmp_path)
        assert path.parent == tmp_path
        assert path.name.startswith(entry.sha256[:16])
        assert path.suffix == ".kicad_pcb"

    def test_verify_cached_missing_hit_and_mismatch(self, tmp_path: Path) -> None:
        entry = ManifestEntry.from_dict(_entry(sha256=sha256_of_text(MINIMAL_PCB)))
        ok, detail = verify_cached(entry, tmp_path)
        assert not ok and detail == "not cached"

        path = cache_path_for(entry, tmp_path)
        path.write_text(MINIMAL_PCB, encoding="utf-8")
        assert verify_cached(entry, tmp_path) == (True, "ok")

        path.write_text(MINIMAL_PCB + "\n; upstream changed\n", encoding="utf-8")
        ok, detail = verify_cached(entry, tmp_path)
        assert not ok and "!= pinned" in detail

    def test_compare_metrics(self) -> None:
        assert compare_metrics({"pads": 6}, {"pads": 6, "extra": 1}) == []
        assert compare_metrics({"pads": 6}, {"pads": 5}) == ["pads: pinned 6 -> now 5"]
        assert compare_metrics({"pads": 6}, {}) == ["pads: pinned 6, not re-derived"]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _results() -> list[EntryResult]:
    return [
        EntryResult(id="a", kind="pcb", outcome=OK, declared_version="20221018"),
        EntryResult(
            id="b",
            kind="pcb",
            outcome=FAIL_SEXPR_SYNTAX,
            declared_version="20171130",
            signature="ParseError: Unexpected content at position N",
        ),
        EntryResult(id="c", kind="schematic", outcome=MISSING_PAYLOAD, detail="not cached"),
        EntryResult(id="d", kind="schematic", outcome=HASH_MISMATCH, detail="sha mismatch"),
        EntryResult(
            id="e",
            kind="pcb",
            outcome=OK,
            expected_outcome=FAIL_SEXPR_SYNTAX,
            declared_version="20211014",
        ),
        EntryResult(id="f", kind="pcb", outcome=OK, metrics_drift=["pads: pinned 6 -> now 0"]),
    ]


class TestScoring:
    def test_integrity_failures_do_not_count_as_parse_failures(self) -> None:
        manifest = CorpusManifest.from_dict(_manifest_dict())
        report = score_entries(manifest, _results())
        totals = report["totals"]
        assert totals["parsed"] == 4  # two unavailable entries excluded
        assert totals["failed"] == 1
        assert totals["unavailable"] == 2
        assert totals["parse_failure_rate"] == pytest.approx(0.25)
        assert {u["id"] for u in report["unavailable_entries"]} == {"c", "d"}

    def test_regressions_and_unexpected_passes_are_separated(self) -> None:
        manifest = CorpusManifest.from_dict(_manifest_dict())
        report = score_entries(manifest, _results())
        assert report["totals"]["regressions"] == 1
        assert [r["id"] for r in report["regressions"]] == ["b"]
        assert report["totals"]["unexpected_passes"] == 1
        assert [r["id"] for r in report["unexpected_passes"]] == ["e"]

    def test_metrics_drift_is_reported_without_being_a_parse_failure(self) -> None:
        manifest = CorpusManifest.from_dict(_manifest_dict())
        report = score_entries(manifest, _results())
        assert report["totals"]["metrics_drift"] == 1
        assert report["metrics_drift"][0]["id"] == "f"
        assert report["totals"]["failed"] == 1  # drift did not inflate the failure count

    def test_empty_results_do_not_divide_by_zero(self) -> None:
        manifest = CorpusManifest.from_dict(_manifest_dict())
        report = score_entries(manifest, [])
        assert report["totals"]["parse_failure_rate"] is None
        assert report["totals"]["coverage"] == 0.0

    def test_scorecard_renders_the_signals(self) -> None:
        manifest = CorpusManifest.from_dict(_manifest_dict())
        text = render_scorecard(score_entries(manifest, _results()))
        assert "corpus manifest scorecard" in text
        assert "REGRESSIONS" in text
        assert "unexpected passes" in text
        assert "metrics drift" in text
        assert "parse failures    : 1" in text
        assert "unavailable         : 2" in text


# ---------------------------------------------------------------------------
# Shared parse taxonomy (offline: synthetic payloads only)
# ---------------------------------------------------------------------------
class TestParseTaxonomy:
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ("(kicad_pcb (version 20221018)", "kicad_pcb"),
            ("  (kicad_sch (version 20231120)", "kicad_sch"),
            ("(kicad_symbol_lib (version 1)", "kicad-other:kicad_symbol_lib"),
            ("EESchema Schematic File Version 4", "legacy-eeschema"),
            ("PCBNEW-BOARD Version 1", "legacy-pcbnew"),
            ("|RECORD=Board|Altium", "altium"),
            ("", "empty"),
            ("hello world", "unknown"),
        ],
    )
    def test_detect_format(self, payload: str, expected: str) -> None:
        assert detect_format(payload) == expected

    def test_declared_version(self) -> None:
        assert declared_version(MINIMAL_PCB) == "20221018"
        assert declared_version("(kicad_pcb (generator pcbnew))") is None

    def test_signature_normalises_digits(self) -> None:
        first = signature_of(ValueError("Expected atom at position 4172"))
        second = signature_of(ValueError("Expected atom at position 91"))
        assert first == second, "digit-varying messages must group as one offender"
        assert first.startswith("ValueError: ")

    @pytest.mark.parametrize(
        ("exc", "expected_class"),
        [
            (RecursionError("deep"), "recursion-limit"),
            (MemoryError(), "out-of-memory"),
            (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), "encoding-error"),
            (KeyError("layers"), "missing-node-or-atom"),
            (IndexError("list index"), "missing-node-or-atom"),
            (AttributeError("no attr"), "unexpected-tree-shape"),
            (ValueError("bad float"), "unexpected-value"),
            (RuntimeError("what"), "uncaught-internal-error"),
        ],
    )
    def test_classify_exception(self, exc: BaseException, expected_class: str) -> None:
        assert classify_exception(exc) == expected_class

    def test_parse_payload_accepts_a_well_formed_board(self, tmp_path: Path) -> None:
        outcome, exc, elapsed = parse_payload(MINIMAL_PCB, "pcb", tmp_path, "ok", 1_000_000)
        assert outcome == OK
        assert exc is None
        assert elapsed >= 0.0
        assert list(tmp_path.iterdir()) == [], "scratch files must not be left behind"

    def test_parse_payload_buckets_a_truncated_board(self, tmp_path: Path) -> None:
        outcome, exc, _ = parse_payload(MINIMAL_PCB[:60], "pcb", tmp_path, "bad", 1_000_000)
        assert outcome in FAILURE_CLASSES
        assert exc is not None

    @pytest.mark.parametrize(
        ("payload", "kind", "max_bytes", "expected"),
        [
            ("   \n", "pcb", 1_000_000, SKIP_EMPTY),
            (MINIMAL_PCB, "pcb", 10, SKIP_OVERSIZE),
            (
                "EESchema Schematic File Version 4\n$EndSCHEMATC\n",
                "schematic",
                10_000,
                SKIP_NON_KICAD,
            ),
            (MINIMAL_PCB, "schematic", 1_000_000, SKIP_NON_KICAD),
        ],
    )
    def test_non_parser_outcomes_are_skips(
        self, payload: str, kind: str, max_bytes: int, expected: str, tmp_path: Path
    ) -> None:
        outcome, exc, _ = parse_payload(payload, kind, tmp_path, "skip", max_bytes)
        assert outcome == expected
        assert exc is None
        assert outcome not in FAILURE_CLASSES
