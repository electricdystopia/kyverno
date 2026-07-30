from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/kyvernaut-pr-advisor.yaml"
HYGIENE_WORKFLOW = ROOT / ".github/workflows/kyvernaut-pr-hygiene.yaml"
ISSUE_WORKFLOW = ROOT / ".github/workflows/kyvernaut-issue-triage.yaml"
REPRO_WORKFLOW = ROOT / ".github/workflows/kyvernaut-repro-plan.yaml"
DEPENDENCY_MERGE_WORKFLOW = ROOT / ".github/workflows/kyvernaut-dependency-merge.yaml"
SCOPED_TEST_WORKFLOW = ROOT / ".github/workflows/kyvernaut-scoped-tests.yaml"
QA_WORKFLOW = ROOT / ".github/workflows/kyvernaut-docs-qa.yaml"
DOCS_DRAFT_WORKFLOW = ROOT / ".github/workflows/kyvernaut-docs-draft.yaml"


def test_advisor_workflow_has_minimal_permissions():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {
        "checks": "read",
        "contents": "read",
        "pull-requests": "write",
    }


def test_pull_request_target_only_executes_trusted_default_branch_code():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request_target:" in text
    assert "ref: ${{ github.event.repository.default_branch }}" in text
    assert "persist-credentials: false" in text
    assert "github.event.pull_request.head.ref" not in text
    assert "github.event.pull_request.head.repo" not in text
    assert "github.event.pull_request.head.sha }}" not in text


def test_kill_switch_guards_every_stateful_or_external_step():
    text = WORKFLOW.read_text(encoding="utf-8")
    guarded_steps = (
        "Checkout trusted default branch",
        "Set up Python",
        "Install runtime dependency",
        "Collect PR evidence without shell interpolation",
        "Generate scope, decision, and comment",
        "Upload immutable audit evidence",
    )
    for name in guarded_steps:
        start = text.index(f"- name: {name}")
        following = text[start : start + 260]
        assert "steps.pause.outputs.paused != 'true'" in following


def test_workflow_has_no_merge_push_or_approval_operation():
    text = WORKFLOW.read_text(encoding="utf-8")
    advisor = (ROOT / "kyvernaut/pr_shadow.py").read_text(encoding="utf-8")
    forbidden = (
        "contents: write",
        "pulls.merge",
        "createReview",
        "git push",
        "gh pr merge",
    )
    for fragment in forbidden:
        assert fragment not in text
    assert "issues.addLabels" not in text
    assert "pulls.addLabels" not in text
    assert "change-metadata-decision.json" in advisor


def test_write_token_workflow_uses_hash_locked_runtime_dependency():
    text = WORKFLOW.read_text(encoding="utf-8")
    requirements = (ROOT / "kyvernaut/requirements-runtime.txt").read_text(encoding="utf-8")
    assert "--require-hashes" in text
    assert "--only-binary=:all:" in text
    assert "requirements-runtime.txt" in text
    assert "PyYAML==6.0.3" in requirements
    assert "--hash=sha256:" in requirements


def test_hygiene_workflow_is_capped_scheduled_and_comment_only():
    workflow = yaml.safe_load(HYGIENE_WORKFLOW.read_text(encoding="utf-8"))
    text = HYGIENE_WORKFLOW.read_text(encoding="utf-8")
    assert workflow["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
    }
    assert "schedule:" in text
    assert "per_page: 50" in text
    assert "cooldownMs" in text
    assert "pulls.updateBranch" not in text
    assert "actions.reRunWorkflow" not in text
    assert "contents: write" not in text
    assert "--require-hashes" in text


def test_hygiene_kill_switch_guards_all_external_steps():
    text = HYGIENE_WORKFLOW.read_text(encoding="utf-8")
    guarded_steps = (
        "Checkout trusted default branch",
        "Set up Python",
        "Install hash-locked runtime dependency",
        "Collect oldest open pull requests",
        "Generate capped hygiene batch",
        "Upload immutable hygiene audit",
    )
    for name in guarded_steps:
        start = text.index(f"- name: {name}")
        following = text[start : start + 280]
        assert "steps.pause.outputs.paused != 'true'" in following


def test_issue_triage_workflow_has_one_additive_managed_label_surface():
    workflow = yaml.safe_load(ISSUE_WORKFLOW.read_text(encoding="utf-8"))
    text = ISSUE_WORKFLOW.read_text(encoding="utf-8")
    assert workflow["permissions"] == {"contents": "read", "issues": "write"}
    assert text.count("issues.addLabels") == 1
    assert "issues.setLabels" not in text
    assert "issues.removeLabel" not in text
    assert "issues.deleteComment" not in text
    assert "label_action_authorized == 'true'" in text
    assert "github.event.issue.body" not in text
    assert "persist-credentials: false" in text
    assert "--require-hashes" in text


def test_issue_triage_executor_revalidates_and_caps_exact_evidence():
    text = ISSUE_WORKFLOW.read_text(encoding="utf-8")
    executor = text.index("Revalidate and apply authorized managed labels")
    refetch = text.index("github.rest.issues.get", executor)
    evidence_check = text.index("const evidenceChanged", refetch)
    catalog_check = text.index("github.rest.issues.listLabelsForRepo", evidence_check)
    mutation = text.index("github.rest.issues.addLabels", catalog_check)
    assert executor < refetch < evidence_check < catalog_check < mutation
    assert 'decision.mode !== "active"' in text
    assert "!action.action_authorized" in text
    assert 'action.recommendation !== "apply"' in text
    assert "decision.blockers.length !== 0" in text
    assert "requested.length > action.max_labels_per_issue" in text
    assert "action.max_labels_per_issue > 10" in text
    assert 'issue.state !== "open"' in text
    assert "Boolean(issue.pull_request)" in text
    assert "bodyHash !== decision.evidence.body_sha256" in text
    assert "titleHash !== decision.evidence.title_sha256" in text
    assert "JSON.stringify(decision.evidence.existing_labels)" in text
    assert "issue-triage-execution.json" in text
    assert "always() && steps.pause.outputs.paused != 'true'" in text
    assert text.index("Upload immutable triage audit") < text.index(
        "Fail after preserving label API errors"
    )


def test_issue_triage_kill_switch_guards_every_external_step():
    text = ISSUE_WORKFLOW.read_text(encoding="utf-8")
    guarded_steps = (
        "Checkout trusted default branch",
        "Set up Python",
        "Install hash-locked runtime dependency",
        "Generate triage decision",
        "Revalidate and apply authorized managed labels",
        "Create or update triage comment",
        "Upload immutable triage audit",
    )
    for name in guarded_steps:
        start = text.index(f"- name: {name}")
        following = text[start : start + 280]
        assert "steps.pause.outputs.paused != 'true'" in following


def test_docs_qa_workflow_is_manual_read_only_and_citation_only():
    workflow = yaml.safe_load(QA_WORKFLOW.read_text(encoding="utf-8"))
    text = QA_WORKFLOW.read_text(encoding="utf-8")
    assert workflow["permissions"] == {"contents": "read"}
    assert "workflow_dispatch:" in text
    assert "issues:" not in text
    assert "discussions:" not in text
    assert "pull-requests:" not in text
    assert "github.rest." not in text
    assert "secrets.GITHUB_TOKEN" not in text
    assert "github.ref_name == github.event.repository.default_branch" in text
    assert "ref: ${{ github.sha }}" in text
    assert "persist-credentials: false" in text
    assert "KYVERNAUT_QUESTION: ${{ inputs.question }}" in text
    assert "--question-env KYVERNAUT_QUESTION" in text
    assert '--question "${{ inputs.question }}"' not in text
    assert "echo ${{ inputs.question }}" not in text
    assert "--require-hashes" in text
    assert "Upload immutable retrieval audit" in text


def test_docs_qa_kill_switch_guards_every_external_step():
    text = QA_WORKFLOW.read_text(encoding="utf-8")
    guarded_steps = (
        "Checkout exact trusted default-branch commit",
        "Set up Python",
        "Install hash-locked runtime dependency",
        "Retrieve trusted documentation as data",
        "Upload immutable retrieval audit",
    )
    for name in guarded_steps:
        start = text.index(f"- name: {name}")
        following = text[start : start + 300]
        assert "steps.pause.outputs.paused != 'true'" in following


def test_docs_draft_is_manual_dormant_and_uses_read_only_workflow_token():
    workflow = yaml.safe_load(DOCS_DRAFT_WORKFLOW.read_text(encoding="utf-8"))
    text = DOCS_DRAFT_WORKFLOW.read_text(encoding="utf-8")
    config = yaml.safe_load((ROOT / ".github/ai-maintainer.yaml").read_text(encoding="utf-8"))
    assert workflow["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
    }
    assert "workflow_dispatch:" in text
    assert "pull_request_target:" not in text
    assert "schedule:" not in text
    assert "github.ref_name == github.event.repository.default_branch" in text
    assert config["mode"] == "shadow"
    assert config["documentation"]["draft_pull_requests"]["enabled"] is False
    assert workflow["jobs"]["draft"]["environment"] == "kyvernaut-website"
    assert "ref: ${{ github.sha }}" in text
    assert "persist-credentials: false" in text
    assert "--require-hashes" in text
    assert "--only-binary=:all:" in text
    assert "WEBSITE_CONTENT_INPUT: ${{ inputs.website_content }}" in text
    assert "--content-env WEBSITE_CONTENT_INPUT" in text
    assert '--content "${{ inputs.website_content }}"' not in text


def test_docs_draft_app_token_is_scoped_to_one_repository_and_two_permissions():
    text = DOCS_DRAFT_WORKFLOW.read_text(encoding="utf-8")
    token = text.index("Create website-scoped GitHub App token")
    executor = text.index("Revalidate target and create one draft pull request")
    token_block = text[token:executor]
    assert "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1" in token_block
    assert "owner: kyverno" in token_block
    assert "repositories: website" in token_block
    assert "permission-contents: write" in token_block
    assert "permission-pull-requests: write" in token_block
    assert "permission-issues:" not in token_block
    assert "skip-token-revoke" not in token_block
    assert "github-token: ${{ steps.app-token.outputs.token }}" in text[executor:]
    assert text.count("steps.app-token.outputs.token") == 1


def test_docs_draft_revalidates_both_repositories_before_bounded_mutation():
    text = DOCS_DRAFT_WORKFLOW.read_text(encoding="utf-8")
    plan = text.index("Compile documentation draft decision")
    source_revalidate = text.index(
        "Revalidate source and website immediately before mutation"
    )
    token = text.index("Create website-scoped GitHub App token")
    executor = text.index("Revalidate target and create one draft pull request")
    target_refetch = text.index(
        "const base = await github.rest.git.getRef", executor
    )
    branch_create = text.index("github.rest.git.createRef", target_refetch)
    file_write = text.index(
        "github.rest.repos.createOrUpdateFileContents", branch_create
    )
    pr_create = text.index("github.rest.pulls.create", file_write)
    audit = text.index("Upload immutable documentation draft audit")
    assert plan < source_revalidate < token < executor
    assert executor < target_refetch < branch_create < file_write < pr_create < audit
    assert text.count("github.rest.git.createRef") == 1
    assert text.count("github.rest.repos.createOrUpdateFileContents") == 1
    assert text.count("github.rest.pulls.create") == 1
    assert "draft: true" in text[pr_create : pr_create + 500]
    assert "github.rest.pulls.merge" not in text
    assert "git push" not in text
    assert "pulls.update" not in text
    assert "issues." not in text
    assert "website base changed immediately before mutation" in text
    assert "website target changed immediately before mutation" in text
    assert "source PR evidence changed after planning" in text
    assert "dispatcher no longer has write-level permission" in text
    assert 'state: "all"' in text[executor:]
    assert "already used by a closed pull request" in text[executor:]


def test_docs_draft_kill_switch_guards_every_external_step():
    text = DOCS_DRAFT_WORKFLOW.read_text(encoding="utf-8")
    guarded_steps = (
        "Checkout exact trusted default-branch commit",
        "Set up planner Python",
        "Install hash-locked planner dependency",
        "Collect immutable source and website evidence",
        "Compile documentation draft decision",
        "Revalidate source and website immediately before mutation",
        "Create website-scoped GitHub App token",
        "Revalidate target and create one draft pull request",
        "Upload immutable documentation draft audit",
    )
    for name in guarded_steps:
        start = text.index(f"- name: {name}")
        following = text[start : start + 420]
        assert "steps.pause.outputs.paused != 'true'" in following


def test_reproduction_workflow_separates_plan_sandbox_and_report_permissions():
    workflow = yaml.safe_load(REPRO_WORKFLOW.read_text(encoding="utf-8"))
    text = REPRO_WORKFLOW.read_text(encoding="utf-8")
    assert workflow["permissions"] == {}
    assert workflow["jobs"]["plan"]["permissions"] == {
        "contents": "read",
        "issues": "read",
    }
    assert workflow["jobs"]["execute"]["permissions"] == {
        "actions": "read",
        "contents": "read",
        "issues": "read",
    }
    assert workflow["jobs"]["report"]["permissions"] == {
        "actions": "read",
        "issues": "write",
    }
    assert "issues:" in text
    assert "workflow_dispatch:" in text
    assert "github.event.label.name == 'kyvernaut:repro-approved'" in text
    assert "needs.plan.outputs.execution_authorized == 'true'" in text
    assert "kindest/node:v1.35.1" in text
    assert "make kind-load-all" in text
    assert "make kind-install-kyverno" in text
    assert "python kyvernaut/repro_execute.py" in text
    assert "--initialize-only" in text
    assert "Refetch issue immediately before execution" in text
    assert "Revalidate approval and exact manifest bundle" in text
    assert "cmp --silent" in text
    assert "docker run" not in text
    assert "secrets.GITHUB_TOKEN" not in text
    assert 'GITHUB_TOKEN: ""' in text
    assert "ref: ${{ github.sha }}" in text
    assert "github.ref_name != github.event.repository.default_branch" in text
    assert "persist-credentials: false" in text
    assert "--require-hashes" in text


def test_reproduction_sandbox_denies_egress_and_always_tears_down():
    text = REPRO_WORKFLOW.read_text(encoding="utf-8")
    executor = (ROOT / "kyvernaut/repro_execute.py").read_text(encoding="utf-8")
    config = yaml.safe_load((ROOT / ".github/ai-maintainer.yaml").read_text(encoding="utf-8"))
    assert config["mode"] == "shadow"
    assert config["issue_reproduction"]["enabled"] is False
    assert "DOCKER-USER" in text
    assert "! -d \"$subnet\" -j REJECT" in text
    assert "curl --silent --show-error --fail --max-time 5 https://example.com" in text
    assert "if: always() && steps.pause.outputs.paused != 'true'" in text
    assert "kind delete cluster --name \"$KIND_NAME\"" in text
    assert "-D DOCKER-USER" in text
    assert '"kind": "ResourceQuota"' in executor
    assert '"kind": "LimitRange"' in executor
    assert '"kind": "NetworkPolicy"' in executor
    assert "shell=True" not in executor


def test_reproduction_kill_switch_guards_every_external_step():
    text = REPRO_WORKFLOW.read_text(encoding="utf-8")
    guarded_steps = (
        "Checkout trusted workflow commit",
        "Set up Python",
        "Install hash-locked runtime dependency",
        "Fetch issue as JSON data",
        "Build sanitized execution plan",
        "Upload immutable reproduction plan",
        "Checkout exact trusted workflow commit",
        "Download authorized plan",
        "Set up sandbox Python",
        "Install sandbox hash-locked runtime dependency",
        "Initialize reproduction result audit",
        "Install trusted build tools",
        "Install trusted cluster tools",
        "Create ephemeral KinD cluster",
        "Build and install trusted default-branch Kyverno",
        "Preload allowlisted workload images",
        "Deny cluster network egress",
        "Refetch issue immediately before execution",
        "Revalidate approval and exact manifest bundle",
        "Apply sanitized manifests and capture observations",
        "Download reproduction result",
        "Create or update audited issue comment",
    )
    for name in guarded_steps:
        start = text.index(f"- name: {name}")
        following = text[start : start + 360]
        assert "steps.pause.outputs.paused != 'true'" in following


def test_dependency_executor_is_the_only_narrow_merge_surface():
    workflow = yaml.safe_load(DEPENDENCY_MERGE_WORKFLOW.read_text(encoding="utf-8"))
    text = DEPENDENCY_MERGE_WORKFLOW.read_text(encoding="utf-8")
    assert workflow["permissions"] == {
        "checks": "read",
        "contents": "write",
        "pull-requests": "write",
        "statuses": "read",
    }
    assert "schedule:" in text
    assert "pull_request_target:" not in text
    assert text.count("github.rest.pulls.merge") == 1
    assert "merge_method: batch.merge_method" in text
    assert "sha: action.head_sha" in text
    assert "git push" not in text
    assert "createReview" not in text
    assert "pulls.updateBranch" not in text
    assert "--require-hashes" in text
    assert "persist-credentials: false" in text

    other_workflows = [
        path
        for path in (ROOT / ".github/workflows").glob("kyvernaut-*.yaml")
        if path != DEPENDENCY_MERGE_WORKFLOW
    ]
    assert all("pulls.merge" not in path.read_text(encoding="utf-8") for path in other_workflows)


def test_dependency_executor_revalidates_before_head_bound_merge():
    text = DEPENDENCY_MERGE_WORKFLOW.read_text(encoding="utf-8")
    evidence_check = text.index("const evidenceChanged")
    current_checks = text.index(
        "github.rest.checks.listForRef",
        text.index("Revalidate evidence and execute authorized merges"),
    )
    final_snapshot = text.index("const finalResponse")
    merge = text.index("github.rest.pulls.merge")
    assert evidence_check < current_checks < final_snapshot < merge
    assert "pr.state !== \"open\"" in text
    assert "Boolean(pr.draft)" in text
    assert "pr.head.sha !== action.head_sha" in text
    assert "bodyHash !== action.expected.body_sha256" in text
    assert "pr.mergeable_state !== \"clean\"" in text
    assert "finalPr.head.sha !== action.head_sha" in text
    assert "JSON.stringify(finalLabels)" in text
    assert "batch.actions.length > batch.max_merges_per_run" in text
    assert "batch.max_merges_per_run > 10" in text


def test_dependency_executor_kill_switch_guards_every_external_step():
    text = DEPENDENCY_MERGE_WORKFLOW.read_text(encoding="utf-8")
    guarded_steps = (
        "Checkout trusted default branch",
        "Set up Python",
        "Install hash-locked runtime dependency",
        "Collect bounded dependency PR evidence",
        "Build capped merge batch",
        "Revalidate evidence and execute authorized merges",
    )
    for name in guarded_steps:
        start = text.index(f"- name: {name}")
        following = text[start : start + 320]
        assert "steps.pause.outputs.paused != 'true'" in following
    upload = text.index("- name: Upload immutable dependency audit")
    assert "steps.pause.outputs.paused != 'true'" in text[upload : upload + 220]


def test_scoped_ci_is_read_only_and_plans_from_exact_trusted_base():
    workflow = yaml.safe_load(SCOPED_TEST_WORKFLOW.read_text(encoding="utf-8"))
    text = SCOPED_TEST_WORKFLOW.read_text(encoding="utf-8")
    assert workflow["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
    }
    assert "pull_request_target:" not in text
    assert "ref: ${{ github.event.pull_request.base.sha }}" in text
    assert "path: trusted" in text
    assert "python trusted/kyvernaut/scoped_ci.py" in text
    assert "--config trusted/.github/ai-maintainer.yaml" in text
    assert "--profiles trusted/kyvernaut/conformance-profiles.yaml" in text
    assert "files.length !== context.payload.pull_request.changed_files" in text
    assert 'file.status === "renamed"' in text
    assert "file.previous_filename" in text
    assert "github.event.pull_request.head.ref" not in text
    assert "secrets." not in text
    assert "contents: write" not in text
    assert "pull-requests: write" not in text


def test_scoped_ci_executes_bounded_matrices_without_exposing_token_to_tests():
    text = SCOPED_TEST_WORKFLOW.read_text(encoding="utf-8")
    config = yaml.safe_load((ROOT / ".github/ai-maintainer.yaml").read_text(encoding="utf-8"))
    assert 1 <= config["scoped_ci"]["max_unit_jobs"] <= 20
    assert 1 <= config["scoped_ci"]["max_conformance_jobs"] <= 20
    assert "go test -race -count=1 \"$TEST_PACKAGE\"" in text
    assert "run: make test-cli" in text
    assert "uses: ./.github/actions/tests/conformance/run" in text
    assert "matrix: ${{ fromJSON(needs.plan.outputs.unit_matrix) }}" in text
    assert "matrix: ${{ fromJSON(needs.plan.outputs.conformance_matrix) }}" in text
    assert "token: disabled" in text
    assert "name: kyverno.tar" in text
    assert "kind delete cluster --name kind || true" in text
    assert "make codegen-all-code" not in text
    assert "make verify-codegen" not in text


def test_scoped_ci_kill_switch_guards_all_planning_external_steps():
    text = SCOPED_TEST_WORKFLOW.read_text(encoding="utf-8")
    guarded_steps = (
        "Checkout trusted base commit",
        "Set up planner Python",
        "Install hash-locked planner dependency",
        "Collect changed paths as JSON data",
        "Compile bounded execution matrices",
        "Upload immutable selection audit",
    )
    for name in guarded_steps:
        start = text.index(f"- name: {name}")
        following = text[start : start + 380]
        assert "steps.pause.outputs.paused != 'true'" in following
    assert "assistant_enabled=false" in text
    assert "'unit_matrix={\"include\":[]}'" in text
