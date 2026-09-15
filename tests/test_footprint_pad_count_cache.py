"""Command-scoped reuse must not hide footprint edits or parse failures."""

import os
from unittest.mock import patch

import pytest

from kicad_tools.cli import sch_suggest_footprint as module
from kicad_tools.exceptions import FileFormatError


def write_footprint(path, pads):
    path.write_text('(footprint "x" ' + " ".join(f'(pad "{i}" smd)' for i in pads) + ")")


def test_reuses_counts_but_invalidates_same_size_restored_mtime_edit(tmp_path):
    path = tmp_path / "x.kicad_mod"
    write_footprint(path, ["1", "2"])
    cache = module._FootprintPadCounts()
    with patch.object(module, "load_footprint", wraps=module.load_footprint) as load:
        assert cache.count(path) == 2
        assert cache.count(path) == 2
        assert load.call_count == 1
        stat = path.stat()
        path.write_text(path.read_text().replace("(pad", "(xyz", 1))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert cache.count(path) == 1
        assert load.call_count == 2


def test_deleted_and_invalid_files_do_not_reuse_previous_count(tmp_path):
    path = tmp_path / "x.kicad_mod"
    write_footprint(path, ["1"])
    cache = module._FootprintPadCounts()
    assert cache.count(path) == 1
    path.unlink()
    with pytest.raises(OSError):
        cache.count(path)
    path.write_text("(wrong_tag)")
    with pytest.raises(FileFormatError):
        cache.count(path)
    write_footprint(path, ["1", "2"])
    assert cache.count(path) == 2


def test_file_changed_during_parse_is_not_cached(tmp_path):
    path = tmp_path / "x.kicad_mod"
    write_footprint(path, ["1"])
    cache = module._FootprintPadCounts()
    original = module.load_footprint

    def changing_load(path):
        result = original(path)
        write_footprint(path, ["1", "2"])
        return result

    with patch.object(module, "load_footprint", side_effect=changing_load):
        assert cache.count(path) == 1
    assert cache.count(path) == 2


def test_cache_is_bounded_and_separate_commands_do_not_share_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_MAX_CACHED_PAD_COUNTS", 2)
    cache = module._FootprintPadCounts()
    paths = [tmp_path / f"{i}.kicad_mod" for i in range(3)]
    for path in paths:
        write_footprint(path, ["1"])
        assert cache.count(path) == 1
    assert len(cache._counts) == 2
    with patch.object(module, "load_footprint", wraps=module.load_footprint) as load:
        assert cache.count(paths[0]) == 1
        assert module._FootprintPadCounts().count(paths[0]) == 1
        assert load.call_count == 2


def test_cached_search_preserves_scan_cap_and_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_MAX_SCANNED_FOOTPRINTS", 2)
    monkeypatch.setattr(
        module, "_candidate_library_paths", lambda *a, **kw: [("test", tmp_path, "project")]
    )
    for name in ["a", "b", "c"]:
        write_footprint(tmp_path / f"{name}.kicad_mod", ["1"])
    expected = module.find_footprint_candidates(None, 1, None, 20)
    assert [item["footprint"] for item in expected] == ["a", "b"]
    cache = module._FootprintPadCounts()
    with patch.object(module, "load_footprint", wraps=module.load_footprint) as load:
        for _ in range(2):
            assert module.find_footprint_candidates(None, 1, None, 20, pad_counts=cache) == expected
        assert load.call_count == 2
