from pathlib import Path

from dependency_pr import load_config
from docs_requirement import evaluate_docs


ROOT = Path(__file__).parents[1]
CONFIG = load_config(ROOT / ".github/ai-maintainer.yaml")


def event(body="", labels=None):
    return {
        "pull_request": {
            "body": body,
            "labels": [{"name": label} for label in (labels or [])],
        }
    }


def test_user_facing_engine_change_without_docs_is_missing():
    decision = evaluate_docs(event(), ["pkg/engine/validation.go"], CONFIG)
    assert decision["documentation_required"] is True
    assert decision["status"] == "missing"


def test_in_repo_docs_change_satisfies_requirement():
    decision = evaluate_docs(
        event(),
        ["pkg/webhooks/server.go", "docs/dev/webhooks.md"],
        CONFIG,
    )
    assert decision["status"] == "satisfied"
    assert decision["evidence"]["documentation_files"] == ["docs/dev/webhooks.md"]


def test_website_pr_or_issue_link_satisfies_requirement():
    body = "Docs: https://github.com/kyverno/website/pull/123"
    decision = evaluate_docs(event(body), ["api/kyverno/v1/types.go"], CONFIG)
    assert decision["status"] == "satisfied"
    assert decision["evidence"]["website_links"]


def test_dependency_only_change_does_not_require_docs():
    decision = evaluate_docs(event(), ["go.mod", "go.sum"], CONFIG)
    assert decision["status"] == "not_required"


def test_explicit_review_label_satisfies_requirement():
    decision = evaluate_docs(
        event(labels=["Kyvernaut:Docs-Reviewed"]),
        ["cmd/cli/kubectl-kyverno/main.go"],
        CONFIG,
    )
    assert decision["status"] == "satisfied"
    assert decision["evidence"]["exemption_labels"]


def test_similar_repository_link_does_not_satisfy_requirement():
    body = "https://github.com/attacker/website/pull/123"
    decision = evaluate_docs(event(body), ["charts/kyverno/values.yaml"], CONFIG)
    assert decision["status"] == "missing"
