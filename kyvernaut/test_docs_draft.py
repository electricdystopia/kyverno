import copy
import json
from pathlib import Path

import pytest

from dependency_pr import load_config, validate_config
from docs_draft import evaluate_draft


ROOT = Path(__file__).parents[1]
CONFIG = load_config(ROOT / ".github/ai-maintainer.yaml")
HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40


def source_event(*, body="", labels=None, state="open", draft=False):
    return {
        "repository": {"full_name": "kyverno/kyverno"},
        "pull_request": {
            "number": 123,
            "html_url": "https://github.com/kyverno/kyverno/pull/123",
            "title": "feat: add a user-visible validation option",
            "body": body,
            "state": state,
            "draft": draft,
            "labels": [{"name": label} for label in (labels or [])],
            "base": {"ref": "main"},
            "head": {"sha": HEAD_SHA},
        },
    }


def target_snapshot(*, exists=False):
    return {
        "repository": "kyverno/website",
        "default_branch": "main",
        "base_sha": BASE_SHA,
        "file": {
            "exists": exists,
            "sha": "c" * 40 if exists else None,
            "content_sha256": "d" * 64 if exists else None,
        },
    }


CONTENT = """---
title: Validation option
excerpt: Configure the user-visible validation option.
---

This draft documents the new validation option.
"""


def active_config():
    config = copy.deepcopy(CONFIG)
    config["mode"] = "active"
    config["documentation"]["draft_pull_requests"]["enabled"] = True
    return config


def evaluate(
    *,
    config=None,
    event=None,
    files=None,
    snapshot=None,
    path="src/content/docs/docs/guides/validation-option.md",
    content=CONTENT,
    permission="write",
    environment=None,
):
    return evaluate_draft(
        event or source_event(),
        files or ["pkg/engine/validation.go"],
        snapshot or target_snapshot(),
        path,
        content,
        config or active_config(),
        actor_permission=permission,
        environment=environment,
        run_id="run-123",
    )


def blocker_codes(decision):
    return {item["code"] for item in decision["blockers"]}


def test_active_manual_request_authorizes_one_draft_pr():
    decision = evaluate()
    action = decision["action"]

    assert decision["blockers"] == []
    assert action["action_authorized"] is True
    assert action["target_repository"] == "kyverno/website"
    assert action["target_base_branch"] == "main"
    assert action["target_base_sha"] == BASE_SHA
    assert action["target_path"].endswith("validation-option.md")
    assert action["branch"].startswith("kyvernaut/docs-123-")
    assert action["draft"] is True
    assert "Signed-off-by: Kyvernaut" in action["commit_message"]
    assert "Source-Head: " + HEAD_SHA in action["commit_message"]
    assert decision["evidence"]["documentation_decision"]["status"] == "missing"
    assert decision["evidence"]["content"]["frontmatter_title"] == "Validation option"


def test_checked_in_shadow_and_disabled_policy_cannot_authorize():
    decision = evaluate(config=CONFIG)
    assert decision["action"]["action_authorized"] is False
    assert {"shadow_mode", "draft_workflow_disabled"} <= blocker_codes(decision)


@pytest.mark.parametrize("permission", ["read", "triage", "none", ""])
def test_non_writer_cannot_dispatch_cross_repository_write(permission):
    decision = evaluate(permission=permission)
    assert decision["action"]["action_authorized"] is False
    assert "dispatcher_not_authorized" in blocker_codes(decision)


def test_kill_switch_blocks_planned_write():
    decision = evaluate(environment={"KYVERNAUT_PAUSED": "true"})
    assert decision["action"]["action_authorized"] is False
    assert "kill_switch_active" in blocker_codes(decision)


@pytest.mark.parametrize(
    "path",
    [
        "../README.md",
        "/src/content/docs/docs/escape.md",
        "src/content/docs/docs/../escape.md",
        "src/content/docs/docs/guides/draft.mdx",
        "src/content/docs/docs/.hidden.md",
        "src/content/docs/docs//double.md",
        "README.md",
    ],
)
def test_target_path_must_be_normalized_markdown_below_reviewed_root(path):
    decision = evaluate(path=path)
    assert decision["action"]["action_authorized"] is False
    assert "invalid_website_path" in blocker_codes(decision)


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("", "empty_content"),
        ("# no frontmatter\n", "missing_frontmatter"),
        ("---\ntitle: missing close\n", "missing_frontmatter"),
        ("---\ntitle: ''\n---\n\nbody\n", "missing_title"),
        ("---\ntitle: broken: yaml\n---\n", "invalid_frontmatter"),
        ("---\ntitle: Empty body\n---\n", "empty_document_body"),
        ("---\ntitle: CRLF\r\n---\r\n", "noncanonical_content"),
        ("---\ntitle: no final newline\n---", "noncanonical_content"),
        ("---\ntitle: nul\n---\n\x00\n", "nul_in_content"),
    ],
)
def test_content_contract_fails_closed(content, code):
    decision = evaluate(content=content)
    assert decision["action"]["action_authorized"] is False
    assert code in blocker_codes(decision)


def test_content_size_is_bounded():
    policy = active_config()["documentation"]["draft_pull_requests"]
    content = "---\ntitle: Large\n---\n" + ("x" * policy["max_content_bytes"]) + "\n"
    decision = evaluate(content=content)
    assert "content_too_large" in blocker_codes(decision)


@pytest.mark.parametrize(
    "event",
    [
        source_event(state="closed"),
        source_event(draft=True),
    ],
)
def test_only_open_ready_source_pr_can_authorize(event):
    decision = evaluate(event=event)
    assert decision["action"]["action_authorized"] is False
    assert blocker_codes(decision) & {"source_pr_not_open", "source_pr_is_draft"}


def test_existing_documentation_or_exemption_prevents_duplicate_draft():
    linked = source_event(body="Docs: https://github.com/kyverno/website/pull/42")
    decision = evaluate(event=linked)
    assert "documentation_already_satisfied" in blocker_codes(decision)

    exempt = source_event(labels=["kyvernaut:docs-reviewed"])
    decision = evaluate(event=exempt)
    assert "documentation_already_satisfied" in blocker_codes(decision)


def test_non_user_facing_change_does_not_create_draft():
    decision = evaluate(files=["kyvernaut/README.md"])
    assert "documentation_not_required" in blocker_codes(decision)


def test_source_repository_is_fixed_by_reviewed_policy():
    event = source_event()
    event["repository"]["full_name"] = "attacker/kyverno"
    decision = evaluate(event=event)
    assert decision["action"]["action_authorized"] is False
    assert "source_repository_mismatch" in blocker_codes(decision)


def test_existing_target_file_identity_is_bound_into_plan():
    decision = evaluate(snapshot=target_snapshot(exists=True))
    action = decision["action"]
    assert action["action_authorized"] is True
    assert action["expected_target_file_sha"] == "c" * 40
    assert action["expected_target_content_sha256"] == "d" * 64


def test_duplicate_or_excessive_changed_file_evidence_fails_closed():
    duplicate = evaluate(files=["pkg/engine/a.go", "pkg/engine/a.go"])
    assert "duplicate_changed_files" in blocker_codes(duplicate)

    config = active_config()
    config["documentation"]["draft_pull_requests"]["max_changed_files"] = 1
    excessive = evaluate(config=config, files=["pkg/engine/a.go", "pkg/engine/b.go"])
    assert "too_many_changed_files" in blocker_codes(excessive)


def test_action_and_decision_are_deterministic():
    first = evaluate()
    second = evaluate()
    assert first == second


def test_audit_plan_retains_content_hash_not_supplied_document():
    decision = evaluate()
    serialized = json.dumps(decision)
    assert "This draft documents the new validation option." not in serialized
    assert decision["action"]["content_sha256"]
    assert decision["evidence"]["content"]["content_bytes"] == len(
        CONTENT.encode("utf-8")
    )


def test_config_rejects_cross_repository_policy_expansion():
    config = copy.deepcopy(CONFIG)
    drafts = config["documentation"]["draft_pull_requests"]
    drafts["target_base_branch"] = "release-1-18-0"
    drafts["allowed_extensions"] = [".md", ".mdx"]
    drafts["content_root"] = "../"
    drafts["max_content_bytes"] = 1000000
    config["documentation"]["source_repository"] = "attacker/kyverno"
    config["documentation"]["website_repository"] = "attacker/website"
    errors = validate_config(config)
    assert any("target_base_branch" in error for error in errors)
    assert any("allowed_extensions" in error for error in errors)
    assert any("content_root" in error for error in errors)
    assert any("max_content_bytes" in error for error in errors)
    assert any("source_repository" in error for error in errors)
    assert any("website_repository" in error for error in errors)
