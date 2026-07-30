# Kyvernaut requirement status

This matrix tracks the original project brief against executable repository
evidence. “Implemented” means code and local guardrail tests exist; it does not
mean a dormant active-mode action has maintainer approval or production shadow
history. The checked-in policy intentionally remains `shadow`.

## Required guardrails and repository foundations

| Requirement | Status | Authoritative evidence |
|---|---|---|
| Least privilege, trusted-code execution, no direct protected-branch push | Implemented | Per-workflow permissions and checkout boundaries; `test_workflow_guardrails.py`; `SAFE_AUTOMATION.md` |
| CI/policy gates before dependency merges | Implemented, dormant | `dependency_pr.py`, `dependency_batch.py`, SHA-bound executor workflow |
| Deterministic decision/action audit trail | Implemented | JSON artifacts and marker-owned comments described in `SAFE_AUTOMATION.md` |
| Repository kill switch, per-action overrides, and caps | Implemented | `.github/ai-maintainer.yaml`, workflow pause steps, evaluator tests |
| Module/monorepo evaluation | Implemented | `MODULE_BOUNDARIES.md`, `module-boundaries.yaml`, drift validator |
| Deep root and high-traffic `AGENTS.md` guidance | Implemented | Root plus `pkg/engine`, `pkg/webhooks`, `pkg/controllers`, and conformance files |
| Machine-readable task index | Implemented | `task-index.yaml` and repository validator |
| Explicit autonomous-edit boundaries | Implemented | `SAFE_AUTOMATION.md`, path risk/human-review metadata |
| Path-to-test metadata | Implemented, shadow comparison | `path-test-map.yaml`, `conformance-profiles.yaml`, scoped workflow |
| Structured PR metadata | Implemented | Central labels, PR template, `change-metadata.yaml`, advisor artifact |

## Maintainer workflows

| Requirement | Status | Authoritative evidence / remaining work |
|---|---|---|
| Dependency PR review, major-bump flagging, safe patch/minor merge | Implemented, dormant | Advisor plus capped active-mode executor; needs maintainer shadow evidence and activation review |
| Behind-branch update and CI retrigger | Existing repository owner, not duplicated | `.github/workflows/pr-branch-updater.yml` uses the dedicated GitHub App; Kyvernaut only advises to avoid racing it |
| Stale contributor/reviewer nudges | Implemented | Scheduled capped/cooldown-aware hygiene workflow |
| Diff-to-unit/CLI/conformance selection | Implemented, shadow comparison | Trusted-base planner and capped executable matrices; full CI remains authoritative |
| Issue classification, missing-info request, managed labels | Implemented; labels dormant in shadow | Issue evaluator, additive-only revalidating label executor |
| Isolated issue reproduction | Implemented, separately disabled | Static planner, three permission-separated jobs, KinD sandbox, egress/quota/teardown controls |
| API codegen/verify gate | Existing authoritative workflow | `.github/workflows/check-codegen.yaml` runs generation and verification; Kyvernaut flags when required |
| Documentation-impact identification | Implemented | `docs_requirement.py` and PR advisor evidence |
| Draft documentation PR | Implemented, dormant | Manual supplied-content planner plus revalidating `kyverno/website` App executor; needs App/ruleset/DCO/path-contract review and activation drills |
| Slack/Discussions grounded Q&A | Retrieval foundation only (stretch) | Repository-only cited retrieval and escalation exist; no Slack/Discussions webhook, relevant issue/PR retrieval, synthesis, or write token |

## Evidence commands

```bash
python3 -m pytest kyvernaut -q
python3 kyvernaut/scope_tests.py --repo . --validate
python3 kyvernaut/scoped_ci.py --repo . --validate
python3 kyvernaut/issue_triage.py --repo . --validate-labels
python3 kyvernaut/qa_retrieval.py --repo . --validate
python3 kyvernaut/module_boundaries.py --repo . --validate
python3 kyvernaut/change_metadata.py --repo . --validate
python3 kyvernaut/task_index.py --repo . --validate
```

Production activation additionally requires the live shadow samples,
ruleset/App-permission review, and rollback drills listed in
`SAFE_AUTOMATION.md`; local tests cannot prove those external conditions.
