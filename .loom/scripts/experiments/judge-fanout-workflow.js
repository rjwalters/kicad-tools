/**
 * judge-fanout-workflow.js — Judge fan-out prototype (issues #3739 → #3748)
 * =========================================================================
 *
 * A Claude Code Dynamic Workflow that reviews ONE pull request by fanning out
 * dimension-scoped reviewers, running a typed adversarial "verify" pass, and
 * reducing to a single schema-shaped verdict.
 *
 * STATUS: EXPERIMENT ARTIFACT — NOT WIRED INTO PRODUCTION.
 *   #3739 shipped this as an off-by-default DESIGN SKETCH with trivial prompts.
 *   #3748 HARDENED it into a load-bearing artifact: real dimension-scoped
 *   reviewer prompts, a fuller adversarial-verify prompt, and a No-Fable-Judge
 *   guard — while keeping every invariant below. It remains:
 *   - Under defaults/scripts/experiments/, which is NOT a discovered `workflows/`
 *     directory, so Claude Code will not auto-load it. It is inert until a human
 *     deliberately copies/points a top-level session at it (see README.md +
 *     docs/research/judge-fanout-measurement-runbook.md).
 *   - NOT referenced by defaults/.claude/commands/loom/sweep.md or
 *     defaults/roles/judge.md. The production judge path is untouched.
 *   - The measured comparison (precision / recall / latency / token cost vs. the
 *     single-pass Judge) is still a DEFERRED OPERATOR ACTION — it requires the
 *     Workflow tool, which only a top-level session has. See the runbook.
 *
 * SUBSTRATE: in-session, single-token, exactly ONE level deep (#3289).
 *   - `agent()` calls below are DIRECT — this workflow never calls `workflow()`,
 *     so it adds no second nesting level. The CLI itself rejects nested
 *     workflow() calls; we also avoid them by construction.
 *   - All agents share the session's single OAuth token. This workflow makes NO
 *     claim to multi-account rotation — that is a loom-daemon + spawn-claude.sh
 *     concern and lives on the other side of the substrate boundary.
 *
 * READ-ONLY / SIDE-EFFECT-FREE by construction:
 *   - Returns a verdict object. Applies NO labels, merges NOTHING, transitions
 *     no loom:pr / loom:changes-requested state, creates NO GitHub issues.
 *   - The caller (the measurement harness) decides what, if anything, to do with
 *     the verdict. During the experiment, the answer is "just record it".
 *
 * NO-FABLE-JUDGE INVARIANT (issue #3702; see
 * defaults/.claude/commands/loom/sweep.md § "Model selection for subagent
 * dispatch"): a fan-out reviewer's resolved model must NEVER be `fable`.
 * Reviewing security-adjacent diffs is precisely Fable's refusal surface, and a
 * refusing reviewer would poison the verdict. `assertNotFable()` below enforces
 * this for any model explicitly passed via `args.model` / per-dimension
 * override. When no model is passed the reviewer inherits the session default,
 * so the operator MUST NOT drive this workflow from a `fable` session (the
 * runbook says so explicitly).
 *
 * API: written against Claude Code CLI v2.1.206, whose workflow VM injects the
 * globals `agent`, `parallel`, `pipeline`, `workflow`, `budget`, `args`,
 * `console`, `log`, `phase`. There is NO `verify()` primitive; the adversarial
 * verify pass is an `agent()` call with a schema (confirmed against 2.1.206).
 *
 * Expected `args` shape (passed via the Workflow tool's `args`):
 *   {
 *     pr: number,               // PR number under review (for labeling only)
 *     diff: string,             // the unified diff text to review
 *     dimensions?: string[],    // optional override of the default dimension set
 *     model?: string,           // optional reviewer/verifier model (NEVER "fable")
 *     dimensionModels?: object,  // optional per-dimension model override map
 *   }
 *
 * @workflow-meta (illustrative — the real meta header format is owned by the
 * CLI's workflow-discovery loader; reproduced here as documentation only):
 *   name: judge-fanout-experiment
 *   description: Multi-dimension PR review fan-out + adversarial verify (experiment #3739/#3748)
 *   whenToUse: Experiment only — never on the production sweep/judge path.
 */

/* eslint-disable no-undef */ // agent/parallel/budget/args are workflow-VM globals.

// --- Configuration ----------------------------------------------------------

const DEFAULT_DIMENSIONS = [
  "correctness",
  "security-credential-surface",
  "test-coverage",
  "perf-simplification",
];

// Per-dimension review guidance. Each reviewer is scoped to EXACTLY one lens so
// the fan-out actually buys dimension coverage instead of four redundant
// general reviews. The guidance mirrors the production Judge dimensions
// (defaults/roles/judge.md) without importing any production code path.
const DIMENSION_GUIDANCE = {
  correctness: [
    "Focus: functional correctness and logic. Does the change do what it claims?",
    "Look for: off-by-one errors, inverted conditionals, unhandled null/undefined,",
    "  wrong operator precedence, race conditions, resource leaks, incorrect error",
    "  handling, edge cases (empty input, boundary values), and regressions in",
    "  behavior the diff touches.",
    "Ignore: style, security, test presence, and performance — other reviewers own those.",
  ],
  "security-credential-surface": [
    "Focus: the security and credential surface ONLY.",
    "Look for: hard-coded secrets/tokens/keys, credentials written to tracked or",
    "  non-gitignored paths, world-readable secret files (missing 0600/0700),",
    "  command/SQL/path injection, unsanitized input reaching a shell or eval,",
    "  overly broad file permissions, secrets logged or echoed, and auth checks",
    "  that are weakened or bypassed.",
    "Ignore: general correctness, test coverage, and performance.",
  ],
  "test-coverage": [
    "Focus: test coverage of the change ONLY.",
    "Look for: new/changed behavior that ships with NO test, tests that assert",
    "  nothing meaningful, happy-path-only coverage that skips error/edge cases,",
    "  and deleted or weakened assertions. Note when a bug-fix lacks a regression",
    "  test that would have caught the bug.",
    "Ignore: whether the non-test code is itself correct/secure/fast — flag only",
    "  the ABSENCE or weakness of tests.",
  ],
  "perf-simplification": [
    "Focus: performance and unnecessary complexity ONLY.",
    "Look for: work that scales with the dataset added to a hot/build path,",
    "  accidental O(n^2), redundant I/O or subprocess spawns in loops, and code",
    "  that is more complex than the problem requires (dead branches, needless",
    "  abstraction, duplicated logic that could be one call).",
    "Build-time perf is load-bearing, not advisory: downstream deploy scripts",
    "  hard-cap builds with `timeout`, so per-item build work is a real risk.",
    "Ignore: correctness bugs, security, and test coverage.",
  ],
};

// A finding a dimension reviewer emits. `evidenceLine` anchors the claim to the
// diff so the adversarial pass can check it is actually supported.
const FINDINGS_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["findings"],
  properties: {
    findings: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["severity", "dimension", "claim", "evidenceLine"],
        properties: {
          severity: { type: "string", enum: ["blocker", "major", "minor", "nit"] },
          dimension: { type: "string" },
          claim: { type: "string" },
          evidenceLine: {
            type: "string",
            description: "The diff hunk header or line the claim rests on.",
          },
        },
      },
    },
  },
};

// The adversarial pass re-emits each finding with a diffSupported verdict.
const VERIFIED_FINDINGS_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["findings"],
  properties: {
    findings: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["severity", "dimension", "claim", "diffSupported", "reason"],
        properties: {
          severity: { type: "string", enum: ["blocker", "major", "minor", "nit"] },
          dimension: { type: "string" },
          claim: { type: "string" },
          diffSupported: {
            type: "boolean",
            description: "True only if the diff actually contains evidence for the claim.",
          },
          reason: { type: "string", description: "Why the claim is / isn't supported." },
        },
      },
    },
  },
};

// --- No-Fable-Judge guard (#3702) -------------------------------------------

// A fan-out reviewer/verifier resolved model must NEVER be `fable`. This mirrors
// the production No-Fable-Judge hard invariant: the reviewer here plays the
// Judge role, and Fable refuses security-adjacent diffs. We only guard models
// that are EXPLICITLY passed — a null model inherits the session default, which
// the runbook forbids from being fable.
function assertNotFable(model, context) {
  if (model == null || model === "") return;
  const m = String(model).toLowerCase();
  // Match the alias `fable` and any pinned ID that carries the fable family name.
  if (m === "fable" || m.includes("fable")) {
    throw new Error(
      `judge-fanout: No-Fable-Judge invariant (#3702) violated — ${context} ` +
        `model must never be fable (got "${model}"). Use opus/sonnet instead.`,
    );
  }
}

// --- Prompt builders --------------------------------------------------------

function reviewPrompt(dimension, diff) {
  const guidance = DIMENSION_GUIDANCE[dimension] || [
    `Review ONLY through the "${dimension}" lens; ignore issues outside it.`,
  ];
  return [
    `You are a senior code reviewer scoped to EXACTLY ONE dimension: "${dimension}".`,
    `Review the unified diff below through that single lens and no other.`,
    ``,
    ...guidance,
    ``,
    `Rules:`,
    `  - For EVERY finding, set evidenceLine to the diff hunk header (e.g.`,
    `    "@@ -10,7 +10,9 @@ ...") or the exact added/removed line the claim rests`,
    `    on. A finding with no locatable evidence in the diff will be dropped by`,
    `    the downstream adversarial verifier, so do not guess.`,
    `  - Do NOT invent issues. If the diff is clean for your dimension, return an`,
    `    empty findings array. A clean pass is a valid, valuable result.`,
    `  - Grade severity honestly: blocker (must fix before merge), major`,
    `    (should fix), minor (worth noting), nit (cosmetic). Do not inflate.`,
    `  - Stay in scope: a correctness bug is NOT your finding if your dimension is`,
    `    test-coverage — flag only what your lens owns.`,
    ``,
    `Return ONLY the schema-shaped object. DIFF follows:`,
    ``,
    diff,
  ].join("\n");
}

function verifyPrompt(rawFindings, diff) {
  return [
    `You are an ADVERSARIAL verifier auditing findings produced by a panel of`,
    `single-dimension reviewers. Your job is to protect PRECISION: drop findings`,
    `the diff does not actually support so the final verdict is not polluted by`,
    `unverified nits, hallucinations, or out-of-scope remarks.`,
    ``,
    `For EACH finding below, decide diffSupported:`,
    `  - true  ONLY if the cited evidence (evidenceLine / claim) is genuinely`,
    `          present in the DIFF and the claim is a fair reading of it.`,
    `  - false if the evidence is not in the diff, the claim overstates what the`,
    `          diff shows, the issue is pre-existing (not introduced by this`,
    `          diff), or the finding is speculative / out of the reviewer's`,
    `          declared dimension.`,
    `Be strict and skeptical: when in doubt, mark diffSupported=false and explain`,
    `why in the reason field. Do NOT invent new findings and do NOT upgrade`,
    `severities — re-emit each finding's original severity/dimension/claim`,
    `verbatim, adding only diffSupported + reason.`,
    ``,
    `FINDINGS (JSON):`,
    JSON.stringify(rawFindings, null, 2),
    ``,
    `DIFF:`,
    diff,
  ].join("\n");
}

// --- The workflow body ------------------------------------------------------
//
// A top-level `return` is how a workflow script yields its result to the
// Workflow tool. (Workflow scripts run in a VM where top-level await + return
// are allowed — see CLI 2.1.206.)

const dimensions = Array.isArray(args?.dimensions) && args.dimensions.length
  ? args.dimensions
  : DEFAULT_DIMENSIONS;

// Guard: this prototype is single-PR and single-token. Refuse absurd fan-outs so
// a mis-call can't blow the shared budget. (budget is a shared HARD ceiling in
// 2.1.206; agent() throws once it is exhausted anyway — this is belt-and-braces.)
if (dimensions.length > 8) {
  throw new Error(
    `judge-fanout: ${dimensions.length} dimensions requested; cap is 8 (single-PR, single-token experiment).`,
  );
}

// No-Fable-Judge: validate every explicitly-passed model BEFORE dispatching any
// agent, so a fable model fails fast instead of poisoning a partial fan-out.
const reviewerModel = args?.model ?? null;
assertNotFable(reviewerModel, "fan-out reviewer/verifier");
const dimensionModels = (args && args.dimensionModels) || {};
for (const dim of dimensions) {
  assertNotFable(dimensionModels[dim], `dimension reviewer "${dim}"`);
}

// Resolve the model for one dimension: per-dimension override → global override →
// null (inherit session default). Never fable (asserted above).
function reviewerModelFor(dim) {
  return dimensionModels[dim] ?? reviewerModel ?? null;
}

// Build the agent() options for one dimension reviewer, omitting `model` when it
// resolves to null so the agent inherits the session default cleanly.
function reviewerOpts(dim) {
  const opts = {
    label: `review:${dim}`,
    phase: "review", // explicit phase group avoids racing the global phase()
    // #3705 note: effort IS recoverable in-session (agent accepts opts.effort);
    // correctness gets the highest tier, the rest medium.
    effort: dim === "correctness" ? "high" : "medium",
    schema: FINDINGS_SCHEMA,
  };
  const m = reviewerModelFor(dim);
  if (m != null && m !== "") opts.model = m;
  return opts;
}

// 1. FAN OUT — one reviewer per dimension. Direct agent() calls => exactly one
//    level deep. parallel() is a BARRIER: it awaits all reviewers.
const rawFindings = (
  await parallel(
    dimensions.map((dim) => () =>
      agent(reviewPrompt(dim, String(args?.diff ?? "")), reviewerOpts(dim)),
    ),
  )
)
  // agent() returns null if the user skips it or it dies terminally — filter those.
  .filter(Boolean)
  .flatMap((r) => (r && Array.isArray(r.findings) ? r.findings : []));

// Short-circuit: nothing to verify.
if (rawFindings.length === 0) {
  return {
    pr: args?.pr ?? null,
    verdict: "approve",
    findings: [],
    droppedUnverified: 0,
    dimensionsCovered: dimensions,
    note: "no findings from any dimension reviewer",
  };
}

// 2. ADVERSARIAL VERIFY — an agent() with a schema (NOT a verify() primitive).
//    Drops findings the diff doesn't actually support. Reuses the global model
//    override when set (never fable — asserted above).
const verifyOpts = {
  label: "adversarial-verify",
  phase: "verify",
  effort: "high",
  schema: VERIFIED_FINDINGS_SCHEMA,
};
if (reviewerModel != null && reviewerModel !== "") verifyOpts.model = reviewerModel;

const verifyResult = await agent(
  verifyPrompt(rawFindings, String(args?.diff ?? "")),
  verifyOpts,
);

const verified = ((verifyResult && verifyResult.findings) || []).filter(
  (f) => f && f.diffSupported === true,
);

// 3. TYPED REDUCE — plain JS. No free-text/label parsing. READ-ONLY: we return a
//    verdict; we apply nothing to the PR. A blocker or major finding fails the
//    review; minor/nit findings are recorded but do not block.
const hasBlocker = verified.some(
  (f) => f.severity === "blocker" || f.severity === "major",
);

return {
  pr: args?.pr ?? null,
  verdict: hasBlocker ? "changes-requested" : "approve",
  findings: verified,
  droppedUnverified: rawFindings.length - verified.length,
  dimensionsCovered: dimensions,
  // NOTE: the caller decides what to do with this. This workflow merges nothing,
  // labels nothing, and creates no issues — it is a review-quality experiment.
};
