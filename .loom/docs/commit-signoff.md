# Commit sign-off (DCO): roles never attest it

Some repositories require a [Developer Certificate of Origin](https://developercertificate.org/)
(DCO) sign-off on **every** commit: a `Signed-off-by: Name <email>` trailer,
produced by `git commit --signoff`, usually enforced by a required `sign-off` /
`DCO` status check.

## Policy (operator ruling 2026-10-25, #13089)

**No role attests a DCO sign-off.** A `Signed-off-by:` trailer is a human's
certification of origin; an agent cannot give it. Builder, Doctor and every other
role therefore:

- never pass `--signoff` / `-s` (including on `--amend` and rebase re-authoring);
- never write a `Signed-off-by:` trailer by hand;
- **route to the operator instead.** When an agent-authored PR opens in a repo
  whose required checks match `dco` or `sign-?off`, apply `loom:operator` with a
  comment saying an operator-signed replacement is needed, **before** any Judge or
  Doctor pass.

The `commit.signoff` knob in `.loom/config.json` and the `CONTRIBUTING.md` /
`DCO`-file heuristic that used to make the roles sign off are retired. A repo that
still sets `commit.signoff` gets no behavior change from it.

## Detecting a DCO requirement

Check the base branch's required status checks (branch protection or ruleset) for
a name matching `dco` or `sign-?off`. A `CONTRIBUTING.md` or `DCO` file that
mentions `Signed-off-by` is a hint to look, not proof the check is required.

## Routing recipe

```bash
gh pr edit <PR> --add-label "loom:operator"
./.loom/scripts/post-comment.sh <PR> --body "DCO required: agent-authored commits carry no sign-off (operator ruling 2026-10-25, #13089). An operator-signed replacement PR is needed."
```

The operator signs the work off and opens the replacement; the agent PR is closed.
