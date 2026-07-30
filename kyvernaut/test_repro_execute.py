import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

from dependency_pr import load_config
from repro_execute import execute, main, sandbox_controls
from repro_plan import build_plan


ROOT = Path(__file__).parents[1]
CONFIG = load_config(ROOT / ".github/ai-maintainer.yaml")

MANIFESTS = """apiVersion: kyverno.io/v1
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


def authorized_bundle():
    issue = {
        "number": 9,
        "html_url": "https://github.com/kyverno/kyverno/issues/9",
        "body": (
            "### Steps to reproduce\n\n```yaml\n"
            + MANIFESTS
            + "\n```\n\n### Expected behavior\n\nThe Pod should be rejected."
        ),
        "labels": [{"name": "kyvernaut:repro-approved"}],
    }
    return build_plan(issue, active_config(), run_id="run-9")


class FakeRunner:
    def __init__(self, *, reject_pod=False, setup_failure=False, output="ok"):
        self.calls = []
        self.reject_pod = reject_pod
        self.setup_failure = setup_failure
        self.output = output

    def __call__(self, argv, stdin, timeout):
        self.calls.append({"argv": argv, "stdin": stdin, "timeout": timeout})
        is_setup = stdin and "kind: Namespace" in stdin
        is_pod = stdin and "kind: Pod" in stdin and "kind: Namespace" not in stdin
        returncode = 1 if (self.setup_failure and is_setup) or (self.reject_pod and is_pod) else 0
        return {
            "argv": argv,
            "returncode": returncode,
            "stdout": self.output,
            "stderr": "admission denied" if returncode else "",
            "timed_out": False,
        }


def test_executor_creates_controls_injects_namespace_and_captures_observations():
    plan, manifests = authorized_bundle()
    runner = FakeRunner(reject_pod=True)
    result, files = execute(
        plan,
        manifests,
        active_config(),
        runner=runner,
        sleeper=lambda _: None,
    )
    assert result["execution_performed"] is True
    assert result["outcome"] == "completed"
    assert result["expected_behavior"] == "The Pod should be rejected."
    assert result["actual_behavior"].startswith("1 manifest(s) accepted; 1 rejected")
    assert result["apply_results"][0]["kind"] == "ClusterPolicy"
    assert result["apply_results"][0]["namespace"] is None
    assert result["apply_results"][1]["kind"] == "Pod"
    assert result["apply_results"][1]["namespace"] == "kyvernaut-repro"
    pod_call = next(
        call for call in runner.calls if call["stdin"] and "kind: Pod" in call["stdin"]
    )
    assert "namespace: kyvernaut-repro" in pod_call["stdin"]
    assert "--namespace" in pod_call["argv"]
    assert {
        "events.txt",
        "kyverno-logs.txt",
        "kyverno-pods.txt",
        "policies.txt",
        "policy-reports.txt",
        "sandbox-resources.txt",
    } == set(files)


def test_generated_controls_enforce_namespace_budget_and_default_deny():
    controls = sandbox_controls(active_config()["issue_reproduction"])
    assert "kind: ResourceQuota" in controls
    assert "kind: LimitRange" in controls
    assert "kind: NetworkPolicy" in controls
    assert "policyTypes:" in controls
    assert "- Egress" in controls
    assert "automountServiceAccountToken" not in controls


def test_executor_refuses_tampered_or_replayed_plan():
    plan, manifests = authorized_bundle()
    with pytest.raises(ValueError, match="hash"):
        execute(
            plan,
            manifests + "\n# changed",
            active_config(),
            runner=FakeRunner(),
            sleeper=lambda _: None,
        )
    plan["execution_performed"] = True
    with pytest.raises(ValueError, match="unused authorized plan"):
        execute(
            plan,
            manifests,
            active_config(),
            runner=FakeRunner(),
            sleeper=lambda _: None,
        )


def test_executor_rechecks_active_repository_policy_and_sandbox_limits():
    plan, manifests = authorized_bundle()
    with pytest.raises(ValueError, match="does not authorize"):
        execute(
            plan,
            manifests,
            CONFIG,
            runner=FakeRunner(),
            sleeper=lambda _: None,
        )
    config = active_config()
    config["issue_reproduction"]["max_pods"] = 9
    with pytest.raises(ValueError, match="sandbox policy"):
        execute(
            plan,
            manifests,
            config,
            runner=FakeRunner(),
            sleeper=lambda _: None,
        )


def test_executor_stops_if_fixed_sandbox_controls_fail():
    plan, manifests = authorized_bundle()
    runner = FakeRunner(setup_failure=True)
    with pytest.raises(RuntimeError, match="sandbox namespace controls"):
        execute(
            plan,
            manifests,
            active_config(),
            runner=runner,
            sleeper=lambda _: None,
        )
    assert len(runner.calls) == 1


def test_executor_caps_captured_output():
    plan, manifests = authorized_bundle()
    config = active_config()
    config["issue_reproduction"]["max_output_bytes"] = 65536
    plan["sandbox"]["max_output_bytes"] = 65536
    runner = FakeRunner(output="x" * 70000)
    result, _ = execute(
        plan,
        manifests,
        config,
        runner=runner,
        sleeper=lambda _: None,
    )
    assert result["output_truncated"] is True
    captured = result["sandbox_setup"]["stdout"]
    assert len(captured.encode()) < 66000


def test_authorized_manifest_hash_is_exact():
    plan, manifests = authorized_bundle()
    assert hashlib.sha256(manifests.encode()).hexdigest() == plan["evidence"]["manifest_sha256"]


def test_initialize_only_writes_failure_placeholder(tmp_path, monkeypatch):
    plan, _ = authorized_bundle()
    plan_path = tmp_path / "plan.json"
    output_dir = tmp_path / "result"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "repro_execute.py",
            "--plan",
            str(plan_path),
            "--output-dir",
            str(output_dir),
            "--initialize-only",
        ],
    )
    assert main() == 0
    result = json.loads((output_dir / "repro-result.json").read_text(encoding="utf-8"))
    assert result["outcome"] == "infrastructure_failure"
    assert result["execution_performed"] is False
    assert result["decision_id"] == plan["decision_id"]
