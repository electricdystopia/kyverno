#!/usr/bin/env python3
"""Classify issues and authorize only repository-managed, reversible labels."""

import argparse
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path

import yaml

from dependency_pr import load_config


COMMENT_MARKER = "<!-- kyvernaut:issue-triage:v1 -->"
EMPTY_VALUES = {"", "_no response_", "no response", "n/a", "na", "none", "1."}
HEADING = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)


def parse_sections(body: str) -> dict[str, str]:
    matches = list(HEADING.finditer(body))
    sections = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[match.group(1).strip().casefold()] = body[match.end() : end].strip()
    return sections


def _present(value: str | None) -> bool:
    return bool(value and value.strip().casefold() not in EMPTY_VALUES)


def _section(sections: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = sections.get(name.casefold())
        if value is not None:
            return value
    return None


def _label_names(issue: dict) -> list[str]:
    values = []
    for label in issue.get("labels", []):
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            values.append(name)
    return values


def _unique_labels(labels: list[str]) -> list[str]:
    unique = []
    seen = set()
    for label in labels:
        normalized = label.casefold()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(label)
    return unique


def validate_label_catalog(repo_root: Path, config: dict) -> list[str]:
    """Verify every managed/excluded label is declared in repository metadata."""
    errors = []
    known = set()
    labels_path = repo_root / ".github/labels.yml"
    try:
        labels = yaml.safe_load(labels_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return [f"cannot read .github/labels.yml: {error}"]
    if not isinstance(labels, dict):
        return [".github/labels.yml must be a mapping"]
    known.update(str(name).casefold() for name in labels)

    templates = repo_root / ".github/ISSUE_TEMPLATE"
    for path in sorted(templates.glob("*.y*ml")):
        try:
            template = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            errors.append(f"{path.relative_to(repo_root)} is invalid YAML: {error}")
            continue
        if not isinstance(template, dict):
            continue
        values = template.get("labels", [])
        if isinstance(values, list):
            known.update(value.casefold() for value in values if isinstance(value, str))

    policy = config["issue_triage"]
    required = policy["managed_labels"] + policy["excluded_labels"]
    missing = sorted(label for label in required if label.casefold() not in known)
    if missing:
        errors.append("triage labels missing from repository metadata: " + ", ".join(missing))
    return errors


def classify_issue(issue: dict) -> dict:
    title = issue.get("title") if isinstance(issue.get("title"), str) else ""
    body = issue.get("body") if isinstance(issue.get("body"), str) else ""
    labels = _label_names(issue)
    normalized_labels = {label.casefold() for label in labels}
    text = f"{title}\n{body}".casefold()

    if "enhancement" in normalized_labels or title.casefold().startswith("[feature]"):
        primary = "feature"
        basis = "template label/title"
    elif "bug" in normalized_labels or title.casefold().startswith("[bug]"):
        primary = "bug"
        basis = "template label/title"
    elif "question" in normalized_labels or title.rstrip().endswith("?") or re.search(r"\bhow (do|can|to)\b", title.casefold()):
        primary = "question"
        basis = "label/title wording"
    else:
        primary = "unknown"
        basis = "no deterministic template signal"

    areas = []
    if (
        "type:cli" in normalized_labels
        or "[cli]" in title.casefold()
        or "kubectl kyverno" in text
        or "kyverno cli" in text
    ):
        areas.append("cli")
    if re.search(r"\bwebhooks?\b|\badmission(review| controller)?\b", text):
        areas.append("webhook")
    return {"primary": primary, "areas": areas, "basis": basis}


def missing_information(issue: dict, classification: dict) -> list[str]:
    body = issue.get("body") if isinstance(issue.get("body"), str) else ""
    sections = parse_sections(body)
    missing = []
    primary = classification["primary"]

    if primary == "bug":
        if not _present(_section(sections, "Kyverno Version", "Kyverno CLI Version")):
            missing.append("Kyverno version")
        if not _present(_section(sections, "Description")):
            missing.append("actual behavior / description")
        steps = _section(sections, "Steps to reproduce")
        if not _present(steps):
            missing.append("minimal steps to reproduce")
        if not _present(_section(sections, "Expected behavior")):
            missing.append("expected behavior")
        if "webhook" in classification["areas"]:
            if not _present(_section(sections, "Kubernetes Version")):
                missing.append("Kubernetes version")
            if not _present(_section(sections, "Kubernetes Platform")):
                missing.append("Kubernetes platform")
            if not _present(_section(sections, "Kyverno Rule Type")):
                missing.append("Kyverno rule type")
        reproduction_text = f"{steps or ''}\n{body}"
        if "apiversion:" not in reproduction_text.casefold() and "```yaml" not in reproduction_text.casefold():
            missing.append("minimal policy/resource manifests or a self-contained test case")
    elif primary == "feature":
        if not _present(_section(sections, "Problem Statement")):
            missing.append("problem statement")
        if not _present(_section(sections, "Solution Description")):
            missing.append("proposed solution")
    return missing


def evaluate_issue(event: dict, config: dict, *, environment: dict[str, str] | None = None, run_id: str | None = None) -> dict:
    issue = event.get("issue")
    if not isinstance(issue, dict):
        raise ValueError("event must contain an issue object")
    policy = config["issue_triage"]
    environment = environment or {}
    labels = _label_names(issue)
    switch = config["kill_switch"]
    pause_value = environment.get(switch["environment_variable"], "")
    paused = pause_value.strip().casefold() in {value.casefold() for value in switch["truthy_values"]}
    excluded = sorted(label for label in labels if label.casefold() in {value.casefold() for value in policy["excluded_labels"]})
    classification = classify_issue(issue)
    missing = missing_information(issue, classification)

    blockers = []
    if not config["enabled"]:
        blockers.append({"code": "assistant_disabled", "detail": "assistant is disabled"})
    if not policy["enabled"]:
        blockers.append({"code": "workflow_disabled", "detail": "issue triage is disabled"})
    if paused:
        blockers.append({"code": "kill_switch_active", "detail": "repository kill switch is active"})
    if excluded:
        blockers.append({"code": "excluded_label", "detail": "excluded label(s): " + ", ".join(excluded)})
    issue_number = issue.get("number")
    if (
        not isinstance(issue_number, int)
        or isinstance(issue_number, bool)
        or issue_number <= 0
    ):
        blockers.append({"code": "invalid_issue_number", "detail": "issue number is invalid"})
    if issue.get("state") != "open":
        blockers.append({"code": "issue_not_open", "detail": "issue is not open"})
    if issue.get("pull_request") is not None:
        blockers.append({"code": "pull_request_not_issue", "detail": "pull requests are not triaged as issues"})

    suggestions = list(policy["suggested_labels"][classification["primary"]])
    for area in classification["areas"]:
        suggestions.extend(policy["suggested_labels"][area])
    suggestions = _unique_labels(suggestions)
    existing_normalized = {label.casefold() for label in labels}
    labels_to_apply = [
        label for label in suggestions if label.casefold() not in existing_normalized
    ]
    if len(labels_to_apply) > policy["max_labels_per_issue"]:
        blockers.append(
            {
                "code": "label_rate_limit",
                "detail": (
                    f"{len(labels_to_apply)} labels exceed the configured "
                    f"{policy['max_labels_per_issue']}-label action cap"
                ),
            }
        )
    action_authorized = bool(
        not blockers
        and labels_to_apply
        and policy["apply_labels"]
        and config["mode"] == "active"
    )
    if action_authorized:
        label_recommendation = "apply"
    elif blockers:
        label_recommendation = "do_not_apply"
    elif not labels_to_apply:
        label_recommendation = "no_change"
    elif policy["apply_labels"]:
        label_recommendation = "would_apply"
    else:
        label_recommendation = "suggest_only"
    body = issue.get("body") if isinstance(issue.get("body"), str) else ""
    title = issue.get("title") if isinstance(issue.get("title"), str) else ""
    evidence = {
        "issue_number": issue_number,
        "issue_url": issue.get("html_url"),
        "author": (issue.get("user") or {}).get("login"),
        "state": issue.get("state"),
        "existing_labels": sorted(labels),
        "classification": classification,
        "suggested_labels": suggestions,
        "labels_to_apply": labels_to_apply,
        "missing_information": missing,
        "title_sha256": hashlib.sha256(title.encode()).hexdigest(),
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    label_action = {
        "recommendation": label_recommendation,
        "action_authorized": action_authorized,
        "requested_labels": labels_to_apply,
        "max_labels_per_issue": policy["max_labels_per_issue"],
    }
    material = {
        "run_id": run_id,
        "mode": config["mode"],
        "evidence": evidence,
        "blockers": blockers,
        "label_action": label_action,
    }
    decision_id = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return {
        "schema_version": 1,
        "decision_id": decision_id,
        "run_id": run_id,
        "mode": config["mode"],
        "comment_allowed": not blockers,
        "label_action": label_action,
        "labels_applied": [],
        "blockers": blockers,
        "evidence": evidence,
    }


def render_comment(decision: dict, run_url: str | None = None) -> str:
    evidence = decision["evidence"]
    classification = evidence["classification"]
    lines = [
        COMMENT_MARKER,
        "## Kyvernaut issue triage",
        "",
    ]
    action = decision["label_action"]
    if action["action_authorized"]:
        lines.append(
            "> Active policy authorized managed labels; a separate audited step "
            "revalidates the issue before applying them."
        )
    elif action["recommendation"] == "would_apply":
        lines.append(
            "> Shadow mode: managed labels are recommendations only, and issue "
            "content was not executed."
        )
    else:
        lines.append(
            "> Deterministic triage only; issue content was parsed as data and was not executed."
        )
    lines.extend(
        [
        "",
        f"- Classification: **{html.escape(classification['primary'])}** "
        f"({html.escape(classification['basis'])})",
        ]
    )
    if classification["areas"]:
        lines.append("- Areas: " + ", ".join(f"`{html.escape(area)}`" for area in classification["areas"]))
    lines.append(
        "- Suggested labels: "
        + ", ".join(f"`{html.escape(label)}`" for label in evidence["suggested_labels"])
    )
    if evidence["missing_information"]:
        lines.extend(["", "To make reproduction and maintainer review possible, please add:"])
        lines.extend(f"- {html.escape(item)}" for item in evidence["missing_information"])
    else:
        lines.extend(["", "The selected issue-form fields appear complete for initial triage."])
    lines.extend(["", f"Decision ID: <code>{html.escape(decision['decision_id'])}</code>."])
    if run_url:
        lines.append(f"[Open the audit run]({html.escape(run_url, quote=True)}).")
    return "\n".join(lines) + "\n"


def main() -> int:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--config", type=Path, default=root / ".github/ai-maintainer.yaml")
    parser.add_argument("--repo", type=Path, default=root)
    parser.add_argument("--validate-labels", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--run-url")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.validate_labels:
            errors = validate_label_catalog(args.repo, config)
            if errors:
                raise ValueError("invalid issue label catalog:\n  - " + "\n  - ".join(errors))
            print("Issue triage label catalog is valid.")
            return 0
        if args.event is None or args.output_dir is None:
            raise ValueError("--event and --output-dir are required unless --validate-labels is used")
        event = json.loads(args.event.read_text(encoding="utf-8"))
        decision = evaluate_issue(event, config, environment=dict(os.environ), run_id=args.run_id)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "issue-triage-decision.json").write_text(
            json.dumps(decision, indent=2) + "\n", encoding="utf-8"
        )
        (args.output_dir / "comment.md").write_text(
            render_comment(decision, args.run_url), encoding="utf-8"
        )
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write(f"comment_allowed={str(decision['comment_allowed']).lower()}\n")
                stream.write(
                    f"label_action_authorized={str(decision['label_action']['action_authorized']).lower()}\n"
                )
                stream.write(f"decision_id={decision['decision_id']}\n")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"issue triage failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
