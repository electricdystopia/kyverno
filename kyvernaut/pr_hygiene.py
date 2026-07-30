#!/usr/bin/env python3
"""Evaluate behind and stale pull requests without mutating GitHub."""

import hashlib
import json
from datetime import datetime, timezone

from dependency_pr import VALID_MERGEABLE_STATES


def parse_github_time(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("pull request updated_at must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("pull request updated_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _labels(pr: dict) -> list[str]:
    values = []
    for label in pr.get("labels", []):
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            values.append(name)
    return values


def _reviewers(pr: dict) -> list[str]:
    values = []
    for reviewer in pr.get("requested_reviewers", []):
        login = reviewer.get("login") if isinstance(reviewer, dict) else reviewer
        if isinstance(login, str):
            values.append(login)
    return values


def _paused(config: dict, environment: dict[str, str]) -> bool:
    switch = config["kill_switch"]
    raw = environment.get(switch["environment_variable"])
    if raw is None:
        return False
    truthy = {value.casefold() for value in switch["truthy_values"]}
    return raw.strip().casefold() in truthy


def evaluate_hygiene(
    event: dict,
    config: dict,
    *,
    mergeable_state: str = "unknown",
    now: datetime | None = None,
    environment: dict[str, str] | None = None,
    run_id: str | None = None,
) -> dict:
    if mergeable_state not in VALID_MERGEABLE_STATES:
        raise ValueError(f"invalid mergeable state: {mergeable_state}")
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        raise ValueError("event must contain a pull_request object")
    policy = config["pr_hygiene"]
    environment = environment or {}
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    updated_at = parse_github_time(pr.get("updated_at"))
    age_days = max(0, int((now - updated_at).total_seconds() // 86400))
    labels = _labels(pr)
    reviewers = _reviewers(pr)
    base = pr.get("base") or {}
    head = pr.get("head") or {}
    base_branch = base.get("ref")
    base_repo = (base.get("repo") or {}).get("full_name")
    head_repo = (head.get("repo") or {}).get("full_name")
    same_repo = bool(base_repo and head_repo and base_repo.casefold() == head_repo.casefold())

    blockers = []

    def block(condition: bool, code: str, detail: str):
        if condition:
            blockers.append({"code": code, "detail": detail})

    block(not config["enabled"], "assistant_disabled", "enabled is false in repository policy")
    block(not policy["enabled"], "workflow_disabled", "PR hygiene workflow is disabled")
    block(_paused(config, environment), "kill_switch_active", "repository kill switch is active")
    block(bool(pr.get("draft")), "draft_pr", "draft pull requests are not nudged")
    block(base_branch not in policy["base_branches"], "base_branch_not_allowed", f"base branch {base_branch!r} is not allowlisted")
    held = sorted(
        label
        for label in labels
        if label.casefold() in {configured.casefold() for configured in policy["hold_labels"]}
    )
    block(bool(held), "hold_label", "PR has hold/no-nudge label(s): " + ", ".join(held))

    candidates = []
    if not blockers:
        if mergeable_state == "behind" and policy["suggest_branch_update"]:
            can_update = same_repo or bool(pr.get("maintainer_can_modify"))
            candidates.append(
                {
                    "action": "update_branch" if can_update else "request_author_update",
                    "reason": "head branch is behind the configured base branch",
                    "automatable": can_update,
                }
            )
        if age_days >= policy["stale_after_days"]:
            candidates.append(
                {
                    "action": "nudge_author",
                    "reason": f"no recorded PR activity for {age_days} days",
                    "automatable": True,
                }
            )
        elif reviewers and age_days >= policy["reviewer_nudge_after_days"]:
            candidates.append(
                {
                    "action": "nudge_reviewers",
                    "reason": f"requested review has been idle for {age_days} days",
                    "automatable": True,
                    "reviewers": reviewers,
                }
            )

    action_authorized = bool(candidates) and config["mode"] == "active" and not blockers
    evidence = {
        "pull_request_number": pr.get("number") or event.get("number"),
        "pull_request_url": pr.get("html_url"),
        "author": (pr.get("user") or {}).get("login"),
        "base_branch": base_branch,
        "head_ref": head.get("ref"),
        "same_repository": same_repo,
        "maintainer_can_modify": bool(pr.get("maintainer_can_modify")),
        "mergeable_state": mergeable_state,
        "updated_at": updated_at.isoformat(),
        "evaluated_at": now.isoformat(),
        "age_days": age_days,
        "labels": labels,
        "requested_reviewers": reviewers,
    }
    material = {
        "mode": config["mode"],
        "run_id": run_id,
        "evidence": evidence,
        "blockers": blockers,
        "candidates": candidates,
    }
    decision_id = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return {
        "schema_version": 1,
        "decision_id": decision_id,
        "run_id": run_id,
        "mode": config["mode"],
        "recommendations": candidates,
        "action_authorized": action_authorized,
        "blockers": blockers,
        "evidence": evidence,
    }
