"""Tests for the #7688 embedded-checksum spike harness.

The harness's negative result ("not MurmurHash3, for any seed or fixed initial
state") rests on its pure-Python forward and inverse MurmurHash3. These tests
pin the forward functions to reference vectors from the ``mmh3`` package, then
check that each inverse recovers the initial state.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "research" / "embedded_checksum_probe.py"


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("embedded_checksum_probe_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# (data, seed, x64_128 digest, x86_128 digest), as mmh3.hash_bytes() prints them.
VECTORS = [
    (b"", 0, "00000000000000000000000000000000", "00000000000000000000000000000000"),
    (b"hello world\n", 0, "317a6925b8cbf822f389c50e7f1e7272", "206c689acd7163bdbadac1b88df6bf1b"),
    (
        b"The quick brown fox jumps over the lazy dog",
        0x9747B28C,
        "213163d23b7f8a73e516c07e727345f9",
        "5ed5d48a7161b84c9c3aa78e3e79b6cd",
    ),
    (bytes(range(37)), 1, "017a0e0503ebe35d11bfd67c08ddb624", "b0e076ed176786d17d0b7c91fc0af4c4"),
]


@pytest.mark.parametrize(("data", "seed", "x64", "x86"), VECTORS)
def test_forward_matches_reference(probe, data, seed, x64, x86):
    assert struct.pack("<QQ", *probe.mmh3_x64_128(data, seed)).hex() == x64
    assert struct.pack("<4I", *probe.mmh3_x86_128(data, seed)).hex() == x86


@pytest.mark.parametrize("length", [0, 1, 8, 9, 15, 16, 17, 100, 1023])
def test_inverse_recovers_seed_for_every_tail_length(probe, length):
    data = bytes((i * 131 + 7) & 0xFF for i in range(length))
    seed = 0xABCD1234
    assert probe.mmh3_x64_128_initial_state(data, *probe.mmh3_x64_128(data, seed)) == (seed, seed)
    assert probe.mmh3_x86_128_initial_state(data, probe.mmh3_x86_128(data, seed)) == [seed] * 4


def test_inverse_recovers_arbitrary_initial_state(probe):
    data = b"ISO-10303-21;\nHEADER;\n" * 5
    state = (0x0123456789ABCDEF, 0xFEDCBA9876543210)
    assert probe.mmh3_x64_128_initial_state(data, *probe.mmh3_x64_128(data, *state)) == state


def test_iter_samples_parses_multiline_data_block(probe, tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb\n\t(embedded_files\n\t\t(file\n\t\t\t(name "a b.step")\n'
        "\t\t\t(type model)\n\t\t\t(data |KLUv/QBYAQ\n\t\t\t\tAAaGk=|\n\t\t\t)\n"
        '\t\t\t(checksum "00ff00ff00ff00ff00ff00ff00ff00ff")\n\t\t)\n\t)\n)\n'
    )
    (s,) = list(probe.iter_samples([pcb]))
    assert s["name"] == "a b.step"
    assert s["type"] == "model"
    assert s["data"] == "KLUv/QBYAQAAaGk="
    assert s["checksum"] == "00FF00FF00FF00FF00FF00FF00FF00FF"


def test_footprint_level_blocks_without_data_are_skipped(probe, tmp_path):
    fp = tmp_path / "x.kicad_mod"
    fp.write_text('(embedded_files (file (name "x.step") (type model) (checksum "AB")))')
    assert list(probe.iter_samples([fp])) == []


def test_output_orders_are_distinct_byte_permutations(probe):
    digest = bytes(range(16))
    forms = probe.output_orders(digest)
    assert len(set(forms.values())) == len(forms)
    assert all(sorted(f) == sorted(digest) for f in forms.values())
