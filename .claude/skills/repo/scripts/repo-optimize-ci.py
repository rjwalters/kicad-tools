#!/usr/bin/env python3
"""Deterministic half of /repo:optimize-ci — audit GitHub Actions workflows.

Parses .github/workflows/*.y(a)ml, extracts triggers, path filters, caches,
concurrency, and toolchains, cross-references required status checks (classic
branch protection + rulesets), and — when Actions history is readable —
estimates minutes per finding from recent runs. Emits a ranked report (text or
--json) that commands/repo/optimize-ci.md reasons over.

READ-ONLY by construction: every GitHub call is a GET. Writing proposed edits
(`--apply`) is the prompt's job, behind its own confirmation.

Subcommands:
  scan    Offline: analyze a local checkout. Required checks are supplied by
          flag (--required CONTEXT, repeatable, or --required-unknown), so the
          analysis is testable with no network.
  report  Online: scan (a local checkout, or --remote to read the workflows via
          the contents API) plus required-check lookup and run-history
          evidence. Degrades to "not measured" / "unknown" rather than failing.

Python 3.9+ and nothing outside the standard library. Workflow YAML is read by
a small built-in block-YAML reader (below) rather than PyYAML: PyYAML is not
guaranteed on a consumer machine, and under YAML 1.1 it parses the `on:` key as
boolean True — the one key this audit cares most about.
"""

import argparse
import base64
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# Files whose change can never affect a build or test run in a typical repo.
# Deliberately NOT `**/*.md`: in a prompt/skill repo (this one included)
# markdown IS the product and is exercised by the test suite. The prompt still
# has to verify no job reads these (README-layout tests, doc linters) before
# recommending an ignore list — see optimize-ci.md.
DOC_ONLY_PATTERNS = [
    "docs/**",
    "**/README*",
    "CHANGELOG*",
    "CONTRIBUTING*",
    "CODE_OF_CONDUCT*",
    "SECURITY*",
    "LICENSE*",
    "AUTHORS*",
    ".github/ISSUE_TEMPLATE/**",
    ".github/PULL_REQUEST_TEMPLATE*",
    ".github/pull_request_template*",
]

# Toolchain -> (root-level input files that affect it, when present).
TOOLCHAIN_INPUTS = {
    "node": ["package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
             "npm-shrinkwrap.json", "pnpm-workspace.yaml", ".nvmrc", ".node-version"],
    "python": ["pyproject.toml", "requirements.txt", "requirements-dev.txt",
               "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock", "setup.py",
               "setup.cfg", ".python-version"],
    "rust": ["Cargo.toml", "Cargo.lock", "rust-toolchain", "rust-toolchain.toml"],
    "go": ["go.mod", "go.sum"],
    "docker": ["Dockerfile", ".dockerignore"],
}
LOCKFILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "npm-shrinkwrap.json",
             "uv.lock", "poetry.lock", "Pipfile.lock", "Cargo.lock", "go.sum"}

RECOMMENDED_CONCURRENCY = (
    "concurrency:\n"
    "  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}\n"
    "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}"
)

JOB_LEVEL_FILTER = (
    "Keep the workflow triggering on every PR and filter at JOB level instead: add a\n"
    "`changes` job running dorny/paths-filter, gate the heavy jobs with\n"
    "`if: needs.changes.outputs.<filter> == 'true'`, and add an always-running\n"
    "aggregator job (`if: always()`, `needs: [<all gated jobs>]`, failing only if a\n"
    "needed job failed or was cancelled) — then make THAT aggregator the required\n"
    "check. A skipped job reports as success; a skipped workflow reports nothing."
)


# `heavy-default-branch-suite` fires when a workflow that runs on PRs AND on
# default-branch pushes costs at least this many runner-minutes per day on the
# default branch (job-minutes of original push attempts, see main_stats).
DEFAULT_HEAVY_MAIN_MINUTES_PER_DAY = 300.0
# Default-branch runs whose jobs are fetched to measure the per-run cost.
MAIN_JOBS_SAMPLE = 10
# Findings whose estimate includes avoided default-branch runs. They describe
# overlapping populations, so aggregation takes the largest, never the sum.
DEFAULT_BRANCH_MEASURES = ("default-branch-overlap", "heavy-default-branch")

class YamlError(Exception):
    pass


# ---------------------------------------------------------------------------
# Minimal block-YAML reader: block mappings/sequences, single-line flow
# collections, quoted and plain scalars (incl. multi-line plain), block
# scalars (| and >), comments. Anchors/aliases/tags are kept as plain strings.
# That is the subset GitHub workflow files use in practice.
# ---------------------------------------------------------------------------

def _strip_comment(text):
    out, quote = [], None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"') and (i == 0 or text[i - 1] in " \t:[{,-"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _split_key(text):
    """Return (key, rest) if text is `key: rest` / `key:`, else None."""
    if text[:1] in ("'", '"'):
        q = text[0]
        end = text.find(q, 1)
        while end != -1 and q == "'" and text[end + 1:end + 2] == "'":
            end = text.find(q, end + 2)
        if end == -1:
            return None
        after = text[end + 1:].lstrip()
        if not after.startswith(":"):
            return None
        return _scalar(text[:end + 1]), after[1:].strip()
    if text[:1] in ("[", "{"):
        return None
    depth = 0
    for i, ch in enumerate(text):
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0 and (i + 1 == len(text) or text[i + 1] in " \t"):
            if text.startswith("${{") and "}}" not in text[:i]:
                continue
            return text[:i].strip(), text[i + 1:].strip()
    return None


def _scalar(text):
    t = text.strip()
    if not t:
        return None
    if t[0] == '"' and t.endswith('"') and len(t) >= 2:
        try:
            return json.loads(t)
        except ValueError:
            return t[1:-1]
    if t[0] == "'" and t.endswith("'") and len(t) >= 2:
        return t[1:-1].replace("''", "'")
    if t[0] in "[{":
        return _flow(t)
    # YAML 1.2 core schema only: `on`/`yes`/`no` stay strings.
    if t in ("true", "True", "TRUE"):
        return True
    if t in ("false", "False", "FALSE"):
        return False
    if t.lower() in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+", t):
        return float(t)
    return t


def _flow_split(inner):
    items, depth, quote, cur = [], 0, None, []
    for ch in inner:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        items.append("".join(cur).strip())
    return items


def _flow(t):
    if t[0] == "[":
        if not t.endswith("]"):
            raise YamlError(f"unterminated flow sequence: {t}")
        return [_scalar(x) for x in _flow_split(t[1:-1])]
    if not t.endswith("}"):
        raise YamlError(f"unterminated flow mapping: {t}")
    out = {}
    for item in _flow_split(t[1:-1]):
        kv = _split_key(item)
        if kv is None:
            out[_scalar(item)] = None
        else:
            out[kv[0]] = _scalar(kv[1])
    return out


class _Reader:
    def __init__(self, text):
        self.raw = text.replace("\t", "  ").splitlines()
        self.lines = []  # (indent, content, raw_index)
        for idx, raw in enumerate(self.raw):
            content = _strip_comment(raw)
            if not content.strip() or content.strip() in ("---", "..."):
                continue
            indent = len(content) - len(content.lstrip(" "))
            self.lines.append([indent, content.strip(), idx])
        self.i = 0

    def peek(self):
        return self.lines[self.i] if self.i < len(self.lines) else None

    def parse(self):
        if not self.lines:
            return None
        value = self.block(self.lines[0][0])
        if self.peek() is not None:
            raise YamlError(f"unexpected content at line {self.peek()[2] + 1}")
        return value

    def block(self, indent):
        first = self.peek()
        if first[1] == "-" or first[1].startswith("- "):
            return self.sequence(indent)
        if _split_key(first[1]) is None:
            self.i += 1
            value = _scalar(first[1])
            if isinstance(value, str) and first[1][:1] not in "'\"":
                value = self.continuation(value, indent - 1)
            return value
        return self.mapping(indent)

    def sequence(self, indent):
        out = []
        while True:
            line = self.peek()
            if line is None or line[0] != indent or not (line[1] == "-" or line[1].startswith("- ")):
                return out
            rest = line[1][1:].lstrip(" ")
            if not rest:
                self.i += 1
                nxt = self.peek()
                out.append(self.block(nxt[0]) if nxt and nxt[0] > indent else None)
                continue
            # Re-home the inline item content at its own column and parse it
            # as a block, so `- key: v` followed by sibling keys is a mapping.
            line[0] = indent + (len(line[1]) - len(rest))
            line[1] = rest
            out.append(self.block(line[0]))

    def mapping(self, indent):
        out = {}
        while True:
            line = self.peek()
            if line is None or line[0] != indent or line[1] == "-" or line[1].startswith("- "):
                return out
            kv = _split_key(line[1])
            if kv is None:
                raise YamlError(f"expected 'key:' at line {line[2] + 1}: {line[1]}")
            key, rest = kv
            self.i += 1
            if rest[:1] in ("|", ">") and re.fullmatch(r"[|>][-+]?\d*", rest):
                out[key] = self.block_scalar(indent, line[2], rest)
            elif rest:
                if rest[:1] in "[{" and not self._balanced(rest):
                    rest = self.join_flow(rest)
                val = _scalar(rest)
                if isinstance(val, str) and rest[:1] not in "'\"":
                    val = self.continuation(val, indent)
                out[key] = val
            else:
                nxt = self.peek()
                if nxt and nxt[0] > indent:
                    out[key] = self.block(nxt[0])
                elif nxt and nxt[0] == indent and (nxt[1] == "-" or nxt[1].startswith("- ")):
                    out[key] = self.sequence(indent)
                else:
                    out[key] = None

    @staticmethod
    def _balanced(text):
        return text.count("[") + text.count("{") == text.count("]") + text.count("}")

    def join_flow(self, text):
        while not self._balanced(text) and self.peek() is not None:
            text += " " + self.peek()[1]
            self.i += 1
        return text

    def continuation(self, value, indent):
        # Multi-line plain scalar: deeper lines that are not themselves keys.
        while True:
            nxt = self.peek()
            if nxt is None or nxt[0] <= indent or _split_key(nxt[1]) is not None \
                    or nxt[1].startswith("- "):
                return value
            value = f"{value} {nxt[1]}"
            self.i += 1

    def block_scalar(self, indent, header_idx, header):
        # Consume raw lines (blank ones included) deeper than the key.
        body, idx = [], header_idx + 1
        while idx < len(self.raw):
            raw = self.raw[idx]
            if raw.strip() and (len(raw) - len(raw.lstrip(" "))) <= indent:
                break
            body.append(raw)
            idx += 1
        while self.i < len(self.lines) and self.lines[self.i][2] < idx:
            self.i += 1
        nonblank = [len(b) - len(b.lstrip(" ")) for b in body if b.strip()]
        cut = min(nonblank) if nonblank else 0
        text = [b[cut:] for b in body]
        while text and not text[-1].strip():
            text.pop()
        if header[0] == "|":
            value = "\n".join(text)
        else:
            # Folding: line breaks between plain lines become spaces; blank
            # lines and more-indented lines keep their breaks.
            parts = []
            for t in text:
                if not t.strip():
                    parts.append("\n")
                elif t[0] == " ":
                    if parts and not parts[-1].endswith("\n"):
                        parts.append("\n")
                    parts.append(t + "\n")
                else:
                    if parts and not parts[-1].endswith("\n"):
                        parts.append(" ")
                    parts.append(t)
            value = "".join(parts).rstrip("\n")
        # Chomping: `-` strips the final break, default (clip) keeps one.
        return value if "-" in header or not value else value + "\n"


def parse_yaml(text):
    return _Reader(text).parse()


# ---------------------------------------------------------------------------
# GitHub path-filter globs (paths / paths-ignore): `**` crosses `/`, `*` and
# `?` do not, `!` negates, and the last matching pattern wins.
# ---------------------------------------------------------------------------

def glob_regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            end = pattern.find("]", i)
            if end == -1:
                out.append(re.escape(ch))
                i += 1
            else:
                out.append(pattern[i:end + 1])
                i = end + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("".join(out) + r"\Z")


def filter_matches(patterns, path):
    """True if `path` is selected by an ordered GitHub pattern list."""
    hit = False
    for pat in patterns or []:
        pat = str(pat)
        neg = pat.startswith("!")
        if glob_regex(pat[1:] if neg else pat).match(path):
            hit = not neg
    return hit


def is_doc_only(path):
    return filter_matches(DOC_ONLY_PATTERNS, path)


# ---------------------------------------------------------------------------
# Workflow model
# ---------------------------------------------------------------------------

def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def normalize_on(on):
    """Return {event: config-dict} for the workflow's triggers."""
    if isinstance(on, str):
        return {on: {}}
    if isinstance(on, list):
        return {str(e): {} for e in on}
    if isinstance(on, dict):
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in on.items()}
    return {}


def _text(value):
    return "" if value is None else str(value)


def detect_toolchains(steps):
    found = set()
    for step in steps:
        uses = _text(step.get("uses")).lower()
        run = _text(step.get("run"))
        if "setup-node" in uses or "pnpm/action-setup" in uses or \
                re.search(r"(^|[\s;&|(])(npm|pnpm|yarn|npx)\s", run):
            found.add("node")
        if "setup-python" in uses or "setup-uv" in uses or "snok/install-poetry" in uses or \
                re.search(r"(^|[\s;&|(])(pip3?|uv|poetry|pytest|tox)\s", run):
            found.add("python")
        if "rust-toolchain" in uses or "rust-cache" in uses or \
                re.search(r"(^|[\s;&|(])cargo\s", run):
            found.add("rust")
        if "setup-go" in uses or re.search(r"(^|[\s;&|(])go (build|test|vet|mod|run)\b", run):
            found.add("go")
        if "docker/build-push-action" in uses or re.search(r"(^|[\s;&|(])docker (buildx )?build\b", run):
            found.add("docker")
    return found


def job_display_name(job_id, job):
    name = job.get("name") if isinstance(job, dict) else None
    return str(name) if name else job_id


def is_required(job_id, job, required):
    """Does this job produce a check-run whose context is a required check?"""
    names = {job_id, job_display_name(job_id, job)}
    for ctx in required:
        for name in names:
            if ctx == name or ctx.startswith(name + " (") or ctx.startswith(name + " / ") \
                    or ctx.endswith(" / " + name):
                return True
            # Names with expressions (`test (${{ matrix.os }})`) — compare the
            # literal prefix before the first expression.
            if "${{" in name:
                prefix = name.split("${{", 1)[0].rstrip(" (")
                if prefix and ctx.startswith(prefix):
                    return True
    return False


class Workflow:
    def __init__(self, path, text):
        self.path = path
        self.error = None
        try:
            doc = parse_yaml(text)
        except (YamlError, ValueError, UnicodeDecodeError) as exc:
            doc, self.error = {}, str(exc)
        if not isinstance(doc, dict):
            doc, self.error = {}, self.error or "workflow is not a mapping"
        self.doc = doc
        self.name = _text(doc.get("name")) or Path(path).name
        self.on = normalize_on(doc.get("on", doc.get(True)))
        jobs = doc.get("jobs")
        self.jobs = {str(k): (v if isinstance(v, dict) else {}) for k, v in (jobs or {}).items()} \
            if isinstance(jobs, dict) else {}

    def steps(self, job):
        return [s for s in _as_list(job.get("steps")) if isinstance(s, dict)]

    def all_steps(self):
        return [s for job in self.jobs.values() for s in self.steps(job)]

    @property
    def pr_events(self):
        return [e for e in ("pull_request", "pull_request_target") if e in self.on]

    def uses_job_level_filter(self):
        for job in self.jobs.values():
            for step in self.steps(job):
                uses = _text(step.get("uses")).lower()
                if "paths-filter" in uses or "changed-files" in uses:
                    return True
            if "needs.changes" in _text(job.get("if")):
                return True
        return False

    def local_uses(self):
        out = set()
        for job in self.jobs.values():
            refs = [_text(job.get("uses"))] + [_text(s.get("uses")) for s in self.steps(job)]
            for ref in refs:
                if ref.startswith("./"):
                    out.add(ref[2:].split("@", 1)[0].rstrip("/"))
        return out


def load_local(root):
    wf_dir = Path(root) / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    return [Workflow(f".github/workflows/{p.name}", p.read_text(encoding="utf-8", errors="replace"))
            for p in sorted(wf_dir.iterdir()) if p.suffix in (".yml", ".yaml") and p.is_file()]


def local_root_files(root):
    return {p.name for p in Path(root).iterdir() if p.is_file()} if Path(root).is_dir() else set()


# ---------------------------------------------------------------------------
# Static analysis
# ---------------------------------------------------------------------------

def finding(fid, severity, category, wf, message, recommendation, job=None, measure=None):
    return {"id": fid, "severity": severity, "category": category, "workflow": wf.path,
            "job": job, "message": message, "recommendation": recommendation,
            "measure": measure, "estMinutesSaved": None}


def analyze(workflows, root_files, required, default_branch="main", partial=False):
    """Static findings. `required` is a list of contexts, or None for unknown.

    `partial` means `required` came from only some sources (another was
    unreadable): a workflow matching none of them may still be required.
    """
    findings = []
    required_known = required is not None and not partial
    for wf in workflows:
        if wf.error:
            findings.append(finding("unparseable-workflow", "low", "meta", wf,
                                    f"could not parse ({wf.error}); not audited",
                                    "The built-in reader could not parse it (YAML anchors/aliases "
                                    "are unsupported). Check that GitHub accepts the file (an "
                                    "invalid workflow never runs), then audit it by hand."))
            continue
        findings += _path_findings(wf, root_files, required, required_known)
        findings += _cache_findings(wf)
        findings += _wasted_run_findings(wf, default_branch)
    # One row per distinct problem: a job with three identical build steps
    # should read as one finding (x3), not three.
    merged = {}
    for f in findings:
        k = (f["id"], f["workflow"], f["job"], f["message"])
        if k in merged:
            merged[k]["occurrences"] += 1
        else:
            f["occurrences"] = 1
            merged[k] = f
    return list(merged.values())


def _required_jobs(wf, required):
    if required is None:
        return []
    return [jid for jid, job in wf.jobs.items() if is_required(jid, job, required)]


def _path_findings(wf, root_files, required, required_known):
    out = []
    req_jobs = _required_jobs(wf, required)
    wf_filtered = [e for e in wf.pr_events
                   if wf.on[e].get("paths") is not None or wf.on[e].get("paths-ignore") is not None]

    # Required-check safety (critical): a workflow-level PR path filter skips
    # the whole workflow, which then never reports its required status.
    if wf_filtered and req_jobs:
        names = ", ".join(f"`{job_display_name(j, wf.jobs[j])}`" for j in req_jobs)
        out.append(finding(
            "required-check-workflow-paths", "critical", "required-check", wf,
            f"workflow-level `{wf_filtered[0]}` path filter on a workflow whose job(s) {names} "
            "are REQUIRED status checks — a PR that touches none of the paths never gets that "
            "status and is blocked forever",
            "Remove the workflow-level `paths:`/`paths-ignore:`. " + JOB_LEVEL_FILTER))
    elif wf_filtered and not required_known:
        out.append(finding(
            "unverified-required-check-paths", "medium", "required-check", wf,
            f"workflow-level `{wf_filtered[0]}` path filter, but required checks could not be "
            "(fully) read — if any job here is a required check, a PR that touches none of the "
            "paths never gets that status and is blocked forever",
            "Verify in the branch protection / ruleset settings that no job in this workflow is a "
            "required check. If one is, remove the workflow-level `paths:`/`paths-ignore:`. "
            + JOB_LEVEL_FILTER))

    # Change relevance: PR-triggered, no filter at any level.
    if wf.pr_events and not wf_filtered and not wf.uses_job_level_filter() and wf.jobs:
        if req_jobs or not required_known:
            why = ("contains required check(s) " + ", ".join(f"`{j}`" for j in req_jobs)
                   if req_jobs else "required checks could not be (fully) read, so assume it may be required")
            rec = f"Workflow {why}: do NOT add workflow-level `paths:`. " + JOB_LEVEL_FILTER
        else:
            rec = ("No job here is a required check, so a workflow-level filter is safe. Add "
                   "`paths-ignore:` for files no job reads, e.g. " +
                   ", ".join(f"`{p}`" for p in DOC_ONLY_PATTERNS[:5]) +
                   " — after confirming no job consumes them (README tests, doc linters, "
                   "link checkers). Never ignore lockfiles, the workflow file, or shared dirs.")
        out.append(finding(
            "unfiltered-pr-workflow", "medium", "path-filter", wf,
            "runs every job on every PR, including documentation-only PRs", rec,
            measure="doc-only-pr-runs"))

    # Under-filtering: an existing PR `paths:` list that omits a real input, so
    # a PR changing only that input skips validation and merges green. Push-only
    # filters are not checked: on a release/deploy workflow they are deliberate
    # scoping, not a merge gate.
    inputs = inferred_inputs(wf, root_files)
    for event in wf.pr_events:
        cfg = wf.on[event]
        missing = []
        if cfg.get("paths") is not None:
            missing = [p for p in inputs if not filter_matches(_as_list(cfg["paths"]), p)]
        elif cfg.get("paths-ignore") is not None:
            missing = [p for p in inputs if filter_matches(_as_list(cfg["paths-ignore"]), p)]
        if missing:
            kind = "`paths:` omits" if cfg.get("paths") is not None else "`paths-ignore:` excludes"
            out.append(finding(
                "under-filtered-paths", "high", "path-filter", wf,
                f"`on.{event}` {kind} real input(s) {', '.join(f'`{m}`' for m in missing)} — a "
                "change to them skips this workflow and can merge green while broken",
                f"Add {', '.join(f'`{m}`' for m in missing)} to `on.{event}.paths` "
                "(or drop them from `paths-ignore`)."))
    return out


def inferred_inputs(wf, root_files):
    """Files this workflow demonstrably depends on (that exist at the root)."""
    inputs = [wf.path]
    toolchains = detect_toolchains(wf.all_steps())
    for tc in sorted(toolchains):
        for name in TOOLCHAIN_INPUTS[tc]:
            if name in root_files and name not in inputs:
                inputs.append(name)
    for ref in sorted(wf.local_uses()):
        target = ref if ref.endswith((".yml", ".yaml")) else ref + "/action.yml"
        if target not in inputs:
            inputs.append(target)
    return inputs


def _key_text(value):
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return _text(value)


def _key_has_version(key):
    """True if the cache key embeds a resolved tool version (step output or literal)."""
    if re.search(r"steps\.[\w-]+\.outputs\.[\w-]*version[\w-]*", key, re.I):
        return True
    if re.search(r"(?<![\w.])v?\d+\.\d+(\.\d+)?(?![\w.])", re.sub(r"\$\{\{.*?\}\}", "", key)):
        return True
    return False


def _cache_findings(wf):
    out = []
    for jid, job in wf.jobs.items():
        steps = wf.steps(job)
        has_matrix = isinstance(job.get("strategy"), dict) and job["strategy"].get("matrix") is not None
        job_caches = False
        for step in steps:
            uses = _text(step.get("uses")).lower()
            with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
            if uses.startswith("actions/cache") and "/save" not in uses:
                job_caches = True
                key = _key_text(with_.get("key"))
                restore = with_.get("restore-keys")
                if re.search(r"github\.(sha|run_id|run_number|run_attempt)", key):
                    out.append(finding(
                        "cache-key-never-hits", "low" if restore else "high", "cache", wf,
                        f"cache key `{key}` includes a per-run value, so the exact key never hits"
                        + (" (restore-keys still restore a prefix, but every run uploads a new "
                           "cache and evicts useful ones)" if restore else ""),
                        "Key on the inputs instead: `${{ runner.os }}-<tool>-${{ hashFiles('<lockfile>') }}`"
                        " with a `restore-keys:` prefix fallback.", job=jid))
                elif "hashFiles(" not in key and not _key_has_version(key):
                    out.append(finding(
                        "cache-key-no-lockfile-hash", "high", "cache", wf,
                        f"cache key `{key}` has no `hashFiles(...)` of a lockfile, so it never "
                        "invalidates — stale dependencies are restored forever",
                        "Append `${{ hashFiles('<lockfile>') }}` (package-lock.json, pnpm-lock.yaml, "
                        "uv.lock, Cargo.lock, go.sum, …) to the key.", job=jid))
                if not restore and "hashFiles(" in key:
                    out.append(finding(
                        "cache-no-restore-keys", "low", "cache", wf,
                        "cache has no `restore-keys:` fallback, so every lockfile change is a full "
                        "cold miss", "Add `restore-keys: ${{ runner.os }}-<tool>-`.", job=jid))
                if has_matrix and "matrix." not in key:
                    varies = any("matrix." in json.dumps(
                        {k: v for k, v in o.items() if k in ("run", "with", "env", "if")},
                        default=str) for o in steps if o is not step)
                    out.append(finding(
                        "cache-matrix-key-collision", "medium" if varies else "info", "cache", wf,
                        "matrix job caches under a key with no `matrix.*` component — matrix legs "
                        "race to save and restore each other's (wrong) caches" if varies else
                        "matrix job caches under a key with no `matrix.*` component, but no other "
                        "step references `matrix.*`, so the cached content is likely identical "
                        "across legs (shared cache is probably intentional)",
                        "Include the distinguishing matrix values (e.g. `${{ matrix.os }}`, "
                        "`${{ matrix.node }}`) in the key." if varies else
                        "No action unless the legs install different content; then add "
                        "`matrix.*` to the key.", job=jid))
            if "swatinem/rust-cache" in uses:
                job_caches = True
        out += _toolchain_cache_findings(wf, jid, steps, job_caches)
    return out


def _toolchain_cache_findings(wf, jid, steps, job_caches):
    out = []
    runs = "\n".join(_text(s.get("run")) for s in steps)
    uses_all = [(_text(s.get("uses")).lower(), s.get("with") if isinstance(s.get("with"), dict) else {})
                for s in steps]

    def step_with(fragment):
        return [w for u, w in uses_all if fragment in u]

    def miss(tool, message, rec):
        out.append(finding(f"no-cache-{tool}", "medium", "cache", wf, message, rec,
                           job=jid, measure="job-minutes"))

    node = step_with("setup-node")
    if re.search(r"\b(npm (ci|install|i)\b|pnpm (install|i)\b|yarn( install)?\s*($|\n|&&))", runs) \
            and not job_caches and not any(w.get("cache") for w in node):
        miss("node", "installs node dependencies with no dependency cache",
             "Set `cache: npm|pnpm|yarn` on actions/setup-node (keys on the lockfile hash "
             "automatically).")
    py = step_with("setup-python")
    if re.search(r"\b(pip3? install|poetry install|pipenv install)\b", runs) and not job_caches \
            and not any(w.get("cache") for w in py):
        miss("python", "installs python dependencies with no dependency cache",
             "Set `cache: pip|poetry|pipenv` on actions/setup-python (with "
             "`cache-dependency-path:` pointing at the lockfile), or use astral-sh/setup-uv.")
    for w in step_with("setup-uv"):
        if _text(w.get("enable-cache")).lower() == "false":
            miss("python", "astral-sh/setup-uv has `enable-cache: false`",
                 "Drop `enable-cache: false` (or set it true) so the uv cache keys on uv.lock.")
    if re.search(r"(^|[\s;&|(])cargo (build|test|clippy|check|nextest)\b", runs) and not job_caches:
        miss("rust", "builds with cargo with no `~/.cargo` / `target/` cache",
             "Add `Swatinem/rust-cache` after the toolchain step (keys on Cargo.lock and the "
             "toolchain).")
    for w in step_with("setup-go"):
        if _text(w.get("cache")).lower() == "false":
            miss("go", "actions/setup-go has `cache: false`",
                 "Remove `cache: false`; setup-go caches the module/build cache keyed on go.sum "
                 "by default.")
    for w in step_with("docker/build-push-action"):
        if not w.get("cache-from"):
            miss("docker", "docker/build-push-action with no `cache-from` — every layer rebuilds",
                 "Add `cache-from: type=gha` and `cache-to: type=gha,mode=max`.")
    if re.search(r"(^|[\s;&|(])docker build\b", runs) and "--cache-from" not in runs:
        miss("docker", "plain `docker build` with no layer cache — every layer rebuilds each run",
             "Switch to docker/setup-buildx-action + docker/build-push-action with "
             "`cache-from: type=gha` / `cache-to: type=gha,mode=max`.")
    return out


def _cancel_value(conc):
    if isinstance(conc, dict):
        return conc.get("cancel-in-progress")
    return None


def _group_of(conc):
    if isinstance(conc, dict):
        group = conc.get("group")
        return None if group is None else str(group)
    if isinstance(conc, str):
        return conc
    return None


def _pushes_default_branch(wf, default_branch):
    """Can a push to the default branch trigger this workflow?

    Honors `branches` / `branches-ignore` patterns (negations included) and
    tag-only filters. `paths` filters are ignored: a path-filtered workflow
    still runs on some default-branch pushes.
    """
    if "push" not in wf.on:
        return False
    push = wf.on["push"]
    branches, ignore = push.get("branches"), push.get("branches-ignore")
    if branches is not None:
        return filter_matches(_as_list(branches), default_branch)
    if ignore is not None:
        return not filter_matches(_as_list(ignore), default_branch)
    return push.get("tags") is None


def _expr_inner(value):
    m = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", value, re.S)
    return (m.group(1) if m else value).strip()


def eval_cancel_for_default_push(value, default_branch):
    """Evaluate `cancel-in-progress` for a push to the default branch.

    True / False when statically decidable, None when it is not (an arbitrary
    expression is never treated as safely PR-scoped).
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    expr = _expr_inner(str(value))
    if expr.lower() in ("true", "false"):
        return expr.lower() == "true"
    m = re.fullmatch(r"github\.event_name\s*(==|!=)\s*'([\w-]+)'", expr)
    if m:
        return (m.group(2) == "push") == (m.group(1) == "==")
    m = re.fullmatch(r"github\.ref\s*(==|!=)\s*'([^']*)'", expr)
    if m:
        return (m.group(2) == f"refs/heads/{default_branch}") == (m.group(1) == "==")
    m = re.fullmatch(r"github\.ref_name\s*(==|!=)\s*'([^']*)'", expr)
    if m:
        return (m.group(2) == default_branch) == (m.group(1) == "==")
    return None


def classify_group(group):
    """Return (kind, distinguishes_workflows) for a concurrency group string.

    kind: literal | ref | unique | uncertain | missing. `unique` means a
    per-run value (sha/run id) so the group never queues anything.
    """
    if not group or not group.strip():
        return "missing", False
    exprs = re.findall(r"\$\{\{(.*?)\}\}", group, re.S)
    literal = re.sub(r"\$\{\{.*?\}\}", "", group, flags=re.S)
    has_literal = bool(re.sub(r"[\s\-_/:.]", "", literal))
    if not exprs:
        return "literal", True
    text = " ".join(exprs)
    distinct = has_literal or bool(re.search(r"github\.(workflow|job)\b", text))
    if re.search(r"github\.(sha|run_id|run_number|run_attempt)\b", text):
        return "unique", distinct
    if re.search(r"github\.(ref|ref_name|head_ref)\b", text):
        return "ref", distinct
    return "uncertain", distinct


def _concurrency_blocks(wf):
    blocks = []
    if wf.doc.get("concurrency") is not None:
        blocks.append((None, wf.doc["concurrency"]))
    for jid, job in wf.jobs.items():
        if job.get("concurrency") is not None:
            blocks.append((jid, job["concurrency"]))
    return blocks


def _default_branch_findings(wf, default_branch, has_pr):
    """Checks for workflows a push to the default branch can trigger.

    Separate from the PR checks: a push-only workflow has no PRs to supersede,
    but a queue of merges (or a cancelled started run) is still a cost/verdict
    problem on the default branch.
    """
    out = []
    covered = wf.doc.get("concurrency") is not None or \
        all(job.get("concurrency") is not None for job in wf.jobs.values())
    if not covered and not has_pr:
        out.append(finding(
            "missing-default-branch-concurrency", "medium", "wasted-runs", wf,
            f"runs on pushes to `{default_branch}` with no `concurrency:` group — every merge "
            "queues a full run, even when newer merges have already landed",
            "Add at workflow level (one running, the newest waiting, nothing started is "
            "cancelled):\n"
            "concurrency:\n"
            "  group: ${{ github.workflow }}-${{ github.ref }}\n"
            "  cancel-in-progress: false\n"
            "A deploy should also resolve the newest verified commit when it starts and refuse "
            "a rollback (see the deploy guidance in optimize-ci.md).",
            measure="default-branch-overlap"))
    for jid, conc in _concurrency_blocks(wf):
        scope = f"job `{jid}` " if jid else ""
        kind, distinct = classify_group(_group_of(conc))
        if kind == "unique":
            out.append(finding(
                "unsafe-default-branch-group", "medium", "wasted-runs", wf,
                f"{scope}concurrency group embeds a per-run value (sha / run id), so it is unique "
                f"per run and never queues or supersedes `{default_branch}` runs",
                "Scope the group to the ref and the workflow: "
                "`${{ github.workflow }}-${{ github.ref }}`.", job=jid))
        elif kind == "ref" and not distinct:
            out.append(finding(
                "unsafe-default-branch-group", "medium", "wasted-runs", wf,
                f"{scope}concurrency group is only the ref, so every workflow on that ref shares "
                "one group and queues (or cancels) the others",
                "Prefix the workflow: `${{ github.workflow }}-${{ github.ref }}`.", job=jid))
        elif kind == "uncertain":
            out.append(finding(
                "uncertain-default-branch-group", "low", "wasted-runs", wf,
                f"{scope}concurrency group is an expression that cannot be evaluated statically; "
                f"whether `{default_branch}` runs are isolated per ref and per workflow is unverified",
                "Check by hand that the group resolves to a ref-scoped, workflow-distinct string.",
                job=jid))
        verdict = eval_cancel_for_default_push(_cancel_value(conc), default_branch)
        if verdict is True:
            out.append(finding(
                "cancel-on-default-branch", "medium", "wasted-runs", wf,
                f"{scope}`cancel-in-progress: true` also cancels `{default_branch}` pushes — a burst "
                f"of merges can leave intermediate `{default_branch}` commits with no completed run "
                "(a `cancelled` run is no verdict)",
                "Scope cancellation to PRs: "
                "`cancel-in-progress: ${{ github.event_name == 'pull_request' }}` "
                "(or `false` on a push-only workflow).", job=jid, measure="default-branch-cancelled"))
        elif verdict is None:
            out.append(finding(
                "uncertain-default-branch-cancel", "low", "wasted-runs", wf,
                f"{scope}`cancel-in-progress` is an expression that cannot be evaluated statically; "
                f"it may cancel started `{default_branch}` runs",
                "Check by hand, or use "
                "`${{ github.event_name == 'pull_request' }}`, which is false on pushes.", job=jid))
    return out


def _wasted_run_findings(wf, default_branch):
    out = []
    if not wf.jobs:
        return out
    if wf.pr_events:
        wf_conc = wf.doc.get("concurrency")
        job_concs = [job.get("concurrency") for job in wf.jobs.values()]
        if wf_conc is None and not all(c is not None for c in job_concs):
            out.append(finding(
                "missing-concurrency", "medium", "wasted-runs", wf,
                "no `concurrency:` group — every push to a PR leaves the superseded run burning minutes",
                "Add at workflow level (cancels superseded PR runs; never cancels default-branch "
                "pushes):\n" + RECOMMENDED_CONCURRENCY, measure="superseded-pr-runs"))
        elif wf_conc is not None:
            cancel = _cancel_value(wf_conc)
            if cancel is None or cancel is False:
                out.append(finding(
                    "concurrency-no-cancel", "medium", "wasted-runs", wf,
                    "`concurrency:` group without `cancel-in-progress` — superseded PR runs queue "
                    "instead of being cancelled",
                    "Set `cancel-in-progress: ${{ github.event_name == 'pull_request' }}`.",
                    measure="superseded-pr-runs"))
    if _pushes_default_branch(wf, default_branch):
        out += _default_branch_findings(wf, default_branch, bool(wf.pr_events))
    push = wf.on.get("push")
    if wf.pr_events and push is not None and \
            all(push.get(k) is None for k in ("branches", "branches-ignore", "tags")):
        out.append(finding(
            "duplicate-push-pr", "medium", "wasted-runs", wf,
            "triggers on `push` to every branch AND `pull_request` — each commit on a "
            "same-repo PR branch runs twice",
            f"Restrict `on.push.branches` to `[{default_branch}]` (PRs are covered by "
            "`pull_request`).", measure="duplicate-push-runs"))
    return out


# ---------------------------------------------------------------------------
# GitHub reads (all GET). Every failure degrades; none aborts the report.
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, endpoint, message):
        super().__init__(f"{endpoint}: {message}")
        m = re.search(r"HTTP (\d{3})", message)
        self.status = int(m.group(1)) if m else None


class GitHub:
    def api(self, endpoint):
        result = subprocess.run(["gh", "api", "--method", "GET", endpoint],
                                text=True, capture_output=True, check=False)
        if result.returncode:
            raise ApiError(endpoint, (result.stderr or result.stdout).strip())
        return json.loads(result.stdout) if result.stdout.strip() else None


def origin_repo():
    result = subprocess.run(["git", "remote", "get-url", "origin"],
                            text=True, capture_output=True, check=False)
    url = result.stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _check_contexts(data):
    found = list(data.get("contexts") or []) + [c.get("context") for c in data.get("checks") or []]
    return [c for c in found if c]


def _classic_required(gh, repo, branch):
    """Classic branch-protection required contexts as (source_state, contexts).

    `repos/O/R/branches/<b>` exposes `protected` and
    `protection.required_status_checks` to anyone with read access, so it is the
    primary source. The dedicated protection endpoint answers 404 to every
    non-admin token even when checks ARE required, so its 404 means "none" only
    when the branch itself reports `protected: false`; otherwise it is unknown.
    """
    protected = None
    try:
        info = gh.api(f"repos/{repo}/branches/{branch}") or {}
        protected = info.get("protected")
        if protected is False:
            return "none", []
        rsc = (info.get("protection") or {}).get("required_status_checks")
        if isinstance(rsc, dict) and ("contexts" in rsc or "checks" in rsc):
            found = _check_contexts(rsc)
            return ("read" if found else "none"), found
    except ApiError:
        pass
    try:
        found = _check_contexts(gh.api(f"repos/{repo}/branches/{branch}/protection/required_status_checks") or {})
        return "read", found
    except ApiError as exc:
        why = ("branch is protected but its checks are unreadable" if protected
               else "branch protection status unreadable")
        return f"unknown ({exc.status or 'error'}: {why})", []


def required_checks(gh, repo, branch):
    """Union of classic-protection and ruleset required contexts.

    Returns {"state": known|unknown, "partial": bool, "contexts": [...], "sources": {...}}.
    An unreadable source is never reported as "no required checks": with no
    contexts from any source the state is unknown; with contexts from one source
    but another unreadable, `partial` is set and callers must treat any workflow
    matching none of the known contexts as possibly required.
    """
    sources = {}
    sources["classic"], contexts = _classic_required(gh, repo, branch)
    unknown = sources["classic"].startswith("unknown")
    try:
        for rule in gh.api(f"repos/{repo}/rules/branches/{branch}") or []:
            if rule.get("type") == "required_status_checks":
                for chk in (rule.get("parameters") or {}).get("required_status_checks") or []:
                    if chk.get("context"):
                        contexts.append(chk["context"])
        sources["rulesets"] = "read"
    except ApiError as exc:
        if exc.status == 404:
            sources["rulesets"] = "none"
        else:
            sources["rulesets"] = f"unknown ({exc.status or 'error'})"
            unknown = True
    uniq = sorted(set(contexts))
    return {"state": "unknown" if unknown and not uniq else "known",
            "partial": unknown and bool(uniq), "contexts": uniq, "sources": sources}


def load_remote(gh, repo, ref):
    listing = gh.api(f"repos/{repo}/contents/.github/workflows?ref={ref}") or []
    workflows = []
    for entry in listing:
        name = entry.get("name", "")
        if entry.get("type") != "file" or not name.endswith((".yml", ".yaml")):
            continue
        blob = gh.api(f"repos/{repo}/contents/.github/workflows/{name}?ref={ref}") or {}
        text = base64.b64decode(blob.get("content", "")).decode("utf-8", errors="replace")
        workflows.append(Workflow(f".github/workflows/{name}", text))
    root = gh.api(f"repos/{repo}/contents?ref={ref}") or []
    return workflows, {e.get("name") for e in root if e.get("type") == "file"}


def _ts(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if value else None


def _minutes(start, end):
    s, e = _ts(start), _ts(end)
    return max((e - s).total_seconds() / 60.0, 0.0) if s and e else 0.0


def _run_path(run):
    return (run.get("path") or "").split("@", 1)[0]


def _row(run):
    """Normalize a run for timeline math; None when timestamps are missing."""
    created = _ts(run.get("created_at"))
    start = _ts(run.get("run_started_at")) or created
    end = _ts(run.get("updated_at"))
    if not created or not start or not end or end < start:
        return None
    return {"id": run.get("id"), "created": created, "start": start, "end": end,
            "conclusion": run.get("conclusion"),
            "wall": (end - start).total_seconds() / 60.0}


def observed_overlap(rows):
    """Runs whose start precedes the end of an earlier run (strict: touching
    timestamps are not overlap). A diagnostic only: it is NOT the number of
    runs a one-running/one-pending queue would avoid."""
    count, latest_end = 0, None
    for r in sorted(rows, key=lambda r: (r["start"], str(r["id"]))):
        if latest_end is not None and r["start"] < latest_end:
            count += 1
        latest_end = r["end"] if latest_end is None else max(latest_end, r["end"])
    return count


def replay_queue(rows):
    """Replay arrivals through GitHub's concurrency queue (no cancel-in-progress).

    The documented contract: per group at most one run is running and one is
    pending; a newer arrival replaces the pending run, which is cancelled. A
    lone arrival during an active run waits and still executes. Arrivals are
    `created_at`, ordered by (time, id) so simultaneous arrivals are
    deterministic; durations are each run's measured wall time. A run arriving
    exactly when the running one ends finds the group free.
    Returns (executed, superseded run ids).
    """
    arrivals = sorted(rows, key=lambda r: (r["created"], str(r["id"])))
    running_end, pending, executed, superseded = None, None, 0, []

    def settle(until):
        nonlocal running_end, pending, executed
        while running_end is not None and (until is None or running_end <= until):
            if pending is None:
                running_end = None
                return
            executed += 1
            running_end = running_end + timedelta(minutes=pending["wall"])
            pending = None

    for r in arrivals:
        settle(r["created"])
        if running_end is None:
            executed += 1
            running_end = r["created"] + timedelta(minutes=r["wall"])
        else:
            if pending is not None:
                superseded.append(pending["id"])
            pending = r
    # Drain: the still-pending run executes after the running one.
    if pending is not None:
        executed += 1
    return executed, superseded


def bucket_estimate(rows, per_run, hours):
    """Counterfactual: run once per occupied fixed-UTC bucket on its newest commit."""
    size = 3600 * hours
    occupied = len({int(r["created"].timestamp()) // size for r in rows})
    baseline = len(rows) * per_run
    est = occupied * per_run
    return {"bucketHours": hours, "occupiedBuckets": occupied, "baselineRuns": len(rows),
            "perRunMinutes": round(per_run, 2), "baselineMinutes": round(baseline, 1),
            "estimatedMinutes": round(est, 1), "savedMinutes": round(baseline - est, 1),
            "savedPct": round(100.0 * (baseline - est) / baseline, 1) if baseline else 0.0,
            "kind": "counterfactual estimate, not observed savings"}


def main_stats(gh, repo, wf_runs, branch, main_sample=MAIN_JOBS_SAMPLE):
    """Default-branch evidence for one workflow.

    Population: `event == push`, `head_branch == branch`, original attempt only
    (`run_attempt` 1 or absent); reruns are counted and excluded. Runs lacking a
    created/updated timestamp are excluded and counted. Cancelled and skipped
    runs stay in the arrival timeline (they arrived) but not in the cost
    population: a cancelled run's duration is truncated.
    """
    push = [r for r in wf_runs if r.get("event") == "push" and r.get("head_branch") == branch]
    originals = [r for r in push if (r.get("run_attempt") or 1) == 1]
    pairs = [(r, _row(r)) for r in originals]
    timed = [(r, row) for r, row in pairs if row]
    rows = [row for _, row in timed]
    eligible = [(r, row) for r, row in timed if row["conclusion"] not in ("cancelled", "skipped", None)]
    stats = {"pushRuns": len(push), "originalRuns": len(originals),
             "rerunAttempts": len(push) - len(originals),
             "missingTimestamps": len(originals) - len(timed),
             "cancelledRuns": sum(1 for row in rows if row["conclusion"] == "cancelled"),
             "eligibleRuns": len(eligible)}
    sampled, job_total = 0, 0.0
    for r, _ in sorted(eligible, key=lambda t: t[1]["created"], reverse=True)[:main_sample]:
        try:
            jobs = (gh.api(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100") or {}).get("jobs") or []
        except ApiError:
            continue
        if not jobs:
            continue
        sampled += 1
        job_total += sum(_minutes(j.get("started_at"), j.get("completed_at")) for j in jobs)
    if sampled:
        per_run, basis = job_total / sampled, "job-minutes"
    elif eligible:
        per_run, basis = sum(row["wall"] for _, row in eligible) / len(eligible), "wall-time"
    else:
        per_run, basis = None, None
    stats.update({"costBasis": basis, "sampledJobRuns": sampled,
                  "perRunMinutes": None if per_run is None else round(per_run, 2),
                  "observedOverlap": observed_overlap(rows)})
    executed, superseded = replay_queue(rows) if rows else (0, [])
    stats["replay"] = {"arrivals": len(rows), "executed": executed, "superseded": len(superseded),
                       "estMinutesSaved": None if per_run is None else round(len(superseded) * per_run, 1)}
    elig_rows = [row for _, row in eligible]
    if per_run is not None and elig_rows:
        stats["batching"] = {"hourly": bucket_estimate(elig_rows, per_run, 1),
                             "threeHourly": bucket_estimate(elig_rows, per_run, 3)}
    else:
        stats["batching"] = None
    return stats


def _percentile(values, pct):
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    idx = max(int(-(-pct * len(ordered) // 100)) - 1, 0)
    return ordered[min(idx, len(ordered) - 1)]


def _list_runs(gh, repo, endpoint, cap, since):
    """Completed runs from one runs-listing endpoint, newest first, up to `cap`.

    Returns (runs, total_count): total_count is the API's count of completed
    runs in the window, so it can exceed len(runs) when the cap bites.
    """
    runs, page, total = [], 1, None
    per_page = min(100, cap)
    while len(runs) < cap:
        data = gh.api(f"{endpoint}?per_page={per_page}&page={page}&status=completed"
                      f"&created=%3E%3D{since}") or {}
        batch = data.get("workflow_runs") or []
        if total is None and isinstance(data.get("total_count"), int):
            total = data["total_count"]
        runs += batch
        if len(batch) < per_page:
            break
        page += 1
    runs = runs[:cap]
    return runs, (total if total is not None and total >= len(runs) else len(runs))


def _span_days(rs, default):
    created = sorted(t for t in (_ts(r.get("created_at")) for r in rs) if t)
    if len(created) > 1:
        return max((created[-1] - created[0]).total_seconds() / 86400.0, 1.0)
    return float(default)


def gather_history(gh, repo, max_runs=100, days=30, jobs_sample=20, pr_sample=30,
                   default_branch=None, main_sample=MAIN_JOBS_SAMPLE, workflows=None):
    """Recent runs, per-workflow runner-minutes, default-branch stats, per-PR changed files.

    With `workflows`, up to `max_runs` completed runs are sampled PER WORKFLOW
    (so a cheap, bursty workflow cannot crowd out an expensive one) and each
    workflow's true run count for the window (`total_count`) is recorded so
    estimates can be scaled. Without it (or if no per-workflow listing is
    readable) it falls back to one repo-wide listing capped at `max_runs`.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    runs, totals, caps = [], {}, {}
    for wf in workflows or []:
        path = getattr(wf, "path", "")
        if getattr(wf, "error", None) or not path:
            continue
        try:
            wf_runs, total = _list_runs(
                gh, repo, f"repos/{repo}/actions/workflows/{Path(path).name}/runs", max_runs, since)
        except ApiError:
            continue
        wf_runs = [r for r in wf_runs if r.get("status") == "completed"]
        for r in wf_runs:
            r.setdefault("path", path)
        runs += wf_runs
        totals[path] = max(total, len(wf_runs))
        caps[path] = totals[path] > len(wf_runs)
    per_wf_mode = bool(totals)
    if not per_wf_mode:
        runs, _ = _list_runs(gh, repo, f"repos/{repo}/actions/runs", max_runs, since)
        capped = len(runs) >= max_runs
        runs = [r for r in runs if r.get("status") == "completed"]
    else:
        capped = any(caps.values())
    # A capped sample covers only the newest part of the window: measure the
    # span it really covers instead of presenting it as the full window.
    window_days = float(days)
    if capped and not per_wf_mode:
        window_days = _span_days(runs, days)
    by_path = {}
    for run in runs:
        by_path.setdefault(_run_path(run), []).append(run)
    per_workflow = {}
    for path, wf_runs in by_path.items():
        sampled, job_minutes = 0, {}
        total_sample = 0.0
        total_runs = totals.get(path, len(wf_runs)) if per_wf_mode else len(wf_runs)
        wf_capped = caps.get(path, False) if per_wf_mode else False
        for run in wf_runs[:jobs_sample]:
            try:
                jobs = (gh.api(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100") or {}).get("jobs") or []
            except ApiError:
                continue
            sampled += 1
            for job in jobs:
                mins = _minutes(job.get("started_at"), job.get("completed_at"))
                job_minutes.setdefault(job.get("name", "?"), []).append(mins)
                total_sample += mins
        if sampled:
            per_run = total_sample / sampled
        else:
            walls = [_minutes(r.get("run_started_at"), r.get("updated_at")) for r in wf_runs]
            per_run = sum(walls) / len(walls) if walls else 0.0
        per_workflow[path] = {
            "runs": len(wf_runs),
            "totalRuns": total_runs,
            "sampleCapped": wf_capped,
            "scale": round(total_runs / len(wf_runs), 4) if wf_runs else 1.0,
            "windowDays": round(_span_days(wf_runs, days), 2) if wf_capped else float(days),
            "avgRunnerMinutesPerRun": round(per_run, 2),
            "totalRunnerMinutes": round(per_run * total_runs, 1),
            "jobs": {k: round(sum(v) / len(v), 2) for k, v in sorted(job_minutes.items())},
            "jobSpread": {k: {"n": len(v), "median": round(_percentile(v, 50), 2),
                              "p90": round(_percentile(v, 90), 2)}
                          for k, v in sorted(job_minutes.items())},
            "sampledRuns": sampled,
        }
    # A run's `pull_requests` array is emptied once its PR merges and the
    # branch is deleted, so fall back to one listing of recent PRs and map the
    # run's head branch to a PR number.
    by_branch = {}
    if any(r.get("event") in ("pull_request", "pull_request_target") and not r.get("pull_requests")
           for r in runs):
        try:
            for pr in gh.api(f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100") or []:
                by_branch.setdefault((pr.get("head") or {}).get("ref"), pr.get("number"))
        except ApiError:
            pass
    pr_files, pr_numbers = {}, []
    for run in runs:
        if run.get("event") in ("pull_request", "pull_request_target"):
            nums = [pr.get("number") for pr in run.get("pull_requests") or []]
            if not nums and by_branch.get(run.get("head_branch")):
                nums = [by_branch[run["head_branch"]]]
            run["_prs"] = nums
            for number in nums:
                if number not in pr_numbers:
                    pr_numbers.append(number)
    for number in pr_numbers[:pr_sample]:
        try:
            files = gh.api(f"repos/{repo}/pulls/{number}/files?per_page=100") or []
            pr_files[number] = [f.get("filename", "") for f in files]
        except ApiError:
            continue
    main = {}
    if default_branch:
        for path, wf_runs in by_path.items():
            stats = main_stats(gh, repo, wf_runs, default_branch, main_sample)
            if stats["pushRuns"]:
                main[path] = stats
    return {"since": since, "runs": runs, "perWorkflow": per_workflow, "prFiles": pr_files,
            "prSampled": len(pr_files), "prSeen": len(pr_numbers), "defaultBranch": default_branch,
            "main": main, "sampleCapped": capped, "windowDays": round(window_days, 2),
            "requestedDays": days, "maxRuns": max_runs,
            "perWorkflowSampling": per_wf_mode}


def estimate(findings, history):
    """Fill estMinutesSaved (over the sampled window) where a finding is measurable."""
    if history is None:
        return
    for f in findings:
        stats = history["perWorkflow"].get(f["workflow"])
        if not stats:
            if f["measure"]:
                f["estNote"] = "no runs of this workflow in the window"
                if f["measure"] not in ("job-minutes", "default-branch-cancelled"):
                    f["estMinutesSaved"] = 0.0
            continue
        main = (history.get("main") or {}).get(f["workflow"])
        per_run = stats["avgRunnerMinutesPerRun"]
        runs = [r for r in history["runs"] if r.get("path", "").split("@", 1)[0] == f["workflow"]]
        if f["measure"] == "doc-only-pr-runs":
            pr_runs = [r for r in runs if r.get("event") in ("pull_request", "pull_request_target")]
            doc_only = 0
            known = 0
            for r in pr_runs:
                nums = r.get("_prs") or [p.get("number") for p in r.get("pull_requests") or []]
                files = next((history["prFiles"][n] for n in nums if n in history["prFiles"]), None)
                if files is None:
                    continue
                known += 1
                if files and all(is_doc_only(x) for x in files):
                    doc_only += 1
            f["estMinutesSaved"] = round(doc_only * per_run, 1)
            f["estNote"] = f"{doc_only}/{known} classified PR runs touched only documentation files"
        elif f["measure"] == "superseded-pr-runs":
            pr_runs = sorted([r for r in runs if r.get("event") in ("pull_request", "pull_request_target")],
                             key=lambda r: r.get("created_at", ""))
            saved, count = 0.0, 0
            for i, r in enumerate(pr_runs):
                later = [x for x in pr_runs[i + 1:] if x.get("head_branch") == r.get("head_branch")]
                if not later or r.get("conclusion") == "cancelled":
                    continue
                nxt_created, end = _ts(later[0].get("created_at")), _ts(r.get("updated_at"))
                start = _ts(r.get("run_started_at") or r.get("created_at"))
                if nxt_created and end and start and nxt_created < end:
                    wall = max((end - start).total_seconds(), 1.0)
                    frac = min((end - nxt_created).total_seconds() / wall, 1.0)
                    saved += frac * per_run
                    count += 1
            f["estMinutesSaved"] = round(saved, 1)
            f["estNote"] = f"{count} PR run(s) kept running after a newer push to the same branch"
            if f["id"] == "missing-concurrency" and main and main["replay"]["estMinutesSaved"] is not None:
                # Disjoint population (default-branch push runs): safe to add.
                f["estDefaultBranchMinutes"] = main["replay"]["estMinutesSaved"]
                f["estMinutesSaved"] = round(saved + main["replay"]["estMinutesSaved"], 1)
                f["estNote"] += "; " + _main_note(main, history)
        elif f["measure"] == "duplicate-push-runs":
            pr_shas = {r.get("head_sha") for r in runs if r.get("event") == "pull_request"}
            dup = [r for r in runs if r.get("event") == "push" and r.get("head_sha") in pr_shas]
            f["estMinutesSaved"] = round(len(dup) * per_run, 1)
            f["estNote"] = f"{len(dup)} push run(s) duplicated a pull_request run of the same commit"
        elif f["measure"] == "default-branch-overlap":
            if not main:
                f["estMinutesSaved"] = None
                f["estNote"] = "no default-branch push runs of this workflow in the sample: not measured"
            else:
                f["estMinutesSaved"] = main["replay"]["estMinutesSaved"]
                f["estDefaultBranchMinutes"] = main["replay"]["estMinutesSaved"]
                f["estNote"] = _main_note(main, history)
        elif f["measure"] == "default-branch-cancelled":
            if main:
                f["estNote"] = (f"{main['cancelledRuns']} of {main['originalRuns']} default-branch push "
                                "run(s) in the sample were cancelled (no verdict)")
        elif f["measure"] == "job-minutes":
            f["estNote"] = (f"not measured (cache hit/miss needs job logs); workflow averages "
                            f"{per_run} runner-min/run over {stats['runs']} runs")
        scale = stats.get("scale", 1.0)
        if scale > 1.0 and f["measure"] in ("doc-only-pr-runs", "superseded-pr-runs",
                                            "duplicate-push-runs", "default-branch-overlap"):
            for key in ("estMinutesSaved", "estDefaultBranchMinutes"):
                if f.get(key) is not None:
                    f[key] = round(f[key] * scale, 1)
            f["estNote"] = (f.get("estNote") or "") + (
                f"; scaled x{scale:g} from a sample of {stats['runs']} to the window's "
                f"{stats['totalRuns']} runs")


def _sample_note(history):
    if history.get("perWorkflowSampling"):
        capped = [Path(p).name for p, w in (history.get("perWorkflow") or {}).items()
                  if w.get("sampleCapped")]
        if capped:
            return (f"per-workflow sample capped at {history.get('maxRuns')} runs for "
                    f"{', '.join(capped)}: rates use each sample's own span")
        return f"full {history.get('requestedDays')}-day window (every run sampled)"
    if history.get("sampleCapped"):
        return (f"sample capped at {history.get('maxRuns')} runs: covers ~{history.get('windowDays')} "
                f"of the {history.get('requestedDays')} requested days, so totals are over that span only")
    return f"full {history.get('windowDays')}-day window"


def _main_note(main, history):
    """Observed overlap and the replay estimate, labeled separately."""
    rp = main["replay"]
    cost = ("not measured" if main["perRunMinutes"] is None else
            f"{main['perRunMinutes']} {main['costBasis']}/run"
            + (" (wall-time fallback, not job-minutes)" if main["costBasis"] == "wall-time" else ""))
    est = "not measured" if rp["estMinutesSaved"] is None else f"~{rp['estMinutesSaved']} min"
    return (f"observed: {main['observedOverlap']} of {rp['arrivals']} default-branch push run(s) started "
            f"while an earlier run was still running (diagnostic, not runs avoided); replay of "
            f"one-running/one-pending queue: {rp['superseded']} superseded, {est} avoidable "
            f"(a lone arrival during a run still executes); cost {cost}; "
            f"{main['cancelledRuns']} already cancelled, {main['rerunAttempts']} rerun attempt(s) and "
            f"{main['missingTimestamps']} run(s) with missing timestamps excluded; {_sample_note(history)}")


def heavy_findings(workflows, history, default_branch, threshold=DEFAULT_HEAVY_MAIN_MINUTES_PER_DAY):
    """Report-only `heavy-default-branch-suite` findings (a decision, never auto-applied)."""
    out = []
    if not history:
        return out
    for wf in workflows:
        if wf.error or not wf.jobs or not wf.pr_events or not _pushes_default_branch(wf, default_branch):
            continue
        main = (history.get("main") or {}).get(wf.path)
        if not main or not main.get("batching"):
            continue
        wf_stats = (history.get("perWorkflow") or {}).get(wf.path) or {}
        days = (wf_stats.get("windowDays") if history.get("perWorkflowSampling") else None) \
            or history.get("windowDays") or history.get("requestedDays") or 1.0
        daily = main["eligibleRuns"] * main["perRunMinutes"] / days
        if daily < threshold:
            continue
        hourly, three = main["batching"]["hourly"], main["batching"]["threeHourly"]
        basis = main["costBasis"]
        f = finding(
            "heavy-default-branch-suite", "low", "wasted-runs", wf,
            f"runs on PRs and again on every `{default_branch}` push: ~{round(daily, 1)} "
            f"runner-min/day on `{default_branch}` ({main['eligibleRuns']} runs at "
            f"{main['perRunMinutes']} {basis}/run) against a threshold of {threshold} "
            "runner-min/day",
            "DECISION, not a cleanup (never part of --apply): add `schedule:` + `workflow_dispatch:` "
            f"and run on the newest `{default_branch}` commit, skipping when that commit already has "
            "a success for THIS workflow; keep the concurrency stanza and the PR trigger and "
            "required checks. Trade-off: a post-merge break is found up to one window later and the "
            "bisect range spans every merge in that window. PR and post-merge runs may not be "
            "equivalent (trigger/path filters differ). A deploy must resolve the newest verified "
            "commit when it starts, not trust the triggering SHA, and refuse a rollback.",
            measure="heavy-default-branch")
        f["reportOnly"] = True
        f["applyEligible"] = False
        f["alternatives"] = {"hourly": hourly, "threeHourly": three}
        f["estMinutesSaved"] = hourly["savedMinutes"]
        f["estDefaultBranchMinutes"] = hourly["savedMinutes"]
        f["estNote"] = (
            f"counterfactual (not observed), fixed UTC buckets over {main['eligibleRuns']} eligible "
            f"run(s), exclusive alternatives: hourly {hourly['occupiedBuckets']} bucket(s) -> "
            f"~{hourly['estimatedMinutes']} of {hourly['baselineMinutes']} min "
            f"(saves ~{hourly['savedMinutes']}, {hourly['savedPct']}%); every 3 h "
            f"{three['occupiedBuckets']} bucket(s) -> ~{three['estimatedMinutes']} min "
            f"(saves ~{three['savedMinutes']}, {three['savedPct']}%). Ranking uses the hourly "
            f"figure; do not add the alternatives. cost basis {basis}"
            + (" (wall-time fallback, not job-minutes)" if basis == "wall-time" else "")
            + f"; {main['cancelledRuns']} cancelled, {main['rerunAttempts']} rerun attempt(s), "
            f"{main['missingTimestamps']} missing-timestamp run(s) excluded; {_sample_note(history)}")
        out.append(f)
    return out


def summarize(findings):
    """Canonical aggregate. Default-branch savings of one workflow overlap (the
    queue stanza and batching avoid the same runs; hourly and 3 h are exclusive),
    so each workflow contributes its largest default-branch figure once."""
    other, db = 0.0, {}
    for f in findings:
        est = f.get("estMinutesSaved")
        if est is None:
            continue
        part = f.get("estDefaultBranchMinutes") or 0.0
        other += est - part
        if part:
            db[f["workflow"]] = max(db.get(f["workflow"], 0.0), part)
    return {"estMinutesSavedTotal": round(other + sum(db.values()), 1),
            "critical": sum(1 for f in findings if f["severity"] == "critical"),
            "note": "sum of per-finding estimates, except default-branch savings: one largest "
                    "figure per workflow; hourly/3 h alternatives are exclusive"}


def rank(findings):
    def key(f):
        est = f.get("estMinutesSaved")
        return (0 if f["severity"] == "critical" else 1,
                -(est or 0.0),
                SEVERITY_ORDER.get(f["severity"], 9), f["workflow"], f.get("job") or "", f["id"])
    return sorted(findings, key=key)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build_report(workflows, findings, required_info, history_info, repo=None, default_branch="main"):
    return {
        "repo": repo,
        "defaultBranch": default_branch,
        "workflowsFound": len(workflows),
        "requiredChecks": required_info,
        "history": history_info,
        "workflows": [{"file": w.path, "name": w.name, "triggers": sorted(w.on),
                       "jobs": sorted(w.jobs), "parseError": w.error} for w in workflows],
        "findings": rank(findings),
        "summary": summarize(findings),
    }


def render_text(report):
    lines = []
    title = report["repo"] or "local checkout"
    lines.append(f"CI OPTIMIZATION REPORT — {title} ({report['workflowsFound']} workflows, "
                 f"default branch {report['defaultBranch']})")
    lines.append("=" * len(lines[0]))
    if report["workflowsFound"] == 0:
        lines.append("No .github/workflows/*.yml found — nothing to audit (GitHub Actions only).")
        return "\n".join(lines) + "\n"
    rc = report["requiredChecks"]
    if rc["state"] == "unknown":
        lines.append("Required checks: UNKNOWN (" + ", ".join(f"{k}: {v}" for k, v in rc.get("sources", {}).items())
                     + ") — treating every PR workflow as possibly required")
    elif rc["contexts"]:
        lines.append("Required checks: " + ", ".join(f"`{c}`" for c in rc["contexts"])
                     + (" (partial — a source was unreadable; workflows matching none of these are "
                        "treated as possibly required)" if rc.get("partial") else ""))
    else:
        lines.append("Required checks: none configured")
    h = report["history"]
    if h["state"] == "measured":
        lines.append(f"Run history: {h['runs']} completed runs since {h['since']}; "
                     f"PR file lists for {h['prSampled']}/{h['prSeen']} PRs (minutes are over this window)")
        if h.get("perWorkflowSampling"):
            lines.append(f"Per-workflow sampling (up to {h.get('maxRuns')} completed runs each); "
                         "estimates are scaled to each workflow's true run count:")
            for path, w in sorted((h.get("perWorkflow") or {}).items()):
                tag = "  CAPPED" if w.get("sampleCapped") else ""
                lines.append(f"  {Path(path).name}: sampled {w['runs']} of {w['totalRuns']} runs{tag}; "
                             f"~{w['avgRunnerMinutesPerRun']} runner-min/run, "
                             f"~{w['totalRunnerMinutes']} runner-min total")
                for name, sp in (w.get("jobSpread") or {}).items():
                    lines.append(f"      job {name}: median {sp['median']} / p90 {sp['p90']} min "
                                 f"(n={sp['n']})")
            if h.get("sampleCapped"):
                lines.append("Sample CAPPED for at least one workflow: its true run count exceeds the "
                             "sample (raise --runs for a bigger sample)")
        elif h.get("sampleCapped"):
            lines.append(f"Sample CAPPED at {h.get('maxRuns')} runs: covers ~{h.get('windowDays')} of "
                         f"{h.get('requestedDays')} requested days; per-day figures use that span "
                         "(raise --runs for a fuller window)")
    else:
        lines.append(f"Run history: not measured ({h.get('reason', 'unavailable')})")
    lines.append("")
    if not report["findings"]:
        lines.append("No findings.")
        return "\n".join(lines) + "\n"
    for n, f in enumerate(report["findings"], 1):
        est = f.get("estMinutesSaved")
        est_s = "not measured" if est is None else f"~{est} min saved"
        where = Path(f["workflow"]).name + (f" / {f['job']}" if f.get("job") else "")
        times = f" x{f['occurrences']}" if f.get("occurrences", 1) > 1 else ""
        lines.append(f"{n:>2}. [{f['severity']}] {where} — {f['id']}{times} ({est_s})")
        lines.append(f"    {f['message']}")
        if f.get("estNote"):
            lines.append(f"    evidence: {f['estNote']}")
        for i, rec in enumerate(f["recommendation"].splitlines()):
            lines.append(("    fix: " if i == 0 else "         ") + rec)
    lines.append("")
    summ = report.get("summary") or {}
    lines.append(f"Estimated total: ~{summ.get('estMinutesSavedTotal', 0)} min over the sampled "
                 "window (default-branch savings counted once per workflow; hourly and 3 h "
                 "batching are alternatives, not additive)")
    return "\n".join(lines) + "\n"


def run_scan(args):
    root = Path(args.root)
    workflows = load_local(root)
    required = None if args.required_unknown else list(args.required or [])
    findings = analyze(workflows, local_root_files(root), required, args.default_branch)
    required_info = {"state": "unknown" if required is None else "known",
                     "contexts": sorted(required or []), "sources": {"flag": "supplied"}}
    return build_report(workflows, findings, required_info,
                        {"state": "not measured", "reason": "offline scan"},
                        default_branch=args.default_branch)


def run_report(args, gh=None):
    gh = gh or GitHub()
    repo = args.repo or origin_repo()
    if not repo:
        raise SystemExit("repo-optimize-ci: origin is not a GitHub remote; pass --repo OWNER/REPO "
                         "(GitHub Actions only)")
    try:
        meta = gh.api(f"repos/{repo}") or {}
        branch = args.default_branch or meta.get("default_branch") or "main"
    except ApiError:
        branch = args.default_branch or "main"
    if args.remote:
        try:
            workflows, root_files = load_remote(gh, repo, branch)
        except ApiError as exc:
            if exc.status == 404:
                workflows, root_files = [], set()
            else:
                raise SystemExit(f"repo-optimize-ci: could not check {repo}: {exc}")
    else:
        workflows, root_files = load_local(args.root), local_root_files(args.root)
    req = required_checks(gh, repo, branch)
    required = None if req["state"] == "unknown" else req["contexts"]
    findings = analyze(workflows, root_files, required, branch, partial=bool(req.get("partial")))
    history_info = {"state": "not measured", "reason": "--no-history"}
    if not args.no_history and workflows:
        try:
            hist = gather_history(gh, repo, args.runs, args.days, default_branch=branch,
                                  workflows=workflows)
            if hist["runs"]:
                threshold = getattr(args, "heavy_main_minutes_per_day", None)
                if threshold is None:
                    threshold = DEFAULT_HEAVY_MAIN_MINUTES_PER_DAY
                findings += heavy_findings(workflows, hist, branch, threshold)
                estimate([f for f in findings if f["measure"] != "heavy-default-branch"], hist)
                history_info = {"state": "measured", "since": hist["since"], "runs": len(hist["runs"]),
                                "prSampled": hist["prSampled"], "prSeen": hist["prSeen"],
                                "perWorkflow": hist["perWorkflow"], "defaultBranchRuns": hist["main"],
                                "sampleCapped": hist["sampleCapped"], "windowDays": hist["windowDays"],
                                "requestedDays": hist["requestedDays"], "maxRuns": hist["maxRuns"],
                                "perWorkflowSampling": hist["perWorkflowSampling"],
                                "heavyThresholdMinutesPerDay": threshold}
            else:
                history_info = {"state": "not measured", "reason": f"no completed runs since {hist['since']}"}
        except ApiError as exc:
            history_info = {"state": "not measured",
                            "reason": f"Actions history unreadable (HTTP {exc.status or '?'})"}
    return build_report(workflows, findings, req, history_info, repo=repo, default_branch=branch)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="offline analysis of a local checkout")
    scan.add_argument("--root", default=".")
    scan.add_argument("--required", action="append", metavar="CONTEXT",
                      help="a required status-check context (repeatable)")
    scan.add_argument("--required-unknown", action="store_true",
                      help="required checks could not be read — recommend only job-level filters")
    scan.add_argument("--default-branch", default="main")
    scan.add_argument("--json", action="store_true")
    rep = sub.add_parser("report", help="scan + required checks + run-history evidence (read-only)")
    rep.add_argument("--repo", help="OWNER/REPO (default: derived from origin)")
    rep.add_argument("--root", default=".")
    rep.add_argument("--remote", action="store_true",
                     help="read workflows via the contents API instead of a local checkout")
    rep.add_argument("--default-branch")
    rep.add_argument("--runs", type=int, default=100, help="max completed runs to sample PER WORKFLOW (repo-wide fallback if per-workflow listing is unreadable)")
    rep.add_argument("--days", type=int, default=30, help="history window in days")
    rep.add_argument("--heavy-main-minutes-per-day", type=float,
                     default=DEFAULT_HEAVY_MAIN_MINUTES_PER_DAY,
                     help="runner-minutes/day on the default branch above which a workflow that also "
                          f"runs on PRs is reported as heavy (default {DEFAULT_HEAVY_MAIN_MINUTES_PER_DAY:g})")
    rep.add_argument("--no-history", action="store_true")
    rep.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_scan(args) if args.command == "scan" else run_report(args)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
