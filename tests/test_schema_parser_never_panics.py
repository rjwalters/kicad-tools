"""Never-panic / deterministic property test for the schema parsers.

Item 5 (issue #4880, "KiCad-demos conformance corpus + never-panic parser
property test") asks for a property-style test asserting that
``schema/pcb.py`` and ``schema/schematic.py`` never raise an *unhandled*
exception on malformed/truncated input, and that parsing the same
well-formed input repeatedly is deterministic.

**Dependency decision (hypothesis vs. manual fuzzing): manual seeded fuzz
loop, no new dependency.** ``hypothesis`` is not a dependency of this repo
today (``grep -n hypothesis pyproject.toml`` returns nothing) and no other
test module uses it. The "never panic" property tested here is bounded to a
small, fixed set of known-good seed fixtures mutated via deterministic
truncation (every prefix length) plus a small ``random.Random(seed)``
mutation loop -- both fully reproducible from a plain integer seed without
hypothesis's shrinking engine, which exists to minimize *arbitrary*
generated counterexamples. A truncation failure here is already minimal (a
prefix), so shrinking buys little, and adding a new dev-only dependency for
one test module is not worth the ongoing maintenance/version-pin surface for
that marginal benefit. Revisit this decision if a future property test needs
richer structured-input generation (e.g. mutating typed field values, not
just bytes) that a hand-rolled loop would make painful.

**What "never panic" means for a pure-Python parser.** Unlike Rust's
``proptest``, no Python exception can crash the interpreter outright --
every exception is technically "caught" once it reaches a bare ``except``.
The meaningful property is therefore narrower: malformed input must produce
either (a) a successfully parsed object, or (b) one of the small set of
*deliberate* parse-failure exceptions the codebase already raises for this
purpose (``ParseError`` -- a ``ValueError`` subclass --, ``KeyError`` from
``SExp.__getitem__``, ``IndexError`` from ``SExp.get_atom``, or a
``kicad_tools.exceptions.KiCadToolsError`` such as ``FileFormatError``).
Anything else (``AttributeError``, bare ``TypeError``, ``ZeroDivisionError``,
...) indicates a code path that assumed well-formed input and crashed on a
malformed one -- exactly the "silently half-understood schema" defect shape
that #4873 (legacy ``(module ...)`` boards) and #4874 (legacy ``gr_arc``
missing ``mid``) are instances of, just surfacing as a raised exception
instead of silently-wrong data.

This module found and drove the fix for one such bug during development:
``PCB._parse_layers()`` called ``int(child.tag)`` guarded only by
``except ValueError`` -- a malformed nested list gives ``child.tag is None``,
and ``int(None)`` raises ``TypeError``, not ``ValueError``, escaping the
guard. Fixed in the same PR that adds this test (`src/kicad_tools/schema/pcb.py`,
``_parse_layers``) by also catching ``TypeError``.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from kicad_tools.exceptions import KiCadToolsError
from kicad_tools.schema.pcb import PCB
from kicad_tools.schema.schematic import Schematic
from kicad_tools.sexp import parse_string

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Exceptions the parser is allowed to raise on malformed input -- these are
# deliberate, documented "this input is not well-formed" signals, not
# unhandled crashes. See the module docstring for the rationale.
ALLOWED_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ValueError,  # covers kicad_tools.sexp.parser.ParseError
    KeyError,
    IndexError,
    KiCadToolsError,  # covers FileFormatError, FileNotFoundError, etc.
)

# Small, self-contained seed fixtures (no external symbol/footprint library
# references) -- one PCB, one schematic. Each (path, constructor, root tag).
SEED_FIXTURES = [
    (FIXTURES_DIR / "copper_sliver_positive.kicad_pcb", PCB, "kicad_pcb"),
    (FIXTURES_DIR / "simple_rc.kicad_sch", Schematic, "kicad_sch"),
]

MUTATION_CHARS = "()\"'  \n\tabcXYZ0129.-"


def _try_parse(text: str, ctor: type, expected_tag: str) -> None:
    """Parse ``text`` and, if it looks like the right root tag, construct it.

    Raises whatever exception escapes; callers decide what's acceptable.
    """
    sexp = parse_string(text)
    if sexp.tag == expected_tag:
        ctor(sexp, None)


def _mutate(chars: list[str], rng: random.Random) -> str:
    """Apply a small number of random byte-level mutations to ``chars``."""
    mutated = chars[:]
    for _ in range(rng.randint(1, 8)):
        if not mutated:
            break
        op = rng.choice(("del", "ins", "flip", "dup"))
        idx = rng.randrange(len(mutated))
        if op == "del":
            del mutated[idx]
        elif op == "ins":
            mutated.insert(idx, rng.choice(MUTATION_CHARS))
        elif op == "flip":
            mutated[idx] = rng.choice(MUTATION_CHARS)
        elif op == "dup":
            mutated.insert(idx, mutated[idx])
    return "".join(mutated)


def _seed_fixture_ids() -> list[str]:
    return [path.name for path, _ctor, _tag in SEED_FIXTURES]


class TestNeverPanicOnTruncation:
    """Every prefix of a well-formed file is parsed gracefully or rejected."""

    @pytest.mark.timeout(60)
    @pytest.mark.parametrize("path,ctor,tag", SEED_FIXTURES, ids=_seed_fixture_ids())
    def test_every_truncation_length_is_graceful(self, path: Path, ctor: type, tag: str) -> None:
        text = path.read_text(encoding="utf-8")
        failures: list[tuple[int, str, str]] = []
        for cut in range(len(text) + 1):
            truncated = text[:cut]
            try:
                _try_parse(truncated, ctor, tag)
            except ALLOWED_EXCEPTIONS:
                continue
            except Exception as exc:  # noqa: BLE001 -- deliberately broad; see below
                failures.append((cut, type(exc).__name__, str(exc)[:200]))
        assert not failures, (
            f"{path.name}: truncating to these lengths raised an "
            f"un-allow-listed exception instead of a graceful parse failure "
            f"(first 5 shown): {failures[:5]}"
        )


class TestNeverPanicOnRandomMutation:
    """Seeded random byte mutations are parsed gracefully or rejected."""

    # Fixed seeds -> fully reproducible without re-running a search.
    SEEDS = (1, 42, 999)
    TRIALS_PER_SEED = 500

    @pytest.mark.timeout(60)
    @pytest.mark.parametrize("path,ctor,tag", SEED_FIXTURES, ids=_seed_fixture_ids())
    def test_seeded_mutations_are_graceful(self, path: Path, ctor: type, tag: str) -> None:
        text = path.read_text(encoding="utf-8")
        chars = list(text)
        failures: list[tuple[int, int, str, str]] = []
        for seed in self.SEEDS:
            rng = random.Random(seed)
            for trial in range(self.TRIALS_PER_SEED):
                mutated_text = _mutate(chars, rng)
                try:
                    _try_parse(mutated_text, ctor, tag)
                except ALLOWED_EXCEPTIONS:
                    continue
                except Exception as exc:  # noqa: BLE001 -- deliberately broad; see below
                    failures.append((seed, trial, type(exc).__name__, str(exc)[:200]))
        assert not failures, (
            f"{path.name}: seeded mutation(s) raised an un-allow-listed "
            f"exception instead of a graceful parse failure (seed, trial, "
            f"type, message; first 5 shown): {failures[:5]}"
        )


class TestDeterministicParse:
    """Parsing the same well-formed input repeatedly yields identical output."""

    @pytest.mark.parametrize("path,ctor,tag", SEED_FIXTURES, ids=_seed_fixture_ids())
    def test_repeated_parse_is_deterministic(self, path: Path, ctor: type, tag: str) -> None:
        text = path.read_text(encoding="utf-8")
        outputs = set()
        for _ in range(5):
            sexp = parse_string(text)
            assert sexp.tag == tag
            ctor(sexp, None)  # exercise the schema layer too, not just sexp
            outputs.add(sexp.to_string())
        assert len(outputs) == 1, (
            f"{path.name}: repeated parses of the same well-formed input "
            f"produced different serialized output -- the parser is not "
            f"deterministic."
        )
