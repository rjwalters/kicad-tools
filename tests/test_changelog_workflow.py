"""Workflow-YAML tests for ``.github/workflows/changelog.yml`` (issue #9383).

The fragment check's verdict is read by exact-head CI gates, which treat a
``cancelled`` check-run under ``Changelog Fragment Check`` as red. A shared
concurrency group with ``cancel-in-progress`` could strand exactly that row:
two ``pull_request`` events on one PR head inside the job's ~20 s runtime (a
PR created and immediately edited, a push plus a label flip) raced, and the
cancelled first run stayed red even after the newer run succeeded (observed
on PRs #9379 and #9375). These tests pin the per-run concurrency group, and
the evaluated job-name invariant that keeps every substantive verdict on the
required name (issue #8434 round 4) for the events the job does run on.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/changelog.yml"
WORKFLOW: dict[str, Any] = yaml.safe_load(WORKFLOW_PATH.read_text())
JOB = WORKFLOW["jobs"]["fragment-check"]
REQUIRED_NAME = "Changelog Fragment Check"


# --- the concurrency group (issue #9383) -----------------------------------


def test_concurrency_preserves_per_run_pr_isolation() -> None:
    group = WORKFLOW["concurrency"]["group"]
    # One group per run: no run of this workflow can cancel another, so no
    # `cancelled` check-run can exist under the required name for an
    # exact-head gate to misread.
    assert "github.run_id" in group
    # Not keyed on the PR number -- a `changelog-<PR>` group (alone or inside
    # a ternary) plus cancel-in-progress is exactly the #9383 false red.
    assert "github.event.pull_request.number" not in group
    # No action-conditional split back to a shared group for any event type;
    # label events (skipped or not) get the same per-run key, which matters
    # because concurrency is evaluated before the job `if:`.
    assert "github.event.action" not in group
    assert "format(" not in group


# --- a tiny GitHub-expression evaluator ------------------------------------
#
# Enough of the expression language for this workflow's `name:`/`if:` fields:
# `&&` / `||` with JS-style short-circuiting that returns operand values,
# `==` / `!=`, parentheses, single-quoted strings and dotted context paths
# (missing paths resolve to None, as GitHub's null). Anything else in the
# expression is a hard error, so a future syntax this cannot parse fails the
# test loudly instead of silently passing.

_TOKEN = re.compile(
    r"\s*(?:(?P<op>\|\||&&|!=|==|\(|\))"
    r"|(?P<str>'[^']*')"
    r"|(?P<path>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*))"
)


def _tokenize(expression: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    while pos < len(expression):
        match = _TOKEN.match(expression, pos)
        if match is None:
            if expression[pos:].strip() == "":
                break
            raise AssertionError(f"unsupported expression syntax at {expression[pos:]!r}")
        pos = match.end()
        assert match.lastgroup is not None
        tokens.append(match.group(match.lastgroup))
    return tokens


def _truthy(value: Any) -> bool:
    return value is not None and value is not False and value != ""


def _loose_eq(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    return left == right


class _Parser:
    def __init__(self, tokens: list[str], context: dict[str, Any]) -> None:
        self._tokens = tokens
        self._pos = 0
        self._context = context

    def _peek(self) -> str | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _next(self) -> str | None:
        token = self._peek()
        self._pos += 1
        return token

    def parse(self) -> Any:
        value = self._or()
        assert self._peek() is None, f"trailing tokens: {self._tokens[self._pos :]}"
        return value

    def _or(self) -> Any:
        value = self._and()
        while self._peek() == "||":
            self._next()
            right = self._and()
            value = value if _truthy(value) else right
        return value

    def _and(self) -> Any:
        value = self._comparison()
        while self._peek() == "&&":
            self._next()
            right = self._comparison()
            value = right if _truthy(value) else value
        return value

    def _comparison(self) -> Any:
        left = self._primary()
        op = self._peek()
        if op in ("==", "!="):
            self._next()
            right = self._primary()
            equal = _loose_eq(left, right)
            return equal if op == "==" else not equal
        return left

    def _primary(self) -> Any:
        token = self._next()
        assert token is not None, "unexpected end of expression"
        if token == "(":
            value = self._or()
            assert self._next() == ")", "unbalanced parenthesis"
            return value
        if token.startswith("'"):
            return token[1:-1]
        node: Any = self._context
        for part in token.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            else:
                node = None
        return node


def _evaluate(expression: str, context: dict[str, Any]) -> Any:
    return _Parser(_tokenize(expression), context).parse()


def _unwrap(field: str) -> str:
    text = JOB[field]
    assert isinstance(text, str) and text.startswith("${{") and text.endswith("}}")
    return text[3:-2].strip()


# --- the evaluated job-name invariant (issue #8434; kept by #9383) ---------


def test_non_label_events_evaluate_to_the_required_name() -> None:
    for action in ("opened", "synchronize", "reopened", "edited"):
        context = {"github": {"event": {"action": action}}}
        assert _evaluate(_unwrap("if"), context) is True
        assert _evaluate(_unwrap("name"), context) == REQUIRED_NAME


def test_changelog_skip_flips_run_and_hit_the_required_name() -> None:
    # A `changelog:skip` flip is the one label event the job runs on, so its
    # name MUST evaluate to the required name (header INVARIANT).
    for action in ("labeled", "unlabeled"):
        event = {"action": action, "label": {"name": "changelog:skip"}}
        context = {"github": {"event": event}}
        assert _evaluate(_unwrap("if"), context) is True
        assert _evaluate(_unwrap("name"), context) == REQUIRED_NAME


def test_other_label_events_are_skipped_off_the_required_name() -> None:
    # Any other label: the job is `if:`-skipped and the *unevaluated* name
    # text lands off the required name; the evaluated form must match that
    # expectation too (it reads `ignored label event`, not the constant).
    for action in ("labeled", "unlabeled"):
        event = {"action": action, "label": {"name": "loom:review-requested"}}
        context = {"github": {"event": event}}
        assert _evaluate(_unwrap("if"), context) is False
        assert _evaluate(_unwrap("name"), context) == "ignored label event"


def _concurrency(event_name: str, run_id: str, sha: str, action: str = "opened") -> tuple[str, Any]:
    context = {
        "github": {
            "event_name": event_name,
            "run_id": run_id,
            "sha": sha,
            "event": {"action": action, "pull_request": {"number": 9303}},
        }
    }
    group = re.sub(
        r"\$\{\{(.*?)\}\}",
        lambda match: str(_evaluate(match[1], context)),
        WORKFLOW["concurrency"]["group"],
    )
    cancel = WORKFLOW["concurrency"]["cancel-in-progress"]
    return group, _evaluate(cancel[3:-2], context)


def test_every_pr_event_has_its_own_group_isolated_from_merge_groups() -> None:
    groups = []
    for n, action in enumerate(
        ("opened", "synchronize", "reopened", "edited", "labeled", "unlabeled")
    ):
        for occurrence in (0, 1):
            group, cancel = _concurrency("pull_request", str(n * 2 + occurrence), "a" * 40, action)
            assert cancel is True
            groups.append(group)
    assert len(set(groups)) == len(groups)
    assert _concurrency("merge_group", "0", "a" * 40, "checks_requested")[0] not in groups


def test_merge_group_keys_are_sha_scoped_and_never_cancel() -> None:
    first = _concurrency("merge_group", "1", "a" * 40, "checks_requested")
    repeat = _concurrency("merge_group", "2", "a" * 40, "checks_requested")
    next_group = _concurrency("merge_group", "3", "b" * 40, "checks_requested")
    assert first == repeat
    assert first[0] != next_group[0]
    assert first[1] is False and next_group[1] is False


def test_merge_group_runs_tree_check_and_preserves_pr_only_gap_report() -> None:
    context = {"github": {"event_name": "merge_group", "event": {"action": "checks_requested"}}}
    assert _evaluate(_unwrap("if"), context) is True
    assert _evaluate(_unwrap("name"), context) == REQUIRED_NAME
    tree_check = next(
        step for step in JOB["steps"] if "changelog_assemble.py --check" in step.get("run", "")
    )
    assert "if" not in tree_check or _evaluate(tree_check["if"], context) is True
    gap = next(step for step in JOB["steps"] if "changelog_gap_report.py" in step.get("run", ""))
    assert _evaluate(gap["if"], context) is False
    assert '--pr "${{ github.event.pull_request.number }}"' in gap["run"]
    assert "--pr-event" not in gap["run"]
