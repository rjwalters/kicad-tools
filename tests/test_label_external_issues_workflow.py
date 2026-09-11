"""Execute the actual GitHub Actions inline JavaScript against mocked REST calls."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/label-external-issues.yml"
TRUSTED = {"login": "loom-fleet-dispatch[bot]", "id": 313063438, "type": "Bot"}


def run_workflow(author, status=404):
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail("Node.js is required in CI for workflow JavaScript regression tests")
        pytest.skip("Node.js is required to execute the workflow inline JavaScript")
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["label_external"]["steps"]
    # Actions implicitly requires success() for these ordinary conditions.
    # Pin the conditions too, so making a write unconditional fails this test.
    for step in steps[1:]:
        assert step["if"] == "fromJSON(steps.check.outputs.result).isCollaborator == false"
    payload = {"scripts": [s["with"]["script"] for s in steps], "author": author, "status": status}
    program = r"""
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const calls = [];
const context = {
  repo: {owner: 'rjwalters', repo: 'kicad-tools'},
  issue: {number: 5130},
  payload: {issue: {user: input.author, author_association: 'CONTRIBUTOR'}}
};
const github = {rest: {
  repos: {checkCollaborator: async (args) => {
    calls.push({method: 'checkCollaborator', args});
    if (input.status === 204) return {status: 204};
    const error = new Error(input.status === null ? 'network unavailable' : `HTTP ${input.status}`);
    if (input.status !== null) error.status = input.status;
    throw error;
  }},
  issues: {
    addLabels: async (args) => calls.push({method: 'addLabels', args}),
    createComment: async (args) => calls.push({method: 'createComment', args})
  }
}};
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const execute = script => new AsyncFunction('github', 'context', 'console', script)(github, context, {log(){}});
(async () => {
  try {
    const result = await execute(input.scripts[0]);
    if (result.isCollaborator === false) {
      for (const script of input.scripts.slice(1)) await execute(script);
    }
    process.stdout.write(JSON.stringify({result, calls}));
  } catch (error) {
    process.stdout.write(JSON.stringify({error: error.message, calls}));
  }
})();
"""
    result = subprocess.run(
        [node, "-e", program],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def test_verified_fleet_app_bypasses_user_collaborator_lookup():
    result = run_workflow(TRUSTED)
    assert result["result"] == {"isCollaborator": True}
    assert result["calls"] == []


@pytest.mark.parametrize("status", [401, 403, 429, 500, None])
def test_api_errors_do_not_label_or_comment(status):
    result = run_workflow({"login": "outside", "id": 12, "type": "User"}, status)
    assert result["error"] == ("network unavailable" if status is None else f"HTTP {status}")
    assert [call["method"] for call in result["calls"]] == ["checkCollaborator"]


@pytest.mark.parametrize(
    "author",
    [
        {"login": "loom-fleet-dispatch[bot]", "id": 1, "type": "Bot"},
        {"login": "loom-fleet-dispatch[bot]", "id": "313063438", "type": "Bot"},
        {"login": "loom-fleet-dispatch[bot]", "id": 313063438, "type": "User"},
        {"login": "other[bot]", "id": 313063438, "type": "Bot"},
        {"login": "loom-fleet-dispatch", "id": 313063438, "type": "Bot"},
        {"login": "loom-fleet-dispatch[bot]", "type": "Bot"},
        {"login": "loom-fleet-dispatch[bot]", "id": 313063438},
        {"login": "dependabot[bot]", "id": 49699333, "type": "Bot"},
        {"login": "outside", "id": 12, "type": "User"},
    ],
)
def test_nonmatching_accounts_keep_external_gate(author):
    result = run_workflow(author, 404)
    assert result["result"] == {"isCollaborator": False}
    assert [c["method"] for c in result["calls"]] == [
        "checkCollaborator",
        "addLabels",
        "createComment",
    ]
    assert result["calls"][0]["args"] == {
        "owner": "rjwalters",
        "repo": "kicad-tools",
        "username": author["login"],
    }
    assert result["calls"][1]["args"] == {
        "owner": "rjwalters",
        "repo": "kicad-tools",
        "issue_number": 5130,
        "labels": ["external"],
    }
    assert "automatically labeled as `external`" in result["calls"][2]["args"]["body"]


@pytest.mark.parametrize(
    "author",
    [
        {"login": "maintainer", "id": 12, "type": "User"},
        {"login": "other[bot]", "id": 13, "type": "Bot"},
    ],
)
def test_other_accounts_can_still_be_verified_collaborators(author):
    result = run_workflow(author, 204)
    assert result["result"] == {"isCollaborator": True}
    assert [c["method"] for c in result["calls"]] == ["checkCollaborator"]


@pytest.mark.parametrize(
    "author",
    [None, {}, {"id": 313063438, "type": "Bot"}, {"login": None}, {"login": ""}, {"login": 123}],
)
def test_missing_author_metadata_fails_without_any_api_calls(author):
    result = run_workflow(author)
    assert result["error"] == "Issue author metadata is missing"
    assert result["calls"] == []


def test_ci_test_job_installs_node():
    workflow = yaml.safe_load((WORKFLOW.parent / "ci.yml").read_text())
    steps = workflow["jobs"]["test"]["steps"]
    prerequisites = next(
        step for step in steps if step.get("name") == "Ensure container has prerequisites"
    )
    assert "nodejs" in prerequisites["run"].split()


@pytest.mark.parametrize("ci", ["true", "TRUE"])
def test_missing_node_fails_in_ci(monkeypatch, ci):
    monkeypatch.setenv("CI", ci)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(pytest.fail.Exception, match="Node.js is required in CI"):
        run_workflow(TRUSTED)


def test_missing_node_can_skip_locally(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(pytest.skip.Exception, match="Node.js is required"):
        run_workflow(TRUSTED)
