#!/usr/bin/env python3
"""Determine whether a PR has satisfied Kyverno's documentation process."""

import hashlib
import json
import re


def _path_matches(path: str, prefix: str) -> bool:
    if prefix.endswith("/"):
        return path.startswith(prefix)
    return path == prefix or path.startswith(prefix + "/")


def _labels(pr: dict) -> list[str]:
    labels = []
    for value in pr.get("labels", []):
        name = value.get("name") if isinstance(value, dict) else value
        if isinstance(name, str):
            labels.append(name)
    return labels


def evaluate_docs(
    event: dict,
    changed_files: list[str],
    config: dict,
    *,
    run_id: str | None = None,
) -> dict:
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        raise ValueError("event must contain a pull_request object")
    policy = config["documentation"]
    labels = _labels(pr)
    normalized_labels = {label.casefold() for label in labels}
    affected = sorted(
        path
        for path in changed_files
        if any(_path_matches(path, prefix) for prefix in policy["user_facing_paths"])
    )
    docs_files = sorted(
        path
        for path in changed_files
        if any(_path_matches(path, prefix) for prefix in policy["documentation_paths"])
    )
    body = pr.get("body") if isinstance(pr.get("body"), str) else ""
    website_repository = re.escape(policy["website_repository"])
    links = sorted(
        set(
            re.findall(
                rf"https://github\.com/{website_repository}/(?:pull|issues)/\d+",
                body,
                flags=re.IGNORECASE,
            )
        )
    )
    exemptions = sorted(
        label
        for label in labels
        if label.casefold() in {value.casefold() for value in policy["exempt_labels"]}
    )
    required = bool(policy["enabled"] and affected)
    satisfied = bool(not required or docs_files or links or exemptions)
    status = "not_required" if not required else "satisfied" if satisfied else "missing"
    evidence = {
        "affected_user_facing_files": affected,
        "documentation_files": docs_files,
        "website_links": links,
        "exemption_labels": exemptions,
    }
    material = {
        "run_id": run_id,
        "status": status,
        "evidence": evidence,
    }
    decision_id = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return {
        "schema_version": 1,
        "decision_id": decision_id,
        "run_id": run_id,
        "documentation_required": required,
        "requirement_satisfied": satisfied,
        "status": status,
        "evidence": evidence,
    }
