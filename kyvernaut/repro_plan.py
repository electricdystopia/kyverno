#!/usr/bin/env python3
"""Build a sanitized reproduction plan from maintainer-approved issue YAML."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

from dependency_pr import load_config


YAML_FENCE = re.compile(r"```ya?ml[ \t]*\n(.*?)\n```", re.IGNORECASE | re.DOTALL)
BLOCKED_KINDS = {
    "APIService",
    "CertificateSigningRequest",
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "DaemonSet",
    "EndpointSlice",
    "IngressClass",
    "Job",
    "CronJob",
    "MutatingWebhookConfiguration",
    "Namespace",
    "NetworkPolicy",
    "Node",
    "PersistentVolume",
    "PersistentVolumeClaim",
    "PriorityClass",
    "Role",
    "RoleBinding",
    "Secret",
    "StorageClass",
    "ValidatingWebhookConfiguration",
}
FORBIDDEN_KEYS = {
    "csi",
    "ephemeralContainers",
    "gitRepo",
    "hostAliases",
    "hostIPC",
    "hostNetwork",
    "hostPID",
    "hostPath",
    "hostPort",
    "nodeName",
    "serviceAccount",
    "serviceAccountName",
    "imagePullSecrets",
    "imageRegistry",
    "namespace",
    "nfs",
    "persistentVolumeClaim",
    "procMount",
    "projected",
    "secret",
    "serviceAccountToken",
    "shareProcessNamespace",
    "sysctls",
    "windowsOptions",
}
SECTION_HEADING = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
WORKLOAD_KINDS = {"Pod", "Deployment"}


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def extract_documents(body: str, policy: dict) -> tuple[list[dict], list[str]]:
    errors = []
    body_bytes = len(body.encode())
    if body_bytes > policy["max_body_bytes"]:
        return [], [f"issue body exceeds {policy['max_body_bytes']} byte limit"]
    fences = YAML_FENCE.findall(body)
    if not fences:
        return [], ["no fenced ```yaml reproduction manifests found"]
    manifest_text = "\n---\n".join(fences)
    if len(manifest_text.encode()) > policy["max_manifest_bytes"]:
        return [], [f"YAML manifests exceed {policy['max_manifest_bytes']} byte limit"]
    documents = []
    try:
        for document in yaml.load_all(manifest_text, Loader=UniqueKeyLoader):
            if document is None:
                continue
            if not isinstance(document, dict):
                errors.append("each YAML document must be a mapping")
                continue
            documents.append(document)
    except (yaml.YAMLError, ValueError, TypeError) as error:
        return [], [f"invalid YAML: {error}"]
    if len(documents) > policy["max_documents"]:
        errors.append(f"manifest count exceeds {policy['max_documents']} document limit")
    return documents, errors


def _walk(value, path="$"):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _section(body: str, name: str) -> str:
    matches = list(SECTION_HEADING.finditer(body))
    wanted = name.casefold()
    for index, match in enumerate(matches):
        if match.group(1).strip().casefold() != wanted:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        return body[match.end() : end].strip()
    return ""


def _workload_spec(document: dict) -> dict | None:
    kind = document.get("kind")
    spec = document.get("spec")
    if not isinstance(spec, dict):
        return None
    if kind == "Pod":
        return spec
    if kind == "Deployment":
        template = spec.get("template")
        if isinstance(template, dict) and isinstance(template.get("spec"), dict):
            return template["spec"]
    return None


def validate_document(document: dict, policy: dict, index: int) -> list[str]:
    prefix = f"document[{index}]"
    errors = []
    api_version = document.get("apiVersion")
    kind = document.get("kind")
    metadata = document.get("metadata")
    if not isinstance(api_version, str) or not api_version:
        errors.append(f"{prefix}: apiVersion is required")
    if not isinstance(kind, str) or not kind:
        errors.append(f"{prefix}: kind is required")
        return errors
    if kind in BLOCKED_KINDS:
        errors.append(f"{prefix}: kind {kind!r} is blocked")
    elif kind not in policy["allowed_kinds"]:
        errors.append(f"{prefix}: kind {kind!r} is not allowlisted")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
        errors.append(f"{prefix}: metadata.name is required")

    workload_spec = _workload_spec(document)
    if kind in WORKLOAD_KINDS and workload_spec is None:
        errors.append(f"{prefix}: workload pod spec is required")
    if workload_spec is not None:
        if workload_spec.get("automountServiceAccountToken") is not False:
            errors.append(
                f"{prefix}: workload must set automountServiceAccountToken: false"
            )
        containers = workload_spec.get("containers")
        if not isinstance(containers, list) or not containers:
            errors.append(f"{prefix}: workload must contain at least one container")
        elif len(containers) > 4:
            errors.append(f"{prefix}: workload exceeds the 4-container limit")
        if isinstance(containers, list):
            for container_index, container in enumerate(containers):
                if not isinstance(container, dict):
                    errors.append(
                        f"{prefix}: container[{container_index}] must be a mapping"
                    )
                    continue
                if container.get("imagePullPolicy") not in {"IfNotPresent", "Never"}:
                    errors.append(
                        f"{prefix}: container[{container_index}] must set imagePullPolicy "
                        "to IfNotPresent or Never"
                    )
    if kind == "Deployment" and isinstance(document.get("spec"), dict):
        replicas = document["spec"].get("replicas", 1)
        if (
            not isinstance(replicas, int)
            or isinstance(replicas, bool)
            or not 0 <= replicas <= policy["max_pods"]
        ):
            errors.append(
                f"{prefix}: replicas must be an integer from 0 through {policy['max_pods']}"
            )

    for path, value in _walk(document):
        key = path.rsplit(".", 1)[-1]
        if key == "kind" and path != "$.kind" and value in BLOCKED_KINDS:
            errors.append(f"{prefix}: nested blocked kind {value!r} at {path}")
        if key in FORBIDDEN_KEYS:
            errors.append(f"{prefix}: forbidden field {path}")
        if key in {"privileged", "allowPrivilegeEscalation", "automountServiceAccountToken"} and value is True:
            errors.append(f"{prefix}: forbidden true field {path}")
        if key == "runAsUser" and value == 0:
            errors.append(f"{prefix}: root runAsUser at {path}")
        if key == "add" and isinstance(value, list) and value:
            errors.append(f"{prefix}: added Linux capabilities at {path}")
        if key in {"command", "args"} and isinstance(value, list) and value:
            errors.append(f"{prefix}: custom container command/args at {path}")
        if key == "imagePullPolicy" and value not in {"IfNotPresent", "Never"}:
            errors.append(f"{prefix}: unsafe imagePullPolicy {value!r} at {path}")
        if key == "seccompProfile" and isinstance(value, dict) and value.get("type") == "Unconfined":
            errors.append(f"{prefix}: unconfined seccomp profile at {path}")
        if key in {"url", "urlPath"} and isinstance(value, str) and re.match(r"https?://", value):
            errors.append(f"{prefix}: external URL at {path}")

    spec = document.get("spec")
    if kind == "Service" and isinstance(spec, dict):
        service_type = spec.get("type", "ClusterIP")
        if service_type not in {"ClusterIP"}:
            errors.append(f"{prefix}: Service type {service_type!r} is blocked")

    allowed_images = set(policy["allowed_images"])
    for path, value in _walk(document):
        if path.endswith(".image") and isinstance(value, str) and value not in allowed_images:
            errors.append(f"{prefix}: container image {value!r} is not allowlisted")

    return errors


def build_plan(issue: dict, config: dict, *, run_id: str | None = None) -> tuple[dict, str]:
    policy = config["issue_reproduction"]
    body = issue.get("body") if isinstance(issue.get("body"), str) else ""
    expected_behavior = _section(body, "Expected behavior")
    if len(expected_behavior.encode()) > 4096:
        expected_behavior = expected_behavior.encode()[:4096].decode(errors="ignore")
    labels = [
        label.get("name") if isinstance(label, dict) else label
        for label in issue.get("labels", [])
    ]
    labels = [label for label in labels if isinstance(label, str)]
    documents, validation_errors = extract_documents(body, policy)
    for index, document in enumerate(documents):
        validation_errors.extend(validate_document(document, policy, index))
    required = {label.casefold() for label in policy["approval_labels"]}
    present = {label.casefold() for label in labels}
    approved = bool(required & present)
    errors = list(validation_errors)
    excluded = {
        label.casefold() for label in config["issue_triage"]["excluded_labels"]
    }
    excluded_present = sorted(label for label in labels if label.casefold() in excluded)
    if excluded_present:
        errors.append(
            "issue has automation-excluded label(s): " + ", ".join(excluded_present)
        )
    if not approved:
        errors.append("maintainer reproduction approval label is missing")

    normalized = yaml.safe_dump_all(documents, sort_keys=False, explicit_start=True)
    # Never emit an apply-ready bundle when static validation failed. The
    # audit retains a hash and detailed errors without leaving dangerous YAML
    # beside the proposed kubectl command.
    sanitized = normalized if not validation_errors else ""
    global_active = config["enabled"] and config["mode"] == "active"
    execution_authorized = bool(
        global_active and policy["enabled"] and approved and documents and not errors
    )
    evidence = {
        "issue_number": issue.get("number"),
        "issue_url": issue.get("html_url"),
        "labels": labels,
        "approved": approved,
        "excluded_labels_present": excluded_present,
        "document_count": len(documents),
        "manifest_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
        "sanitized_bundle_emitted": bool(sanitized),
        "expected_behavior": expected_behavior,
    }
    material = {
        "run_id": run_id,
        "mode": config["mode"],
        "reproduction_enabled": policy["enabled"],
        "evidence": evidence,
        "errors": errors,
    }
    decision_id = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    plan = {
        "schema_version": 1,
        "decision_id": decision_id,
        "run_id": run_id,
        "mode": config["mode"],
        "reproduction_enabled": policy["enabled"],
        "execution_authorized": execution_authorized,
        "execution_performed": False,
        "errors": errors,
        "evidence": evidence,
        "sandbox": {
            "namespace": policy["sandbox_namespace"],
            "max_runtime_seconds": policy["max_runtime_seconds"],
            "max_output_bytes": policy["max_output_bytes"],
            "max_pods": policy["max_pods"],
            "max_cpu_millicores": policy["max_cpu_millicores"],
            "max_memory_mebibytes": policy["max_memory_mebibytes"],
            "network_egress": "deny",
            "repository_credentials_exposed_to_cluster": False,
        },
        "proposed_steps": [
            {
                "action": "create ephemeral KinD cluster",
                "cluster_name": "kyvernaut-repro",
            },
            {"action": "build and install the exact trusted workflow commit"},
            {"action": "preload the configured workload-image allowlist"},
            {"argv": ["kubectl", "apply", "-f", "generated-sandbox-controls.yaml"]},
            {"action": "deny KinD container egress before applying issue manifests"},
            {"action": "refetch and byte-compare issue approval and manifests"},
            {
                "argv": [
                    "python",
                    "kyvernaut/repro_execute.py",
                    "--plan",
                    "repro-plan.json",
                    "--manifests",
                    "sanitized-manifests.yaml",
                ]
            },
            {"argv": ["kind", "delete", "cluster", "--name", "kyvernaut-repro"]},
        ],
    }
    return plan, sanitized


def main() -> int:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=root / ".github/ai-maintainer.yaml")
    parser.add_argument("--run-id")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        issue = json.loads(args.issue.read_text(encoding="utf-8"))
        config = load_config(args.config)
        plan, manifests = build_plan(issue, config, run_id=args.run_id)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "repro-plan.json").write_text(
            json.dumps(plan, indent=2) + "\n", encoding="utf-8"
        )
        (args.output_dir / "sanitized-manifests.yaml").write_text(
            manifests, encoding="utf-8"
        )
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write(f"execution_authorized={str(plan['execution_authorized']).lower()}\n")
                stream.write(f"decision_id={plan['decision_id']}\n")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"reproduction planning failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
