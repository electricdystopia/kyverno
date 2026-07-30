#!/usr/bin/env python3
"""Evaluate a dependency PR against Kyvernaut's fail-closed merge policy.

This module is deliberately a pure decision engine. It reads a GitHub
``pull_request`` event plus trusted orchestration signals and emits an
auditable JSON recommendation. A separate executor may consume an authorized
decision, but this module does not call GitHub and cannot merge.

Example:
    python kyvernaut/dependency_pr.py \
      --event event.json \
      --changed-files changed-files.txt \
      --ci-state success \
      --mergeable-state clean
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import yaml

from scope_tests import load_rules, scope


SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
BUMP_TITLE = re.compile(r"\bbump\s+.+?\s+from\s+(\S+)\s+to\s+(\S+)", re.IGNORECASE)
HEAD_SHA = re.compile(r"^[0-9a-f]{40}$")
VALID_MODES = {"shadow", "active"}
VALID_CI_STATES = {"success", "failure", "pending", "unknown"}
VALID_MERGEABLE_STATES = {"behind", "blocked", "clean", "dirty", "unknown", "unstable"}
SUPPORTED_DEPENDENCY_ACTORS = {"dependabot[bot]", "renovate[bot]"}
SUPPORTED_REPRO_IMAGES = {
    "busybox",
    "busybox:latest",
    "nginx",
    "nginx:latest",
    "registry.k8s.io/pause:3.10",
}
SUPPORTED_REPRO_APPROVAL_LABELS = {"kyvernaut:repro-approved"}


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    errors = validate_config(config)
    if errors:
        raise ValueError("invalid AI maintainer config:\n  - " + "\n  - ".join(errors))
    return config


def validate_config(config: dict) -> list[str]:
    errors = []
    if not isinstance(config, dict):
        return ["document must be a mapping"]
    if config.get("version") != 1:
        errors.append("version must be 1")
    if not isinstance(config.get("enabled"), bool):
        errors.append("enabled must be a boolean")
    if config.get("mode") not in VALID_MODES:
        errors.append("mode must be one of: active, shadow")

    kill_switch = config.get("kill_switch")
    if not isinstance(kill_switch, dict):
        errors.append("kill_switch must be a mapping")
    else:
        if not isinstance(kill_switch.get("environment_variable"), str):
            errors.append("kill_switch.environment_variable must be a string")
        truthy = kill_switch.get("truthy_values")
        if not isinstance(truthy, list) or not truthy or not all(isinstance(value, str) for value in truthy):
            errors.append("kill_switch.truthy_values must be a non-empty string list")

    policy = config.get("dependency_updates")
    if not isinstance(policy, dict):
        return errors + ["dependency_updates must be a mapping"]
    for field in ("enabled", "require_green_ci", "require_mergeable"):
        if not isinstance(policy.get(field), bool):
            errors.append(f"dependency_updates.{field} must be a boolean")
    for field in (
        "actors",
        "base_branches",
        "allowed_update_types",
        "allowed_files",
        "hold_labels",
        "breaking_change_markers",
    ):
        values = policy.get(field)
        if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
            errors.append(f"dependency_updates.{field} must be a non-empty string list")
    update_types = set(policy.get("allowed_update_types") or [])
    unsupported = update_types - {"patch", "minor"}
    if unsupported:
        errors.append(
            "dependency_updates.allowed_update_types contains unsafe/unknown values: "
            + ", ".join(sorted(unsupported))
        )
    actors = {value.casefold() for value in policy.get("actors") or [] if isinstance(value, str)}
    unsupported_actors = actors - SUPPORTED_DEPENDENCY_ACTORS
    if unsupported_actors:
        errors.append(
            "dependency_updates.actors contains actors unsupported by the executor: "
            + ", ".join(sorted(unsupported_actors))
        )
    max_merges = policy.get("max_merges_per_run")
    if not isinstance(max_merges, int) or isinstance(max_merges, bool) or not 1 <= max_merges <= 10:
        errors.append("dependency_updates.max_merges_per_run must be an integer from 1 through 10")
    if policy.get("merge_method") != "squash":
        errors.append("dependency_updates.merge_method must be squash")

    hygiene = config.get("pr_hygiene")
    if not isinstance(hygiene, dict):
        return errors + ["pr_hygiene must be a mapping"]
    for field in ("enabled", "suggest_branch_update"):
        if not isinstance(hygiene.get(field), bool):
            errors.append(f"pr_hygiene.{field} must be a boolean")
    for field in ("base_branches", "hold_labels"):
        values = hygiene.get(field)
        if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
            errors.append(f"pr_hygiene.{field} must be a non-empty string list")
    for field in (
        "stale_after_days",
        "reviewer_nudge_after_days",
        "nudge_cooldown_days",
        "max_nudges_per_run",
    ):
        value = hygiene.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"pr_hygiene.{field} must be a positive integer")

    scoped_ci = config.get("scoped_ci")
    if not isinstance(scoped_ci, dict):
        return errors + ["scoped_ci must be a mapping"]
    for field in ("enabled", "expand_uncertain_scope"):
        if not isinstance(scoped_ci.get(field), bool):
            errors.append(f"scoped_ci.{field} must be a boolean")
    for field in ("max_unit_jobs", "max_conformance_jobs"):
        value = scoped_ci.get(field)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= 20
        ):
            errors.append(f"scoped_ci.{field} must be an integer from 1 through 20")
    kubernetes_version = scoped_ci.get("kubernetes_version")
    if not isinstance(kubernetes_version, str) or not re.fullmatch(
        r"v\d+\.\d+\.\d+", kubernetes_version
    ):
        errors.append("scoped_ci.kubernetes_version must be a pinned vX.Y.Z version")

    triage = config.get("issue_triage")
    if not isinstance(triage, dict):
        return errors + ["issue_triage must be a mapping"]
    for field in ("enabled", "apply_labels"):
        if not isinstance(triage.get(field), bool):
            errors.append(f"issue_triage.{field} must be a boolean")
    for field in ("excluded_labels", "managed_labels"):
        values = triage.get(field)
        if (
            not isinstance(values, list)
            or not values
            or not all(
                isinstance(value, str)
                and value
                and len(value) <= 50
                and "\n" not in value
                for value in values
            )
        ):
            errors.append(f"issue_triage.{field} must be a non-empty safe label list")
        elif len(values) != len({value.casefold() for value in values}):
            errors.append(f"issue_triage.{field} contains duplicate labels")
    maximum_labels = triage.get("max_labels_per_issue")
    if (
        not isinstance(maximum_labels, int)
        or isinstance(maximum_labels, bool)
        or not 1 <= maximum_labels <= 10
    ):
        errors.append("issue_triage.max_labels_per_issue must be an integer from 1 through 10")
    suggestions = triage.get("suggested_labels")
    required_suggestions = {"bug", "feature", "question", "unknown", "cli", "webhook"}
    if not isinstance(suggestions, dict):
        errors.append("issue_triage.suggested_labels must be a mapping")
    else:
        missing = required_suggestions - set(suggestions)
        if missing:
            errors.append(
                "issue_triage.suggested_labels is missing keys: " + ", ".join(sorted(missing))
            )
        for key, values in suggestions.items():
            if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
                errors.append(f"issue_triage.suggested_labels.{key} must be a non-empty string list")
        suggested = {
            value.casefold()
            for values in suggestions.values()
            if isinstance(values, list)
            for value in values
            if isinstance(value, str)
        }
        managed = {
            value.casefold()
            for value in triage.get("managed_labels") or []
            if isinstance(value, str)
        }
        unmanaged = suggested - managed
        if unmanaged:
            errors.append(
                "issue_triage suggestions contain unmanaged labels: "
                + ", ".join(sorted(unmanaged))
            )
    excluded_names = {
        value.casefold()
        for value in triage.get("excluded_labels") or []
        if isinstance(value, str)
    }
    managed_names = {
        value.casefold()
        for value in triage.get("managed_labels") or []
        if isinstance(value, str)
    }
    overlap = excluded_names & managed_names
    if overlap:
        errors.append(
            "issue_triage labels cannot be both managed and excluded: "
            + ", ".join(sorted(overlap))
        )

    reproduction = config.get("issue_reproduction")
    if not isinstance(reproduction, dict):
        return errors + ["issue_reproduction must be a mapping"]
    if not isinstance(reproduction.get("enabled"), bool):
        errors.append("issue_reproduction.enabled must be a boolean")
    for field in ("approval_labels", "allowed_kinds", "allowed_images"):
        values = reproduction.get(field)
        if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
            errors.append(f"issue_reproduction.{field} must be a non-empty string list")
    unsupported_approval_labels = (
        set(reproduction.get("approval_labels") or []) - SUPPORTED_REPRO_APPROVAL_LABELS
    )
    if unsupported_approval_labels:
        errors.append(
            "issue_reproduction.approval_labels contains labels unsupported by the trigger: "
            + ", ".join(sorted(unsupported_approval_labels))
        )
    unsupported_images = set(reproduction.get("allowed_images") or []) - SUPPORTED_REPRO_IMAGES
    if unsupported_images:
        errors.append(
            "issue_reproduction.allowed_images contains images not preloaded by the sandbox: "
            + ", ".join(sorted(unsupported_images))
        )
    for field in (
        "max_body_bytes",
        "max_documents",
        "max_manifest_bytes",
        "max_runtime_seconds",
        "max_output_bytes",
        "max_pods",
        "max_cpu_millicores",
        "max_memory_mebibytes",
    ):
        value = reproduction.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"issue_reproduction.{field} must be a positive integer")
    if (
        isinstance(reproduction.get("max_manifest_bytes"), int)
        and isinstance(reproduction.get("max_body_bytes"), int)
        and reproduction["max_manifest_bytes"] > reproduction["max_body_bytes"]
    ):
        errors.append("issue_reproduction.max_manifest_bytes cannot exceed max_body_bytes")
    bounded_reproduction_values = {
        "max_runtime_seconds": (30, 600),
        "max_output_bytes": (65536, 4194304),
        "max_pods": (1, 20),
        "max_cpu_millicores": (100, 4000),
        "max_memory_mebibytes": (128, 4096),
    }
    for field, (minimum, maximum) in bounded_reproduction_values.items():
        value = reproduction.get(field)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and not minimum <= value <= maximum
        ):
            errors.append(
                f"issue_reproduction.{field} must be from {minimum} through {maximum}"
            )
    namespace = reproduction.get("sandbox_namespace")
    if (
        not isinstance(namespace, str)
        or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", namespace)
        or namespace in {"default", "kube-system", "kube-public", "kube-node-lease", "kyverno"}
    ):
        errors.append("issue_reproduction.sandbox_namespace must be a dedicated DNS label")

    documentation = config.get("documentation")
    if not isinstance(documentation, dict):
        return errors + ["documentation must be a mapping"]
    if not isinstance(documentation.get("enabled"), bool):
        errors.append("documentation.enabled must be a boolean")
    for field in ("user_facing_paths", "documentation_paths", "exempt_labels"):
        values = documentation.get(field)
        if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
            errors.append(f"documentation.{field} must be a non-empty string list")
    repository = documentation.get("website_repository")
    if not isinstance(repository, str) or repository.count("/") != 1:
        errors.append("documentation.website_repository must be an owner/repository string")
    return errors


def classify_update(title: str) -> dict:
    """Classify the conventional dependency-bot title conservatively."""
    match = BUMP_TITLE.search(title)
    if not match:
        return {"type": "unknown", "from": None, "to": None}
    old_text, new_text = (value.rstrip(".,") for value in match.groups())
    old_match = SEMVER.fullmatch(old_text)
    new_match = SEMVER.fullmatch(new_text)
    if not old_match or not new_match:
        return {"type": "unknown", "from": old_text, "to": new_text}

    old = tuple(int(value) for value in old_match.groups())
    new = tuple(int(value) for value in new_match.groups())
    if new <= old:
        update_type = "unknown"
    elif new[0] != old[0]:
        update_type = "major"
    elif new[1] != old[1]:
        update_type = "minor"
    else:
        update_type = "patch"
    return {"type": update_type, "from": old_text, "to": new_text}


def _pull_request(event: dict) -> dict:
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        raise ValueError("event must contain a pull_request object")
    return pr


def _labels(pr: dict) -> list[str]:
    labels = []
    for label in pr.get("labels", []):
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            labels.append(label["name"])
        elif isinstance(label, str):
            labels.append(label)
    return labels


def _paused_from_environment(config: dict, environment: dict[str, str]) -> tuple[bool, str | None]:
    switch = config["kill_switch"]
    variable = switch["environment_variable"]
    raw = environment.get(variable)
    if raw is None:
        return False, None
    truthy = {value.casefold() for value in switch["truthy_values"]}
    return raw.strip().casefold() in truthy, raw


def evaluate(
    event: dict,
    changed_files: list[str],
    config: dict,
    rules: list[dict],
    *,
    ci_state: str = "unknown",
    mergeable_state: str = "unknown",
    environment: dict[str, str] | None = None,
    run_id: str | None = None,
) -> dict:
    if ci_state not in VALID_CI_STATES:
        raise ValueError(f"invalid CI state: {ci_state}")
    if mergeable_state not in VALID_MERGEABLE_STATES:
        raise ValueError(f"invalid mergeable state: {mergeable_state}")

    environment = environment or {}
    pr = _pull_request(event)
    policy = config["dependency_updates"]
    author = (pr.get("user") or {}).get("login")
    base_branch = (pr.get("base") or {}).get("ref")
    head_sha = (pr.get("head") or {}).get("sha")
    title = pr.get("title") if isinstance(pr.get("title"), str) else ""
    body = pr.get("body") if isinstance(pr.get("body"), str) else ""
    labels = _labels(pr)
    update = classify_update(title)
    scope_plan = scope(changed_files, rules)
    paused, pause_value = _paused_from_environment(config, environment)

    blockers = []

    def require(condition: bool, code: str, detail: str):
        if not condition:
            blockers.append({"code": code, "detail": detail})

    require(config["enabled"], "assistant_disabled", "enabled is false in repository policy")
    require(policy["enabled"], "workflow_disabled", "dependency update workflow is disabled")
    require(not paused, "kill_switch_active", "the configured repository kill switch is active")
    require(pr.get("state") == "open", "pr_not_open", "pull request is not open")
    require(
        isinstance(head_sha, str) and bool(HEAD_SHA.fullmatch(head_sha)),
        "invalid_head_sha",
        "pull request head SHA is missing or is not a 40-character lowercase hex SHA",
    )
    require(not pr.get("draft", False), "draft_pr", "draft pull requests are never merge candidates")
    allowed_actors = {actor.casefold() for actor in policy["actors"]}
    require(
        isinstance(author, str) and author.casefold() in allowed_actors,
        "untrusted_actor",
        f"PR author {author!r} is not allowlisted",
    )
    require(
        base_branch in policy["base_branches"],
        "base_branch_not_allowed",
        f"base branch {base_branch!r} is not allowlisted",
    )
    require(
        update["type"] in policy["allowed_update_types"],
        "update_type_not_allowed",
        f"dependency update type {update['type']!r} is not allowlisted",
    )
    unexpected_files = sorted(set(changed_files) - set(policy["allowed_files"]))
    require(
        not unexpected_files,
        "files_not_allowed",
        "PR changes files outside the dependency allowlist: " + ", ".join(unexpected_files),
    )
    require(
        scope_plan["auto_merge_eligible"],
        "scope_not_eligible",
        "diff-to-test-scope mapper did not approve the change as a dependency-only candidate",
    )
    hold_labels = {label.casefold() for label in policy["hold_labels"]}
    held_labels = sorted(label for label in labels if label.casefold() in hold_labels)
    require(not held_labels, "hold_label", "PR has hold label(s): " + ", ".join(held_labels))
    searchable_text = f"{title}\n{body}".casefold()
    breaking_signals = sorted(
        marker
        for marker in policy["breaking_change_markers"]
        if marker.casefold() in searchable_text
    )
    require(
        not breaking_signals,
        "breaking_change_signal",
        "PR title or body contains configured breaking-change marker(s): "
        + ", ".join(breaking_signals),
    )
    if policy["require_green_ci"]:
        require(ci_state == "success", "ci_not_green", f"required CI state is {ci_state!r}")
    if policy["require_mergeable"]:
        require(
            mergeable_state == "clean",
            "not_mergeable",
            f"mergeable state is {mergeable_state!r}",
        )

    policy_eligible = not blockers
    action_authorized = policy_eligible and config["mode"] == "active"
    if action_authorized:
        recommendation = "merge"
    elif policy_eligible:
        recommendation = "would_merge"
    else:
        recommendation = "do_not_merge"

    evidence = {
        "pull_request_number": pr.get("number") or event.get("number"),
        "pull_request_url": pr.get("html_url"),
        "author": author,
        "base_branch": base_branch,
        "title": title,
        "labels": labels,
        "draft": bool(pr.get("draft", False)),
        "state": pr.get("state"),
        "head_sha": head_sha,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "changed_files": changed_files,
        "dependency_update": update,
        "breaking_change_signals": breaking_signals,
        "ci_state": ci_state,
        "mergeable_state": mergeable_state,
        "scope_auto_merge_candidate": scope_plan["auto_merge_eligible"],
        "kill_switch": {
            "variable": config["kill_switch"]["environment_variable"],
            "present": pause_value is not None,
            "active": paused,
        },
    }
    decision_material = {
        "run_id": run_id,
        "mode": config["mode"],
        "evidence": evidence,
        "blockers": blockers,
    }
    decision_id = hashlib.sha256(
        json.dumps(decision_material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]

    return {
        "schema_version": 1,
        "decision_id": decision_id,
        "run_id": run_id,
        "mode": config["mode"],
        "recommendation": recommendation,
        "policy_eligible": policy_eligible,
        "action_authorized": action_authorized,
        "blockers": blockers,
        "evidence": evidence,
    }


def _changed_files(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=Path, required=True, help="GitHub pull_request event JSON")
    parser.add_argument("--changed-files", type=Path, required=True, help="One changed repository path per line")
    parser.add_argument("--config", type=Path, default=root / ".github/ai-maintainer.yaml")
    parser.add_argument("--map", type=Path, default=Path(__file__).parent / "path-test-map.yaml")
    parser.add_argument("--ci-state", choices=sorted(VALID_CI_STATES), default="unknown")
    parser.add_argument(
        "--mergeable-state",
        choices=sorted(VALID_MERGEABLE_STATES),
        default="unknown",
    )
    parser.add_argument("--run-id", help="External workflow/run identifier for audit correlation")
    args = parser.parse_args()

    try:
        event = json.loads(args.event.read_text(encoding="utf-8"))
        config = load_config(args.config)
        rules = load_rules(args.map)
        decision = evaluate(
            event,
            _changed_files(args.changed_files),
            config,
            rules,
            ci_state=args.ci_state,
            mergeable_state=args.mergeable_state,
            environment=dict(os.environ),
            run_id=args.run_id,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"dependency PR evaluation failed: {error}", file=sys.stderr)
        return 2

    print(json.dumps(decision, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
