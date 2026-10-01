---
name: "decide"
description: "Put operator decisions to the operator as ranked options — best to worst, each with why — so they can answer with a number"
domain: repo
type: command
user-invocable: true
---

# /repo:decide — Ranked Operator Decisions

When work is blocked on a decision that belongs to the operator (cost, an
access grant, a policy change, something irreversible, a call about another
person's system), don't ask an open question and don't bury the choice in a
paragraph. Lay out the options **best to worst**, mark the one you recommend,
and give **the reason each option ranks where it does**. Then the operator can
answer "Q1: 1, Q2: 3" instead of re-deriving the tradeoffs you already worked
out.

This command only drafts the questions. It does not decide anything.
Presenting the draft in the session needs no confirmation. Posting it anywhere
else (a chat room, an issue, a PR) is outward-facing and follows the same
confirm-first rule as [[followups]], unless the user's own invocation already
said where to post.

## Usage

```
/repo:decide                     # Draft ranked questions for the decisions this session is blocked on
/repo:decide "<topic>"           # Draft for one named decision
/repo:decide --post              # Draft, confirm, then post where this project routes operator questions
```

## Steps

### 1. Collect only real operator decisions

Go through the session (and the issue or PR at hand) for choices that are
**genuinely the operator's**. Leave out anything you can settle from the code,
the docs, or a sensible default. Settle those yourself and say so in one line.
A decision belongs to the operator when it involves:

- money or a standing cost;
- granting, widening, or minting a credential or access;
- changing a written policy or making an exception to it;
- an irreversible or outward-facing action (publishing, deleting, migrating);
- a system, account, or budget someone else owns.

If nothing qualifies, say so and stop. A ranked list of fake choices wastes
the operator's time.

### 2. Build the options for each decision

- **2–4 options per question**, each actually different. It's not an option
  if you would never recommend it under any circumstances.
- Include **"defer / do nothing"** when it's a real choice, and say what it
  costs.
- For each option, find out, don't guess, what it costs, what it grants, and
  whether it can be undone. Mark anything unverified as unverified.

### 3. Rank them best to worst and give the why

- The **recommended option goes first** and carries `(recommended)`.
- Each option gets a **Why** that names the tradeoff that puts it at *that*
  rank: what it wins over the options below it, and what it gives up compared
  with the ones above it. "Why: it's best" is not a why. Neither is restating
  the option.
- The last option's why says what makes it worst, so the operator can see it
  was considered and rejected, not forgotten.

### 4. Format

```
Q<n>. <the decision in one line, as a question>
   <one or two lines of context: what is blocked and why it is the operator's call>
  1. <option> (recommended). Why: <what it wins, what it costs>
  2. <option>. Why lower: <what it gives up vs. 1, what it still wins>
  3. <option>. Why last: <what makes it worst>
```

- Number both questions and options so a one-line reply is unambiguous.
- **At most four questions per post.** If there are more, the work needs
  splitting, not a longer message.
- Put the dated, time-critical fact in the question itself (a price change,
  a beta ending, a deadline).
- Link the issue or PR where the ruling will be recorded. If a project uses
  Loom, that's the issue labelled `loom:operator-decision`.

### 5. Deliver

- **Where:** follow the project's own convention for operator questions (its
  `CLAUDE.md` / `AGENTS.md`, or where the user tells you). With no convention,
  present them in the session.
- **Scrub before it leaves the session.** A chat room or public issue is a
  disclosure surface. No secrets, keys, tokens, or host addresses. On a public
  target, run [[scrub]]'s detection classes over the draft first, as
  [[followups]] step 3b does.
- **Attribute it.** If you post through a shared or bot account, say that it
  was drafted by an agent session and on whose behalf.

### 6. Record the answer

When the operator answers, write the ruling where the decision lives (the
linked issue, a decision log, the doc that states the policy), not only in
chat. Chat scrolls away, and the next session reads the repo.

## Example

```
Q1. How should Cloudflare Workers reach our loopback-only telemetry store?
   Workers run on Cloudflare's edge; the store has no public ports. Needs a policy exception or a pull design.
  1. Outbound-only tunnel behind a service-token access policy → allowlisting collector (recommended).
     Why: no inbound port, write-only credential, no per-Worker code.
  2. The same, via a separate relay host. Why lower: better isolation, but a standing host cost for a modest gain.
  3. Poll the provider's query API. Why lower: zero ingress, but 7-day retention and lossy traces.
  4. A hosted SaaS backend. Why last: a second backend, which the single-store policy forbids.
```
