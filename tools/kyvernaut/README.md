# Kyvernaut AI Maintainer Assistant — POC: diff-to-test-scope mapper

This is a working proof-of-concept for the **Phase 0 + Phase 2** slice of the
proposed AI Maintainer Assistant (Kyvernaut): a machine-readable map from Kyverno source
paths to the test suites that cover them, plus a tool that consumes it to
turn a PR diff into a scoped test plan and a merge-risk assessment.

I picked this slice deliberately, not the flashiest one (Dependabot
auto-merge, Slack bot). It's the piece that:

1. **Requires no new infrastructure.** No GitHub App, no sandbox runtime, no
   webhook receiver — just the repo's own structure. You can run it today.
2. **Is falsifiable.** I ran it against real merged PRs from `kyverno/kyverno`
   history and checked the output against what a human reviewer would
   actually want to run. Results below.
3. **De-risks the riskiest part of the larger proposal.** Every other phase
   (auto-merge, scoped CI, issue repro, Q&A) depends on the agent correctly
   understanding "what does this diff touch and how sensitive is that area."
   If that classification is wrong, everything built on top of it is wrong
   in a way that's hard to catch. So it's the part worth proving out first,
   and worth maintainers being able to audit as a plain YAML file rather
   than a black-box model judgment.
4. **Encodes real project knowledge, not invented structure.** The risk
   tiers and human-review flags mirror what's already in `CODEOWNERS` (e.g.
   `pkg/cosign`, `pkg/notary`, `api/kyverno/v1` already have named
   always-review owners) — this doesn't introduce a new policy, it makes an
   existing one machine-actionable.

## What's here

- `path-test-map.yaml` — the manifest itself. ~20 rules covering the main
  `pkg/`, `api/`, `cmd/cli/`, and `charts/` areas, each with: unit test
  packages, chainsaw conformance suites, CLI suites, a risk tier, whether
  codegen verification is required, and whether an automation agent must
  defer to a human regardless of CI outcome.
- `scope_tests.py` — the mapper. Takes a commit SHA, a `git diff` ref range,
  or a plain file list, and emits either a human-readable plan or JSON (for
  wiring into a GitHub Action / bot comment later).
- `test_scope_tests.py` — 6 unit tests asserting the rules behave as
  intended (security paths get flagged, dependency-only diffs are
  auto-merge eligible, most-specific-rule-wins, etc). All passing.
- `example-AGENTS-stub/pkg-cel-policies-ivpol-AGENTS.md` — a worked example
  of the "safe automation boundaries" per-directory doc from the proposal,
  applied to a real, security-sensitive package (`pkg/cel/policies/ivpol`).

## Demo: three real commits from `kyverno/kyverno` main

### 1. Feature PR — `feat(mpol): add auditAnnotations support to MutatingPolicy` (#16721)
25 changed files spanning CEL compiler code, the CLI, CRDs, and new
conformance fixtures.

```
Overall risk: MEDIUM
Auto-merge eligible: False

Unit test packages to run:
  go test ./cmd/cli/kubectl-kyverno/... ./pkg/cel/policies/mpol/...

Chainsaw conformance suites to run:
  chainsaw test test/conformance/chainsaw/cli/**
  chainsaw test test/conformance/chainsaw/mutating-policies/**
  chainsaw test test/conformance/chainsaw/namespaced-mutating-policies/**

CLI test targets to run:
  make test-cli
```

Instead of running the full suite (`test/conformance/chainsaw` alone has
~50 top-level directories), a scoped CI run for this PR is 3 suites + 2
package targets.

### 2. Security-sensitive fix — `fix(ivpol): keep autogen variants per-policy` (#16731)
2 changed files, both in `pkg/cel/policies/ivpol/engine/`.

```
Overall risk: HIGH
Auto-merge eligible: False

⚠️  HUMAN REVIEW REQUIRED — do not auto-merge:
   - pkg/cel/policies/ivpol/engine/reconciler.go -> rule 'cel-ivpol':
     Image verification policy path — same sensitivity class as
     pkg/cosign / pkg/notary.
   - pkg/cel/policies/ivpol/engine/reconciler_test.go -> rule 'cel-ivpol': ...
```

This is the guardrail in action: a two-line-diff PR in a security-relevant
path is correctly kept out of any hypothetical auto-merge path regardless
of how clean the diff looks or how green CI is.

### 3. Dependabot-style PR — `chore(deps): bump cosign` (#16738)
Only `go.mod` / `go.sum` changed.

```
Overall risk: LOW
Auto-merge eligible: True

Unit test packages to run:
  (none matched)
Chainsaw conformance suites to run:
  (none matched)
```

This is exactly the shape of diff Phase 1's Dependabot auto-merge logic
needs to recognize as low-scope — this mapper is the classification input
that check would consume.

## How do we know the scoping is actually correct?

Short answer: the 3 demo commits above prove the tool runs and produces
*plausible* plans — they don't prove the plans are *complete*. That's a
different, harder claim, and it's worth being explicit about the gap.

**What "correct" actually means here.** A false positive (running an extra
suite that wasn't needed) costs CI minutes. A false negative (skipping a
suite that would have caught a real regression) costs a shipped bug — the
failure modes are not symmetric, so the tool has to be conservative by
default, not just usually-right.

**A real bug this surfaced.** While building this out I found that a file
like `pkg/utils/kube/labels.go` — a generic, plausibly cross-cutting utility
— matched the fallback rule, got unit tests, and produced a plan with zero
conformance suites *and no warning*. It looked like a complete, clean plan
while silently under-scoping a change that could affect validate, mutate,
and generate paths simultaneously. Multi-rule matching within one diff was
never the risk (that already worked, see Demo 1's 3-suite union); untagged
fallback matches masquerading as complete coverage was. Fixed now: any file
that only resolves through `pkg-fallback` with no mapped conformance suite
is flagged as `low_confidence_files` and blocks `auto_merge_eligible`, even
at low risk. I also added `AmbiguousMatch` handling for the case where two
different rules could claim the same path at equal specificity — previously
that would've silently picked whichever rule happened to sort first, which
is exactly the kind of thing that could let a security-sensitive rule lose
a coin-flip to a low-risk one. Now it's a hard fail-safe: unmatched +
forced human review, never a silent pick. Both are covered by regression
tests in `test_scope_tests.py`.

**How I'd validate this for real, beyond hand-picked demos** (Phase 0/1 work,
not something 15 days of solo POC time can fully deliver):
1. **Backtest against historical regressions.** For PRs that were later
   reverted or had a same-week follow-up fix, compute what this tool
   would have scoped for the *original* PR and check whether the scope
   would have included the suite that eventually caught the regression.
   This is the actual correctness metric — hit rate against real misses,
   not "did it run without crashing."
2. **Shadow mode before any gating.** Run the scoped plan alongside the
   full suite on every PR for a period, compare results, and only let the
   scoped plan actually skip suites once it's been shown to agree with the
   full suite's pass/fail outcome across a large enough sample. The tool
   should start as a comment-only advisor, not a CI gate.
3. **A CI check on the manifest itself.** Fail if a new top-level `pkg/` or
   `test/conformance/chainsaw/` directory appears with no corresponding
   rule — so coverage gaps get caught at manifest-authoring time instead of
   silently falling into `pkg-fallback` forever.
4. **Maintainer sign-off on the risk tiers.** I derived `high`/
   `requires_human_review` from `CODEOWNERS`, but CODEOWNERS encodes "who
   reviews this," not "what's the blast radius if scoping gets this wrong."
   Those aren't guaranteed to be the same list, and only a maintainer can
   confirm they are.

## Try it yourself

```bash
git clone --depth 60 https://github.com/kyverno/kyverno.git
pip install pyyaml --break-system-packages
python3 scope_tests.py --repo kyverno --commit <any-sha>
python3 -m pytest test_scope_tests.py -q
```

## What I'd do next 

- **Phase 0 (this POC → hardened):** get maintainer sign-off on the risk
  tiers in `path-test-map.yaml` (I derived them from `CODEOWNERS` plus my
  own read of the code, but the actual security/blast-radius judgment
  should be maintainer-approved, not agent-approved). Expand coverage from
  ~20 rules to full `pkg/` coverage; add a CI check that fails if a new
  top-level `pkg/` or `test/conformance/chainsaw/` directory appears without
  a corresponding rule, so the map can't silently go stale.
- **Phase 1:** wire `auto_merge_eligible` + `requires_human_review` from
  this tool into the Dependabot-PR check, and the `codegen_verify_required`
  flag into the codegen gate — both already described in the proposal as
  separate bullet points, both are direct consumers of this output.
- **Phase 2:** port `scope_tests.py` into a GitHub Action that posts the
  plan as a PR comment and only triggers the listed chainsaw suites,
  measuring actual CI time saved on a sample of real PRs before proposing
  it as default behavior (a wrong scope-down that skips a suite that should
  have run is worse than a slow CI run, so this needs a shadow-mode period
  where it comments but doesn't actually gate anything).
- **Phase 0, doc side:** repeat the `example-AGENTS-stub/` pattern for the
  other high-traffic directories named in the proposal
  (`pkg/engine/`, `pkg/webhooks/`, `pkg/controllers/`).
