#!/usr/bin/env python3
"""Execute an authorized Kyvernaut reproduction bundle in an existing KinD cluster.

Cluster creation, trusted Kyverno installation, egress isolation, and teardown
belong to the workflow. This runner creates fixed sandbox controls, applies
validated documents without a shell, and captures bounded observations.
"""

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import yaml

from dependency_pr import load_config
from repro_plan import UniqueKeyLoader, validate_document


NAMESPACED_KINDS = {
    "ConfigMap",
    "Deployment",
    "NamespacedDeletingPolicy",
    "NamespacedGeneratingPolicy",
    "NamespacedImageValidatingPolicy",
    "NamespacedMutatingPolicy",
    "NamespacedValidatingPolicy",
    "Pod",
    "Policy",
    "Service",
}


def sandbox_controls(policy: dict) -> str:
    namespace = policy["sandbox_namespace"]
    memory = policy["max_memory_mebibytes"]
    cpu = policy["max_cpu_millicores"]
    documents = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "kyvernaut",
                    "pod-security.kubernetes.io/enforce": "baseline",
                    "pod-security.kubernetes.io/audit": "restricted",
                    "pod-security.kubernetes.io/warn": "restricted",
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "kyvernaut-budget", "namespace": namespace},
            "spec": {
                "hard": {
                    "pods": str(policy["max_pods"]),
                    "requests.cpu": f"{cpu}m",
                    "limits.cpu": f"{cpu}m",
                    "requests.memory": f"{memory}Mi",
                    "limits.memory": f"{memory}Mi",
                    "configmaps": "20",
                    "services": "5",
                    "count/deployments.apps": "5",
                }
            },
        },
        {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": "kyvernaut-defaults", "namespace": namespace},
            "spec": {
                "limits": [
                    {
                        "type": "Container",
                        "defaultRequest": {"cpu": "50m", "memory": "64Mi"},
                        "default": {"cpu": "250m", "memory": "256Mi"},
                        "max": {"cpu": "500m", "memory": "512Mi"},
                    }
                ]
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "deny-all", "namespace": namespace},
            "spec": {
                "podSelector": {},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        },
    ]
    return yaml.safe_dump_all(documents, sort_keys=False, explicit_start=True)


def _default_runner(argv: list[str], stdin: str | None, timeout: float) -> dict:
    safe_environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "KUBECONFIG", "LANG", "LC_ALL", "PATH"}
    }
    try:
        completed = subprocess.run(
            argv,
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
            timeout=max(1, timeout),
            env=safe_environment,
        )
        return {
            "argv": argv,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as error:
        return {
            "argv": argv,
            "returncode": None,
            "stdout": error.stdout or "",
            "stderr": error.stderr or "",
            "timed_out": True,
        }


class OutputBudget:
    def __init__(self, maximum: int):
        self.remaining = maximum
        self.truncated = False

    def consume(self, value: object) -> str:
        text = value if isinstance(value, str) else str(value or "")
        encoded = text.encode()
        if len(encoded) <= self.remaining:
            self.remaining -= len(encoded)
            return text
        kept = encoded[: self.remaining].decode(errors="ignore")
        self.remaining = 0
        self.truncated = True
        return kept + "\n[output truncated by Kyvernaut]\n"


def _record(raw: dict, budget: OutputBudget) -> dict:
    return {
        "argv": raw["argv"],
        "returncode": raw["returncode"],
        "timed_out": bool(raw.get("timed_out")),
        "stdout": budget.consume(raw.get("stdout")),
        "stderr": budget.consume(raw.get("stderr")),
    }


def _documents(manifests: str) -> list[dict]:
    try:
        values = [
            document
            for document in yaml.load_all(manifests, Loader=UniqueKeyLoader)
            if document is not None
        ]
    except (yaml.YAMLError, ValueError, TypeError) as error:
        raise ValueError(f"sanitized manifest bundle is invalid: {error}") from error
    if not values or not all(isinstance(document, dict) for document in values):
        raise ValueError("sanitized manifest bundle must contain mapping documents")
    return values


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("reproduction exceeded its global runtime limit")
    return remaining


def execute(
    plan: dict,
    manifests: str,
    config: dict,
    *,
    runner: Callable[[list[str], str | None, float], dict] = _default_runner,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[dict, dict[str, str]]:
    policy = config["issue_reproduction"]
    if not config["enabled"] or config["mode"] != "active" or not policy["enabled"]:
        raise ValueError("repository policy does not authorize reproduction execution")
    if (
        not isinstance(plan, dict)
        or plan.get("schema_version") != 1
        or not plan.get("execution_authorized")
        or plan.get("execution_performed")
        or plan.get("errors")
    ):
        raise ValueError("reproduction plan is not an unused authorized plan")
    expected_sandbox = {
        "namespace": policy["sandbox_namespace"],
        "max_runtime_seconds": policy["max_runtime_seconds"],
        "max_output_bytes": policy["max_output_bytes"],
        "max_pods": policy["max_pods"],
        "max_cpu_millicores": policy["max_cpu_millicores"],
        "max_memory_mebibytes": policy["max_memory_mebibytes"],
        "network_egress": "deny",
        "repository_credentials_exposed_to_cluster": False,
    }
    if plan.get("sandbox") != expected_sandbox:
        raise ValueError("reproduction plan sandbox policy does not match trusted configuration")
    manifest_hash = hashlib.sha256(manifests.encode()).hexdigest()
    if manifest_hash != (plan.get("evidence") or {}).get("manifest_sha256"):
        raise ValueError("sanitized manifest hash does not match the authorized plan")

    documents = _documents(manifests)
    if len(documents) != plan["evidence"].get("document_count"):
        raise ValueError("sanitized manifest count does not match the authorized plan")
    validation_errors = []
    for index, document in enumerate(documents):
        validation_errors.extend(validate_document(document, policy, index))
    if validation_errors:
        raise ValueError("runtime manifest revalidation failed: " + "; ".join(validation_errors))

    started_at = time.time()
    deadline = time.monotonic() + policy["max_runtime_seconds"]
    budget = OutputBudget(policy["max_output_bytes"])
    namespace = policy["sandbox_namespace"]
    commands = {}
    controls = sandbox_controls(policy)
    setup_raw = runner(
        ["kubectl", "apply", "--server-side", "--field-manager=kyvernaut-repro", "-f", "-"],
        controls,
        min(30, _remaining(deadline)),
    )
    commands["sandbox_setup"] = _record(setup_raw, budget)
    if setup_raw["returncode"] != 0 or setup_raw.get("timed_out"):
        raise RuntimeError("failed to create sandbox namespace controls")

    apply_results = []
    for index, original in enumerate(documents):
        document = copy.deepcopy(original)
        kind = document["kind"]
        name = document["metadata"]["name"]
        namespaced = kind in NAMESPACED_KINDS
        if namespaced:
            document["metadata"]["namespace"] = namespace
        argv = [
            "kubectl",
            "apply",
            "--server-side",
            "--field-manager=kyvernaut-repro",
        ]
        if namespaced:
            argv.extend(["--namespace", namespace])
        argv.extend(["--filename", "-"])
        raw = runner(
            argv,
            yaml.safe_dump(document, sort_keys=False),
            min(30, _remaining(deadline)),
        )
        record = _record(raw, budget)
        apply_results.append(
            {
                "document_index": index,
                "api_version": document["apiVersion"],
                "kind": kind,
                "name": name,
                "namespace": namespace if namespaced else None,
                "accepted": raw["returncode"] == 0 and not raw.get("timed_out"),
                "command": record,
            }
        )

    sleeper(min(10, _remaining(deadline)))
    snapshot_commands = {
        "sandbox-resources.txt": [
            "kubectl",
            "get",
            "pods,deployments,services,configmaps",
            "--namespace",
            namespace,
            "--output",
            "yaml",
            "--ignore-not-found",
        ],
        "policies.txt": [
            "kubectl",
            "get",
            "clusterpolicies,policies",
            "--all-namespaces",
            "--output",
            "yaml",
            "--ignore-not-found",
        ],
        "policy-reports.txt": [
            "kubectl",
            "get",
            "policyreports,clusterpolicyreports",
            "--all-namespaces",
            "--output",
            "yaml",
            "--ignore-not-found",
        ],
        "events.txt": [
            "kubectl",
            "get",
            "events",
            "--all-namespaces",
            "--sort-by=.lastTimestamp",
        ],
        "kyverno-pods.txt": [
            "kubectl",
            "get",
            "pods",
            "--namespace",
            "kyverno",
            "--output",
            "wide",
        ],
        "kyverno-logs.txt": [
            "kubectl",
            "logs",
            "--namespace",
            "kyverno",
            "--selector",
            "app.kubernetes.io/instance=kyverno",
            "--all-containers=true",
            "--prefix=true",
            "--tail=500",
        ],
    }
    snapshots = {}
    files = {}
    for filename, argv in snapshot_commands.items():
        raw = runner(argv, None, min(30, _remaining(deadline)))
        record = _record(raw, budget)
        snapshots[filename] = {
            "argv": record["argv"],
            "returncode": record["returncode"],
            "timed_out": record["timed_out"],
        }
        files[filename] = record["stdout"] + (
            "\nSTDERR:\n" + record["stderr"] if record["stderr"] else ""
        )

    accepted = sum(item["accepted"] for item in apply_results)
    rejected = len(apply_results) - accepted
    finished_at = time.time()
    result = {
        "schema_version": 1,
        "decision_id": plan["decision_id"],
        "run_id": plan.get("run_id"),
        "execution_performed": True,
        "outcome": "completed",
        "expected_behavior": plan["evidence"].get("expected_behavior", ""),
        "actual_behavior": (
            f"{accepted} manifest(s) accepted; {rejected} rejected; "
            f"{len(snapshots)} diagnostic snapshot(s) captured."
        ),
        "started_at_unix": started_at,
        "finished_at_unix": finished_at,
        "duration_seconds": round(finished_at - started_at, 3),
        "sandbox": expected_sandbox,
        "sandbox_setup": commands["sandbox_setup"],
        "apply_results": apply_results,
        "snapshots": snapshots,
        "output_truncated": budget.truncated,
    }
    return result, files


def _write_failure(output_dir: Path, error: Exception, plan: dict | None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    failure = {
        "schema_version": 1,
        "decision_id": plan.get("decision_id") if isinstance(plan, dict) else None,
        "run_id": plan.get("run_id") if isinstance(plan, dict) else None,
        "execution_performed": False,
        "outcome": "infrastructure_failure",
        "error": str(error),
    }
    (output_dir / "repro-result.json").write_text(
        json.dumps(failure, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--manifests", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=root / ".github/ai-maintainer.yaml")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument(
        "--initialize-only",
        action="store_true",
        help="Write a placeholder infrastructure-failure audit before cluster setup",
    )
    args = parser.parse_args()
    plan = None
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        if args.initialize_only:
            _write_failure(
                args.output_dir,
                RuntimeError("execution infrastructure did not reach manifest application"),
                plan,
            )
            return 0
        if args.manifests is None:
            raise ValueError("--manifests is required unless --initialize-only is used")
        manifests = args.manifests.read_text(encoding="utf-8")
        config = load_config(args.config)
        result, files = execute(plan, manifests, config)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            (args.output_dir / filename).write_text(content, encoding="utf-8")
        (args.output_dir / "repro-result.json").write_text(
            json.dumps(result, indent=2) + "\n",
            encoding="utf-8",
        )
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write(f"outcome={result['outcome']}\n")
                stream.write(f"decision_id={result['decision_id']}\n")
    except (OSError, ValueError, RuntimeError, TimeoutError, json.JSONDecodeError) as error:
        _write_failure(args.output_dir, error, plan)
        print(f"reproduction execution failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
