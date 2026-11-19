"""Workflow-shape tests pinning ``ci.yml``'s merge-queue invariants (issue #9307).

PR #9303 (issue #9299) added a ``merge_group`` trigger to ``ci.yml`` and made
every relied-on job gate admit it. Nothing in-repo pinned that, so a later edit
that dropped one ``github.event_name == 'merge_group' ||`` disjunct would make
that job silently stop reporting on merge groups -- "a check that *cannot run*
must never look like one that passed" (``.loom/docs/ci-principles.md``).

What is pinned here, all by *evaluating* the workflow's own expressions rather
than matching their text:

* ``merge_group`` is a trigger.
* Every job gate admits a merge group, with the ``needs.*`` / ``vars.*`` inputs
  set to their worst case. Jobs deliberately exempt are listed in
  ``PR_ONLY_JOBS`` with a reason, and an exempt job must really be skipped
  (so the allow-list cannot go stale) and must not be a ``needs`` of a job
  that would then be skipped with it.
* The concurrency group for a merge group is ``github.sha``-derived and
  ``cancel-in-progress`` is false for it.

The gate assertions deliberately check only that a gate *admits* ``merge_group``,
not the expression's shape, so they hold whether ``!cancelled()`` sits inside
or is hoisted outside the ``merge_group ||`` disjunct (PRs #9416 / #9345).

Out of scope: ``changelog.yml`` (``tests/test_changelog_workflow.py``) and
``select_routed_pcbs.py`` (``tests/test_select_routed_pcbs.py``).
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
WORKFLOW: dict[str, Any] = yaml.safe_load(CI_WORKFLOW_PATH.read_text())

MERGE_GROUP_DISJUNCT = "github.event_name == 'merge_group' ||"

# Jobs that deliberately do NOT run on a merge group, with the reason. Each is
# also marked ``# merge-group-audit: pr-only`` in ci.yml.
PR_ONLY_JOBS: dict[str, str] = {
    "changes": (
        "path-filter detection gates PR runs only; on a merge group every dependent "
        "job takes the `!= 'pull_request'` branch of its gate and runs in full"
    ),
}

# The comment marker each PR-only job must carry in ci.yml.
AUDIT_MARKER = "merge-group-audit: pr-only"

# --- a tiny GitHub-expression evaluator ------------------------------------
#
# Enough of the expression language for ci.yml's `if:` / `concurrency:` fields:
# `&&` / `||` with JS-style short-circuiting that returns operand values, `!`,
# `==` / `!=`, parentheses, single-quoted strings, dotted context paths
# (missing paths resolve to None, as GitHub's null) and the functions
# `cancelled()` and `format()`. Anything else is a hard error, so syntax this
# cannot parse fails the test loudly instead of silently passing.

_TOKEN = re.compile(
    r"\s*(?:(?P<op>\|\||&&|!=|==|!|\(|\)|,)"
    r"|(?P<str>'(?:[^']|'')*')"
    r"|(?P<path>[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*))"
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
    return value is not None and value is not False and value != "" and value != 0


def _loose_eq(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
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
        left = self._unary()
        op = self._peek()
        if op in ("==", "!="):
            self._next()
            right = self._unary()
            equal = _loose_eq(left, right)
            return equal if op == "==" else not equal
        return left

    def _unary(self) -> Any:
        if self._peek() == "!":
            self._next()
            return not _truthy(self._unary())
        return self._primary()

    def _primary(self) -> Any:
        token = self._next()
        assert token is not None, "unexpected end of expression"
        if token == "(":
            value = self._or()
            assert self._next() == ")", "unbalanced parenthesis"
            return value
        if token.startswith("'"):
            return token[1:-1].replace("''", "'")
        if self._peek() == "(":
            return self._call(token)
        node: Any = self._context
        for part in token.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return node

    def _call(self, name: str) -> Any:
        assert self._next() == "("
        args: list[Any] = []
        if self._peek() != ")":
            args.append(self._or())
            while self._peek() == ",":
                self._next()
                args.append(self._or())
        assert self._next() == ")", f"unbalanced call to {name}()"
        if name == "cancelled":
            assert not args
            return bool(self._context.get("__cancelled__", False))
        if name == "format":
            template, *values = args
            return re.sub(
                r"\{(\d+)\}",
                lambda m: str(values[int(m[1])]),
                str(template),
            )
        raise AssertionError(f"unsupported function {name}() in workflow expression")


def _evaluate(expression: str, context: dict[str, Any]) -> Any:
    return _Parser(_tokenize(expression), context).parse()


def _unwrap(expression: str) -> str:
    """Strip an optional ``${{ ... }}`` wrapper."""
    text = expression.strip()
    if text.startswith("${{") and text.endswith("}}"):
        return text[3:-2].strip()
    return text


def _interpolate(template: str, context: dict[str, Any]) -> str:
    """Evaluate every ``${{ ... }}`` span in a string field."""
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: str(_evaluate(m[1], context)), template)


# --- the invariants, as functions over a workflow dict ---------------------


def _trigger_block(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) parses the bare `on:` key as boolean True.
    return workflow[True] if True in workflow else workflow["on"]


def _merge_group_context(**overrides: Any) -> dict[str, Any]:
    """A merge-group run with every ``needs`` / ``vars`` input at its worst case.

    ``changes`` is skipped on a merge group (it is pull_request-only) so it has
    no outputs and a ``skipped`` result; ``kicad_pin`` reports ``skip == 'true'``
    (its only non-default value, set on the hourly schedule); the opt-in
    ``vars.BOARD_07_CI_ENABLED`` repo variable is unset.
    """
    context: dict[str, Any] = {
        "github": {
            "event_name": "merge_group",
            "sha": "a" * 40,
            "ref": "refs/heads/gh-readonly-queue/main/pr-1",
        },
        "needs": {
            "changes": {"result": "skipped", "outputs": {}},
            "kicad_pin": {"result": "success", "outputs": {"skip": "true"}},
        },
        "vars": {},
    }
    context.update(overrides)
    return context


def _gate_admits_merge_group(job: dict[str, Any]) -> bool:
    if "if" not in job:
        return True
    gate = job["if"]
    if isinstance(gate, bool):
        return gate
    return _truthy(_evaluate(_unwrap(str(gate)), _merge_group_context()))


def _merge_group_gate_violations(workflow: dict[str, Any]) -> list[str]:
    """Human-readable problems with the per-job merge-group gates."""
    problems: list[str] = []
    jobs: dict[str, dict[str, Any]] = workflow["jobs"]
    for name, job in jobs.items():
        admits = _gate_admits_merge_group(job)
        if name in PR_ONLY_JOBS:
            if admits:
                problems.append(f"{name}: listed as pr-only but its gate admits merge_group")
            continue
        if not admits:
            problems.append(f"{name}: gate does not admit merge_group: {job.get('if')!r}")
            continue
        # Without cancelled()/always() the job carries an implicit success(), so
        # a `needs` on a skipped pr-only job would skip it on a merge group too.
        gate_text = str(job.get("if", ""))
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        if "cancelled()" not in gate_text and "always()" not in gate_text:
            for need in needs:
                if need in PR_ONLY_JOBS:
                    problems.append(f"{name}: needs pr-only job {need!r} without cancelled()")
    return problems


def _concurrency(workflow: dict[str, Any], event_name: str, sha: str, ref: str) -> tuple[str, Any]:
    context = {"github": {"event_name": event_name, "sha": sha, "ref": ref, "workflow": "CI"}}
    group = _interpolate(workflow["concurrency"]["group"], context)
    cancel = workflow["concurrency"]["cancel-in-progress"]
    if isinstance(cancel, str):
        cancel = _evaluate(_unwrap(cancel), context)
    return group, cancel


def _concurrency_violations(workflow: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    ref = "refs/heads/gh-readonly-queue/main/pr-1"
    first = _concurrency(workflow, "merge_group", "a" * 40, ref)
    repeat = _concurrency(workflow, "merge_group", "a" * 40, ref)
    other = _concurrency(workflow, "merge_group", "b" * 40, ref)
    if first != repeat:
        problems.append("merge_group group is not stable for one sha")
    if first[0] == other[0]:
        problems.append("merge_group group does not change with github.sha")
    if "a" * 40 not in first[0]:
        problems.append("merge_group group is not github.sha-derived")
    if first[1] is not False or other[1] is not False:
        problems.append("cancel-in-progress is not false for merge_group")
    pr_group, _ = _concurrency(workflow, "pull_request", "a" * 40, "refs/pull/1/merge")
    if pr_group == first[0]:
        problems.append("merge_group group collides with the pull_request group")
    return problems


def _pr_only_job_block(raw: str, name: str) -> str:
    """Slice one PR-only job's own body out of ci.yml's raw text.

    The block runs from the ``\\n  <name>:\\n`` job header to the next
    2-space-indented key (i.e. the next job) or EOF. Bounding on a
    ``runs-on:`` line would not work: keys *inside* a job are indented four
    spaces in ci.yml, so a 2-space ``runs-on`` never occurs and the slice
    would silently run to EOF (#9497), letting a marker anywhere later in
    the file satisfy the check for every PR-only job.
    """
    tail = raw.split(f"\n  {name}:\n", 1)[1]
    return re.split(r"\n  [A-Za-z0-9_-]+:\n", tail, maxsplit=1)[0]


def _pr_only_marker_problems(raw: str) -> list[str]:
    """PR-only jobs whose own ci.yml body lost the audit marker."""
    return [
        f"{name} lost its audit marker"
        for name in PR_ONLY_JOBS
        if AUDIT_MARKER not in _pr_only_job_block(raw, name)
    ]


# --- the tests -------------------------------------------------------------


def test_merge_group_is_a_trigger() -> None:
    triggers = _trigger_block(WORKFLOW)
    assert "merge_group" in triggers
    assert triggers["merge_group"]["types"] == ["checks_requested"]


def test_every_job_gate_admits_merge_group() -> None:
    assert _merge_group_gate_violations(WORKFLOW) == []


def test_gate_check_is_not_vacuous() -> None:
    # The suite really has gated jobs, and the evaluator tells a merge group
    # apart from a PR run: `typecheck` must be false for a worst-case PR run.
    gated = [n for n, j in WORKFLOW["jobs"].items() if "if" in j and n not in PR_ONLY_JOBS]
    assert len(gated) >= 10
    pr_context = _merge_group_context()
    pr_context["github"]["event_name"] = "pull_request"
    typecheck_gate = _unwrap(WORKFLOW["jobs"]["typecheck"]["if"])
    assert _truthy(_evaluate(typecheck_gate, pr_context)) is False


def test_pr_only_jobs_exist_and_carry_the_audit_marker() -> None:
    for name in PR_ONLY_JOBS:
        assert name in WORKFLOW["jobs"], f"stale PR_ONLY_JOBS entry {name!r}"
    assert _pr_only_marker_problems(CI_WORKFLOW_PATH.read_text()) == []


def test_pr_only_job_block_is_bounded_to_the_jobs_own_body() -> None:
    # Shapes the real file must keep handling: a job whose first key is not
    # `runs-on` (the old bound hung off that literal), and the last job in
    # the file, where no following key exists and the block runs to EOF.
    text = "\n  first:\n    needs: setup\n  last:\n    runs-on: ubuntu-latest\n"
    assert _pr_only_job_block(text, "first") == "    needs: setup"
    assert _pr_only_job_block(text, "last") == "    runs-on: ubuntu-latest\n"


def test_audit_marker_moved_to_another_job_is_detected() -> None:
    # Regression test for #9497: the block used to be sliced at a 2-space
    # `"\n  runs-on:"`, which never matches ci.yml (job keys sit at four
    # spaces), so `block` ran to EOF and a marker anywhere later in the file
    # satisfied the check for *every* PR-only job. Move the marker from
    # `changes` to `site`: the sliced block must notice the loss.
    raw = CI_WORKFLOW_PATH.read_text()
    assert raw.count(AUDIT_MARKER) == 1
    mutated = raw.replace(AUDIT_MARKER, "merge-group-audit: moved", 1)
    site_header = "\n  site:\n"
    assert site_header in mutated
    mutated = mutated.replace(
        site_header, f"{site_header}    # {AUDIT_MARKER} -- decoy in another job\n", 1
    )
    assert _pr_only_marker_problems(mutated) == ["changes lost its audit marker"]


def test_dropping_the_merge_group_disjunct_is_detected() -> None:
    # Mutate a copy: strip the leading disjunct from each gate that carries it.
    # Every such gate must then fail the check -- otherwise the worst-case
    # inputs are too lenient to catch the regression this test exists for.
    carriers = [
        name
        for name, job in WORKFLOW["jobs"].items()
        if MERGE_GROUP_DISJUNCT in str(job.get("if", ""))
    ]
    assert len(carriers) >= 10
    for name in carriers:
        mutated = copy.deepcopy(WORKFLOW)
        gate = mutated["jobs"][name]["if"]
        mutated["jobs"][name]["if"] = gate.replace(MERGE_GROUP_DISJUNCT, "", 1)
        problems = _merge_group_gate_violations(mutated)
        assert any(p.startswith(f"{name}:") for p in problems), (
            f"removing the merge_group disjunct from {name} went unnoticed"
        )


@pytest.mark.parametrize("hoisted", [False, True])
def test_gate_shape_agnostic_to_cancelled_hoist(hoisted: bool) -> None:
    # #9416 / #9345 hoist `!cancelled()` outside the `merge_group ||` disjunct
    # in the two BOARD_07 gates. Both shapes admit a merge group.
    mutated = copy.deepcopy(WORKFLOW)
    gate = "${{ github.event_name == 'merge_group' || ((vars.BOARD_07_CI_ENABLED == 'true') && !cancelled() && (needs.changes.outputs.board07 == 'true')) }}"
    if hoisted:
        gate = "${{ !cancelled() && (github.event_name == 'merge_group' || ((vars.BOARD_07_CI_ENABLED == 'true') && (needs.changes.outputs.board07 == 'true'))) }}"
    mutated["jobs"]["board-07-end-to-end"]["if"] = gate
    assert _merge_group_gate_violations(mutated) == []


def test_pr_only_job_needed_without_cancelled_is_detected() -> None:
    mutated = copy.deepcopy(WORKFLOW)
    mutated["jobs"]["lint"]["needs"] = "changes"  # no `if:`, implicit success()
    problems = _merge_group_gate_violations(mutated)
    assert any(p.startswith("lint:") for p in problems)


def test_merge_group_concurrency_is_sha_scoped_and_never_cancels() -> None:
    assert _concurrency_violations(WORKFLOW) == []


def test_concurrency_regressions_are_detected() -> None:
    # Group not sha-derived (keyed on the ref instead).
    by_ref = copy.deepcopy(WORKFLOW)
    by_ref["concurrency"]["group"] = by_ref["concurrency"]["group"].replace(
        "format('ci-merge-group-{0}', github.sha)", "format('ci-merge-group-{0}', github.ref)"
    )
    assert by_ref["concurrency"]["group"] != WORKFLOW["concurrency"]["group"]
    assert any("sha" in p for p in _concurrency_violations(by_ref))

    # cancel-in-progress true for merge_group.
    cancelling = copy.deepcopy(WORKFLOW)
    cancelling["concurrency"]["cancel-in-progress"] = True
    assert any("cancel-in-progress" in p for p in _concurrency_violations(cancelling))


def test_evaluator_rejects_unsupported_syntax() -> None:
    # A construct the evaluator cannot parse must fail loudly, not pass.
    with pytest.raises(AssertionError):
        _evaluate("contains(github.ref, 'x')", _merge_group_context())
    with pytest.raises(AssertionError):
        _evaluate("github.event_name ~ 'x'", _merge_group_context())
