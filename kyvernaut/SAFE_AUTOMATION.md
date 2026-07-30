# Kyvernaut safe automation boundaries

Kyvernaut starts with reversible, observable actions. A green test plan or
an AI classification is evidence, not authority. Repository policy,
GitHub permissions, branch protection, and maintainer controls remain the
authority for every action.

## Current operational mode

The checked-in configuration enables `shadow` mode. In that mode Kyvernaut
may:

- read pull-request metadata, changed paths, mergeability, and check results;
- calculate a scoped test plan and dependency-update recommendation;
- run bounded scoped unit, CLI, and supported Chainsaw tests with read-only
  permissions alongside authoritative CI;
- create or update one marker-owned advisory PR comment;
- create or update capped, cooldown-aware stale PR/reviewer reminders;
- classify new/edited issues, suggest existing template labels, and request
  missing reproduction fields in one marker-owned comment;
- upload the exact plan and decision JSON as a workflow artifact.

It cannot authorize a merge, push, approve, dismiss reviews, change labels,
edit issues, or write repository contents. The only issue mutation is its
own marker-owned comment.

One separately scheduled workflow,
`.github/workflows/kyvernaut-dependency-merge.yaml`, has narrowly scoped
`contents:write` and `pull-requests:write` permission. It is an implemented
but dormant executor: `mode: shadow` makes its action batch empty. Enabling
the executor requires a reviewed repository configuration change to
`mode: active`; editing a pull request cannot activate it.

The workflow uses `pull_request_target` solely to post comments on fork PRs.
Because that event has a write-capable token, it checks out and executes the
default branch only. Pull-request code, actions, requirements, and scripts
must never be executed by this workflow.

The separate scoped-test workflow uses the ordinary `pull_request` event and
is intentionally read-only. Its plan is compiled by code and metadata from
the exact base SHA, while test jobs check out the PR merge commit. No checkout
credential is persisted, no secret is referenced, and the Chainsaw runner
receives a non-secret sentinel rather than the job token.

## Path boundaries

These paths always require human review regardless of CI:

- `api/kyverno/`, `api/reports/`, and `api/policyreport/`;
- `pkg/cosign/`, `pkg/notary/`, `pkg/image/`, and `pkg/sigstoretuf/`;
- `pkg/cel/policies/ivpol/`;
- `.github/workflows/` and `.github/actions/`.

API and generated-code changes also require code-generation verification.
Generated files are never authoritative inputs: change their source and run
the repository generators.

The only current scope-level dependency merge candidate is a diff containing
only `go.mod` and/or `go.sum`. Even that signal is insufficient to merge:
the author, open/draft state, immutable head SHA, semantic update type,
breaking-change markers, CI state, mergeability, changed files, hold labels,
and kill switch must all pass the repository policy.

An unmatched path, ambiguous match, generic `pkg/` fallback, empty diff, or
unknown dependency version is a hard stop for autonomous action.

Repository structure is also explicit rather than inferred. The reviewed
module-boundary manifest must match every checked-in `go.mod`, the absence or
presence of `go.work`, the rationale document, and versioned root dependencies
on `github.com/kyverno/api` and `github.com/kyverno/sdk`. Adding or moving a
module without updating the reviewed decision fails validation.

Stable PR change metadata is derived from the complete changed-file list using
`change-metadata.yaml`. Documentation-only and generated-only require every
file to match reviewed patterns; API scope requires any `api/**` path. An API
diff is incomplete until a maintainer applies exactly one of
`change/breaking-api` or `change/non-breaking-api`. A declared inferred label
cannot override contradictory paths, unsafe/duplicate path evidence fails
closed, and the PR advisor retains the independent metadata decision.

For test execution, uncertain path coverage expands unit scope to `./...`
and marks authoritative full conformance as required. Matrices are
configuration-capped at 12 unit jobs and 8 KinD jobs. Every mapped
conformance root must have either a validated profile matching existing CI
or an explicit unsupported reason. `custom-sigstore`, full `**` selection,
and over-cap selections are never approximated with a misleading generic
green result. Scoped outcomes are shadow comparison evidence and do not
replace existing required unit, codegen, or conformance checks.

## Kill switch and disable controls

Set the repository Actions variable `KYVERNAUT_PAUSED` to `1`, `true`, `yes`,
or `on` (case-insensitive, surrounding whitespace ignored) to stop
Kyvernaut workflows before checkout, API reads, artifact upload, comments,
or merge execution.

Set `enabled: false` in `.github/ai-maintainer.yaml` to prevent new advisory
comments while retaining an auditable disabled decision if the evaluator is
run locally. Individual dependency PRs can be held with any configured hold
label, currently `hold`, `do-not-merge`, or `kyvernaut:hold`. The
`kyvernaut:no-nudge` label also suppresses PR hygiene reminders.

Scheduled hygiene examines at most the 50 oldest open PRs, emits at most the
configured number of reminders, and will not update the same marker-owned
reminder inside the configured cooldown. Behind-branch updates remain
recommendations only in Kyvernaut. The repository's pre-existing
`.github/workflows/pr-branch-updater.yml` owns branch updates using separate
GitHub App credentials; Kyvernaut deliberately does not race it. Updating a
branch naturally creates a new head SHA and causes the repository's PR CI to
run again.

Issue triage is deterministic and grounded in `.github/ISSUE_TEMPLATE`.
Issue title/body content is parsed as data, never inserted into a shell,
interpreted as agent instructions, or executed. Security-labeled issues are
excluded from public automation, and `kyvernaut:no-triage` is a maintainer
override. Shadow mode keeps suggested labels advisory. Active mode can only
add missing labels from the reviewed `managed_labels` catalog, with a
schema-bounded per-issue cap. It cannot remove or replace labels. The executor
re-fetches the issue and requires an exact match on open state, issue identity,
title/body hashes, and the complete existing label set before calling the
single additive-label API. Decisions and execution outcomes are separate audit
records.

Documentation Q&A is retrieval-only. `qa-sources.yaml` restricts grounding to
reviewed repository Markdown and caps question bytes, file count, file size,
section size, excerpts, and returned citations. The retriever uses no network,
model, issue, discussion, or chat content. It emits trusted passages only when
the configured term and weighted-coverage thresholds pass; otherwise it
generates no answer and requests human escalation. The manual workflow has
`contents: read`, accepts the question through an environment variable as
data, runs only on the default branch, and uploads an artifact without a Slack
or Discussions write token.
Reproduction starts when a maintainer applies the fixed
`kyvernaut:repro-approved` label or manually dispatches the workflow, and is
separately disabled in the checked-in configuration. Other label events
skip the planning job. Security-labeled issues are rejected even if they
also carry the approval label.

`kyvernaut-repro-plan.yaml` has three permission-separated jobs. Planning
fetches a maintainer-selected issue with read-only access, accepts only
explicit fenced YAML, applies byte/document limits and static policy, and
uploads a normalized bundle only when validation passes. Execution has no
issue-write permission. Reporting has no repository-content permission and
can only publish the bounded result.

The static policy rejects secrets, RBAC, CRDs, admission registrations,
nodes/namespaces/storage, jobs/daemonsets, host access, privileged/root
settings, added capabilities, service-account access, custom container
commands, explicit namespaces, external URLs, non-ClusterIP services,
unapproved images, non-preloaded pull policies, dangerous volume sources,
and nested generated blocked kinds. Workloads must explicitly disable
service-account token mounting. A maintainer approval label is necessary but
cannot override validation.

When all activation gates pass, the execution job:

1. checks out the exact trusted default-branch workflow commit and builds
   that revision's Kyverno images;
2. creates an ephemeral KinD cluster and installs Kyverno before processing
   issue content;
3. preloads the fixed workload-image allowlist;
4. creates a dedicated namespace with `ResourceQuota`, `LimitRange`,
   baseline Pod Security labels, and a default-deny `NetworkPolicy`;
5. adds a host firewall rule denying KinD-subnet egress and proves an HTTPS
   connection from the control-plane container fails;
6. refetches the issue and byte-compares its newly generated plan and
   sanitized manifest bundle with the authorized artifacts, catching removed
   approval labels or edited bodies;
7. blanks common repository/cloud credential variables, invokes `kubectl`
   only through fixed argv lists, enforces per-command and global timeouts,
   and caps captured output;
8. captures per-document API responses, resources, policy reports, events,
   pod state, and bounded Kyverno logs;
9. removes the firewall rule and deletes the cluster under `always()`.

No repository or cloud credential is mounted into the cluster. Kubernetes
service accounts and controller privileges exist only inside the disposable
cluster. The runner currently reproduces trusted default-branch Kyverno,
not arbitrary historical versions.

## Audit trail

Every evaluation has a deterministic `decision_id` derived from the run,
mode, evidence, and blockers. Workflow artifacts retain:

- `scope-plan.json`;
- `change-metadata-decision.json`;
- scoped-CI `selection.json` and its escaped human summary;
- `dependency-decision.json`;
- dependency executor `decision-batch.json` and, when actions were attempted,
  `execution.json`;
- `pr-hygiene-decision.json` or the scheduled `hygiene-decisions.json`;
- `issue-triage-decision.json` and, when label execution was attempted,
  `issue-triage-execution.json`;
- documentation retrieval `qa-result.json` and `answer.md`;
- reproduction `repro-plan.json`, exact sanitized bundle,
  `repro-result.json`, resource/policy/report/event snapshots, and bounded
  controller logs;
- the human-readable scope plan;
- the exact rendered comment.

The PR comment links to its workflow run and is updated in place using a
versioned marker. This avoids comment spam while GitHub retains edit and
workflow history. Dependency execution artifacts are retained for 90 days;
successful merges receive a marker-owned comment containing their decision
ID and run link.

## Dependency executor invariants

The executor:

1. scans no more than the 50 oldest open PRs and only collects evidence for
   the two supported dependency-bot identities;
2. emits at most `max_merges_per_run` actions (currently 3, schema-bounded to
   1–10) and uses squash merge only;
3. executes only `action_authorized` decisions produced in `active` mode;
4. binds every merge API request to the exact 40-character head SHA;
5. immediately before merging, refetches and compares state, draft status,
   author, base, title, body hash, labels, changed files, check runs, commit
   statuses, and mergeability;
6. fails closed if evidence changed, CI has no signals, any visible check or
   status is pending/failing, or GitHub no longer reports `clean`;
7. relies on protected-branch/ruleset enforcement as an independent final
   gate and has no direct-push implementation.

GitHub API rejection is recorded, not bypassed. A closed or already-merged
PR cannot be retried because it disappears from the open-PR scan, while the
head-SHA precondition prevents a stale authorization from merging a newer
commit.

## Gates before activating dependency merges

The executor is intentionally checked in before activation so its exact
permission and mutation surface can be reviewed. Maintainers must not switch
`mode` to `active` until they explicitly approve all of the following:

1. Risk tiers and always-review paths in `path-test-map.yaml`.
2. A shadow sample with meaningful observed CI failures and zero unexplained
   out-of-scope misses; a sample with no failures is inconclusive.
3. The visible-check/status aggregation and protected branch/ruleset
   configuration for `main`.
4. The ephemeral workflow token permissions, with protected branches still
   denying direct pushes.
5. Patch/minor parsing for every dependency ecosystem being enabled.
6. Rate limits, retry/idempotency behavior, hold labels, and kill-switch
   drills.
7. The rollback procedure below.
8. For issue reproduction: maintainer review and live drills of the
   implemented credential isolation, egress denial, CPU/memory/count/time
   quotas, static restrictions, guaranteed teardown, and adversarial tests.

Activation is a normal reviewed PR changing `mode: shadow` to `mode: active`.
To stop new work immediately, set `KYVERNAUT_PAUSED=true`; then return the
config to shadow mode in a reviewed PR. A merge that already completed is
not rewritten or reset: revert it through the repository's normal reviewed
PR process. A pre-activation drill should prove that the variable suppresses
the executor and that a hold label added between decision and execution
causes evidence revalidation to skip the PR.

PR documentation impact is also deterministic. Changes under configured
user-facing API, command, chart, engine, policy, controller, CEL, validation,
configuration, background, or webhook paths require one of: an in-repo
documentation change, a `kyverno/website` issue/PR link, or an explicit
reviewed exemption label. The advisor reports the requirement but has no
cross-repository token and cannot open a website PR.

The repository's existing `.github/workflows/check-codegen.yaml` remains the
authoritative codegen gate: it runs code and documentation generation plus
`make verify-codegen`. Kyvernaut does not duplicate or weaken that check.

Until those gates are met, the repository must remain in the checked-in
`shadow` mode.
