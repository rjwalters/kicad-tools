"""Relative markdown links survive being copied into a manufacturing bundle (Issue #6899)."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "boards" / "_bundle_links.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("_bundle_links_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "manufacturing"
    (root / "design-source").mkdir(parents=True)
    (root / "firmware").mkdir()
    (root / "firmware" / "README.md").write_text("fw\n")
    (root / "design-source" / "DESIGN_REVIEW.md").write_text("dr\n")
    return root, root / "design-source"


def rw(mod, text, bundle):
    root, dst = bundle
    return mod.rewrite_markdown_links(text, dst, root)


def test_rebases_link_to_sibling_dir(mod, bundle):
    assert rw(mod, "[fw](firmware/README.md)\n", bundle) == "[fw](../firmware/README.md)\n"


def test_rebases_directory_and_keeps_anchor(mod, bundle):
    out = rw(mod, "[a](firmware/) [b](firmware/README.md#top)\n", bundle)
    assert out == "[a](../firmware/) [b](../firmware/README.md#top)\n"


def test_demotes_missing_target(mod, bundle):
    out = rw(mod, "see [ex](../../examples/01/)\n", bundle)
    assert out == "see ex (../../examples/01/, not included in this bundle)\n"


def test_valid_link_anchor_and_urls_untouched(mod, bundle):
    text = "[a](DESIGN_REVIEW.md) [b](#x) [c](https://e.com/x) [d](mailto:a@b.c)\n"
    assert rw(mod, text, bundle) == text


def test_code_is_skipped(mod, bundle):
    text = "```\n[x](nope.md)\n```\n`[y](nope.md)`\n"
    assert rw(mod, text, bundle) == text


def test_reference_style(mod, bundle):
    out = rw(mod, "[fw]: firmware/README.md\n[gone]: nope.md\n", bundle)
    assert out == "[fw]: ../firmware/README.md\ngone: nope.md (not included in this bundle)\n"


def test_idempotent(mod, bundle):
    text = "[fw](firmware/README.md) [ex](../../examples/) [r]: x\n[z]: nope.md\n"
    once = rw(mod, text, bundle)
    assert rw(mod, once, bundle) == once


def test_copy_into_bundle_only_rewrites_markdown(mod, bundle, tmp_path):
    root, dst = bundle
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("[fw](firmware/README.md)\n")
    (src / "a.py").write_text("[fw](firmware/README.md)\n")
    mod.copy_into_bundle(src / "README.md", dst / "README.md", root)
    mod.copy_into_bundle(src / "a.py", dst / "a.py", root)
    assert (dst / "README.md").read_text() == "[fw](../firmware/README.md)\n"
    assert (dst / "a.py").read_text() == "[fw](firmware/README.md)\n"


_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)")
_FENCE = re.compile(r"```.*?```", re.S)


def test_shipped_bundles_have_no_broken_relative_links():
    broken = []
    for md in sorted((ROOT / "boards").glob("*/output/manufacturing/**/*.md")):
        text = _FENCE.sub("", md.read_text(encoding="utf-8"))
        for target in _LINK.findall(text):
            if target.startswith(("#", "/")) or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                continue
            path = target.split("#")[0].split("?")[0]
            if path and not (md.parent / path).exists():
                broken.append(f"{md.relative_to(ROOT)} -> {target}")
    assert not broken, "\n".join(broken)
