# Kyvernaut — Kyverno AI Maintainer Assistant

This directory contains Kyvernaut's Phase 0–4 foundations: repository safety
metadata, a diff-to-test-scope mapper, dependency PR decisions and a dormant
merge executor, PR hygiene, issue triage, and a dormant isolated reproduction
harness, a dormant cross-repository documentation draft workflow, plus
retrieval-only documentation Q&A. The checked-in policy remains in `shadow`
mode with reproduction and documentation drafts separately disabled, so the
system can be evaluated without authorizing dependency merges,
issue-manifest execution, or website writes.

The decisions are deterministic and auditable rather than opaque model
judgments. Risk tiers and human-review flags encode repository structure and
`CODEOWNERS` knowledge in reviewed YAML, and every stateful workflow executes
only trusted default-branch code.

## What's here

- `path-test-map.yaml` — the versioned manifest. Its rules cover the main
  `pkg/`, `api/`, `cmd/cli/`, and `charts/` areas, each with: unit test
  packages, chainsaw conformance suites, CLI suites, a risk tier, whether
  codegen verification is required, and whether an automation agent must
  defer to a human regardless of CI outcome. It also records current
  coverage gaps so newly added directories fail closed.
- `scope_tests.py` — the mapper. Takes a commit SHA, a `git diff` ref range,
  or a plain file list, and emits either a human-readable plan or JSON (for
  GitHub Actions and bot comments). `--validate` checks its
  schema, safety invariants, and repository directory coverage.
- `conformance-profiles.yaml`, `scoped_ci.py`, and
  `.github/workflows/kyvernaut-scoped-tests.yaml` — translate the map into
  capped unit/CLI/Chainsaw matrices and run them on PR merge commits. Planning
  always uses the exact trusted base commit; unsupported specialized suites
  and uncertain paths require authoritative full CI instead of producing a
  false scoped green.
- `test_scope_tests.py` — regression tests asserting the rules behave as
  intended (security paths get flagged, dependency-only diffs are
  candidates, low-risk non-dependency diffs are not, prefix matches respect
  path boundaries, most-specific-rule-wins, etc).
- `.github/workflows/kyvernaut-path-test-map.yaml` — a read-only CI check
  which validates the manifest and runs its tests whenever tracked source
  or conformance directory structure changes.
- `.github/ai-maintainer.yaml` — repository-owned controls for enablement,
  shadow/active mode, bot identities, allowed bump types/files, hold labels,
  required CI/mergeability, and the kill switch.
- `dependency_pr.py` — the Phase 1 dependency-PR decision engine. It emits
  a deterministic, auditable JSON recommendation and deliberately has no
  GitHub client or merge capability.
- `dependency_batch.py` and
  `.github/workflows/kyvernaut-dependency-merge.yaml` — a scheduled,
  rate-limited executor boundary. It is dormant in the checked-in shadow
  mode and revalidates immutable evidence immediately before a head-bound
  squash merge when maintainers explicitly activate it.
- `pr_shadow.py` and `.github/workflows/kyvernaut-pr-advisor.yaml` — the
  operational shadow integration. It executes trusted default-branch code,
  updates one advisory comment, and uploads the exact decision artifacts.
- `pr_hygiene.py`, `hygiene_batch.py`, and
  `.github/workflows/kyvernaut-pr-hygiene.yaml` — behind/stale evaluation
  plus weekday, capped, cooldown-aware author/reviewer reminders.
- `issue_triage.py` and `.github/workflows/kyvernaut-issue-triage.yaml` —
  deterministic bug/feature/question plus CLI/webhook classification,
  missing reproduction-field requests, and a capped, additive-only managed
  label executor that remains dormant in shadow mode.
- `repro_plan.py`, `repro_execute.py`, and
  `.github/workflows/kyvernaut-repro-plan.yaml` — maintainer-dispatched
  extraction, static validation, an ephemeral KinD sandbox, bounded
  execution/diagnostics, guaranteed teardown, and an audited issue result.
  The checked-in policy leaves the execution job unreachable.
- `docs_requirement.py` — detects user-facing diffs without an in-repo docs
  change, `kyverno/website` issue/PR link, or reviewed exemption and includes
  the result in the PR advisory artifact/comment.
- `docs_draft.py` and
  `.github/workflows/kyvernaut-docs-draft.yaml` — a manual, dormant planner
  and cross-repository executor for one draft `kyverno/website` PR. A
  write-authorized maintainer supplies the exact Markdown path and complete
  content; source and target evidence are revalidated before a
  website-scoped GitHub App token can create the deterministic branch,
  signed-off commit, and draft PR.
- `qa-sources.yaml`, `qa_retrieval.py`, and
  `.github/workflows/kyvernaut-docs-qa.yaml` — a bounded, retrieval-only
  Phase 4 foundation over reviewed repository Markdown. It emits exact
  passages with source hashes and line citations when deterministic
  confidence thresholds pass, otherwise it escalates without answering.
  The manual workflow is read-only and has no Slack or Discussions token.
- `SAFE_AUTOMATION.md` — the explicit path, permission, kill-switch, audit,
  and activation boundaries.
- `PROJECT_STATUS.md` — requirement-by-requirement implementation evidence
  and remaining gaps; dormant code is not represented as production proof.
- `pkg/engine/AGENTS.md`, `pkg/webhooks/AGENTS.md`,
  `pkg/controllers/AGENTS.md`, and `test/conformance/AGENTS.md` — local
  entry points, invariants, focused commands, and autonomous-edit boundaries
  for the high-traffic areas named in Phase 0.
- `task-index.yaml` and `task_index.py` — a queryable build/test/lint/codegen
  task catalog with mutation, network, cluster, destructiveness, and
  automation metadata. Validation catches stale Makefile targets.
- `.github/labels.yml` — the central issue/PR label catalog, including
  Kyvernaut hold, opt-out, approval, and review controls.
- `MODULE_BOUNDARIES.md`, `module-boundaries.yaml`, and
  `module_boundaries.py` — the evidence-backed decision to retain the root
  product module, two isolated build-tool modules, and separately versioned
  API/SDK repositories, plus drift validation.
- `change-metadata.yaml` and `change_metadata.py` — stable docs-only,
  generated-only, API-change, and explicit breaking/non-breaking API
  classifications. The PR advisor records this decision and fails metadata
  completeness when an API diff lacks exactly one compatibility declaration.

## Safety boundary: candidate does not mean merge

`auto_merge_eligible` is an opt-in, scope-only signal. Today only the
`dependency-metadata` rule (`go.mod` and `go.sum`) opts in. A low-risk docs,
generated-CRD, or conformance-fixture-only change is not an auto-merge
candidate.

Even when the signal is true, a Phase 1 caller must still verify all of:

1. The PR author is the configured Dependabot/Renovate identity.
2. The dependency change is patch/minor, not major or an unclassified ref.
3. Every required CI and policy check is green.
4. No maintainer hold label is present.
5. The repository/workflow kill switch is not active.
6. The PR remains open, non-draft, unchanged at the authorized head SHA, and
   contains no configured breaking-change marker.

The mapper cannot grant merge permission by itself.

The dependency evaluator also fails closed: missing CI or mergeability
signals are `unknown` and block eligibility. In `shadow` mode a fully valid
PR produces `would_merge` while `action_authorized` remains false. The
checked-in repository policy enables the comment-only advisor but remains
in shadow mode; setting `enabled: false` suppresses its new actions.

## GitHub operation

The PR advisor listens to pull-request changes and evaluates the current
file list without checking out PR code. It has only `checks:read`,
`contents:read`, and `pull-requests:write`; checkout credentials are not
persisted. One marker-owned comment is updated in place, and the full JSON
evidence is retained for 30 days as a workflow artifact. The sole runtime
package installed under that token is locked to the CPython 3.13 Linux wheel
by version and SHA-256.

Set the repository Actions variable `KYVERNAUT_PAUSED=true` (also accepts
`1`, `yes`, or `on`) for the immediate kill switch. The workflow checks it
before checkout, API reads, artifact upload, or comment writes.

The advisor's event-time check snapshot is advisory and may be pending.
The separate dependency workflow runs on a schedule, considers at most 50
open PRs, and authorizes at most three merges per run. In active mode it
refetches the PR immediately before each operation and refuses to merge if
the author, base, title, body hash, labels, files, head SHA, checks, statuses,
or mergeability changed. The GitHub merge request is SHA-bound and branch
protection/rulesets remain the final independent gate. In the checked-in
shadow mode, its execution batch is always empty.

The scoped-test workflow is read-only and runs in `shadow_compare` mode on
PRs. Its planner checks out the exact base SHA into a separate directory and
collects changed filenames as JSON, so a PR cannot rewrite its own selection
policy. It emits at most 12 unit and 8 conformance jobs, runs the selected
CLI suite when required, and reuses the existing conformance runner's
cluster/config profiles. Pull-request tests receive no persisted checkout
credential; the Chainsaw input gets the non-secret sentinel `disabled`
instead of `github.token`.

Unmatched, ambiguous, and generic low-confidence paths expand to
`go test ./...` and explicitly require authoritative full conformance. The
same is true for `**`, over-cap selections, and specialized `custom-sigstore`
infrastructure that cannot be represented faithfully by the generic runner.
Those limitations remain visible in the selection artifact and step summary.
Existing full unit/codegen/conformance workflows remain authoritative; the
scoped workflow must not become a required replacement until real shadow
comparison data demonstrates an acceptable false-negative rate.

The event advisor recommends a branch update when GitHub reports `behind`.
The scheduled hygiene workflow examines the 50 oldest open PRs on weekdays,
posts at most 10 reminders, and observes a seven-day per-comment cooldown.
It does not call GitHub's branch-update or workflow-rerun APIs.
The repository's existing `PR Branch Auto-Updater` already owns branch
updates with dedicated GitHub App credentials, so Kyvernaut does not create
a competing updater.

Issue triage runs when an issue is opened, edited, labeled, or unlabeled. It
parses the repository issue-form headings, never executes issue content, and
updates one advisory comment. Security and `kyvernaut:no-triage` issues are
excluded. The checked-in shadow mode only suggests labels. In active mode, a
separate executor may add at most four labels from the reviewed managed-label
catalog; it never removes or replaces labels and re-fetches the issue to verify
its state, title/body hashes, and complete current label set before acting.
Both the decision and any execution result are retained in the audit artifact.

Maintainers can trigger reproduction by applying
`kyvernaut:repro-approved` or by manually dispatching the workflow. The
planner requires that approval label in either path and rejects privileged,
RBAC, secret,
host-access, explicit-namespace, external-call, custom-command,
service-account-token, unapproved/unpreloaded-image, unsafe pull-policy, and
oversized inputs. Invalid input produces errors and no apply-ready manifest.

Execution requires all three independent gates: global `active` mode,
`issue_reproduction.enabled: true`, and the approval label. It builds trusted
default-branch Kyverno, installs it in an ephemeral KinD cluster, preloads the
small image allowlist, creates a dedicated namespace with quota/limit/default
deny controls, cuts and verifies cluster egress, then refetches the issue and
compares the exact plan and manifest bundle before applying anything.
Issue-authored YAML is passed to `kubectl` over stdin with argv execution,
never through a shell. The result includes the expected statement,
per-document API acceptance/rejection, resources, policies, reports, events,
pod status, and bounded logs. Teardown and egress-rule removal run under
`always()`. The checked-in shadow/disabled configuration authorizes none of
this.

Kyverno's existing `Codegen` workflow remains the blocking source of truth
for `make codegen-all-code`, documentation generation, and
`make verify-codegen`. Kyvernaut consumes codegen risk metadata but does not
create a redundant gate.

Documentation-impact detection remains advisory during ordinary PR events.
The separate documentation draft workflow is manual and dormant. It runs
trusted default-branch code with a read-only workflow token, requires global
active mode plus `documentation.draft_pull_requests.enabled: true`, and
accepts only canonical `.md` content under the reviewed website root
`src/content/docs/docs/`. Kyvernaut does not synthesize prose from PR text:
a source-repository writer supplies the complete content as data. The planner
binds the source PR head, title/body hashes, labels, complete changed-file
list, website base SHA, existing target blob, path, and content hash.

Immediately before mutation it rechecks the dispatcher's permission and both
repositories. Only then is a short-lived GitHub App token requested, scoped
to `kyverno/website` with `contents:write` and `pull-requests:write`. The
executor rechecks the website base and target again, creates at most one
deterministic branch/commit/draft PR, and treats an identical partial retry
idempotently. It cannot merge, mark the PR ready, write release branches, or
change the source PR. Website CI and maintainer review remain authoritative.

## Demo: three real commits from `kyverno/kyverno` main

### 1. Feature PR — `feat(mpol): add auditAnnotations support to MutatingPolicy` (#16721)
25 changed files spanning CEL compiler code, the CLI, CRDs, and new
conformance fixtures.

```
Overall risk: MEDIUM
Auto-merge candidate: False

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
Auto-merge candidate: False

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
Auto-merge candidate: True

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

**Fail-closed manifest checks.** `scope_tests.py --validate` rejects
duplicate IDs/prefixes, invalid risk values, unsafe auto-merge opt-ins, and
stale directory coverage. Existing source/suite gaps are explicit
allowlists in the manifest; adding a new top-level `pkg/` or chainsaw suite
without either a real mapping or an explicit fallback decision fails CI.

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
3. **Keep the manifest CI check blocking.** It now fails if a new top-level
   `pkg/` or `test/conformance/chainsaw/` directory appears with neither a
   mapping nor an explicit fallback decision.
4. **Maintainer sign-off on the risk tiers.** I derived `high`/
   `requires_human_review` from `CODEOWNERS`, but CODEOWNERS encodes "who
   reviews this," not "what's the blast radius if scoping gets this wrong."
   Those aren't guaranteed to be the same list, and only a maintainer can
   confirm they are.

## Try it yourself

```bash
git clone --depth 60 https://github.com/kyverno/kyverno.git
pip install pyyaml --break-system-packages
python3 kyvernaut/scope_tests.py --repo . --validate
python3 kyvernaut/scoped_ci.py --repo . --validate
python3 kyvernaut/issue_triage.py --repo . --validate-labels
python3 kyvernaut/qa_retrieval.py --repo . --validate
python3 kyvernaut/module_boundaries.py --repo . --validate
python3 kyvernaut/change_metadata.py --repo . --validate
python3 kyvernaut/task_index.py --repo . --validate
python3 kyvernaut/task_index.py --category test
python3 kyvernaut/scope_tests.py --repo . --commit <any-sha>
python3 -m pytest kyvernaut -q
```

## What I'd do next 

- **Phase 0 (this POC → hardened):** get maintainer sign-off on the risk
  tiers in `path-test-map.yaml` (I derived them from `CODEOWNERS` plus my
  own read of the code, but the actual security/blast-radius judgment
  should be maintainer-approved, not agent-approved). Expand the current
  rules to full `pkg/` coverage. The stale-map CI check is implemented;
  retire entries from the explicit fallback allowlists as maintainers
  confirm mappings.
- **Phase 1:** keep the dependency executor in shadow mode until historical
  and live shadow data contain meaningful failures, maintainers approve the
  visible-check/ruleset model, and the activation/rollback drill in
  `SAFE_AUTOMATION.md` passes. The executor and immediate evidence
  revalidation are implemented for review. Behind/stale recommendations and
  reminders are implemented; the repository's existing GitHub App workflow
  remains the single owner of branch updates and their resulting CI reruns.
- **Phase 2:** collect outcomes from the implemented scoped unit/CLI/Chainsaw
  workflow and compare them with authoritative full-suite outcomes. Do not
  remove or de-require full checks until the false-negative evidence is
  strong enough for maintainer sign-off.
- **Phase 3:** collect shadow evidence for the implemented issue-label
  suggestions, then review the additive-only executor and label catalog before
  activating it. Validate reproduction output in manual trials. Before
  reproduction activation, maintainers must review the sandbox threat model,
  run adversarial egress/teardown drills, and decide whether reproducing
  trusted default-branch behavior is sufficient or version-selectable Kyverno
  installs are required.
- **Phase 4:** evaluate the implemented retrieval-only, citation-required
  docs index on a maintainer-curated question set. Add synthesis only after
  measuring citation correctness and escalation quality; connect Slack or
  Discussions write access only after a separate permission review. Review
  the dormant website App installation, supplied-content policy, DCO
  identity, Astro path contract, and rollback drill before separately
  enabling documentation drafts.
