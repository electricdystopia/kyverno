#!/usr/bin/env python3
"""Build a capped batch of scheduled PR hygiene shadow reminders."""

import argparse
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dependency_pr import load_config
from pr_hygiene import evaluate_hygiene


COMMENT_MARKER = "<!-- kyvernaut:pr-hygiene:v1 -->"
NUDGE_ACTIONS = {"nudge_author", "nudge_reviewers"}


def _login(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    # GitHub logins contain alphanumerics and single hyphens. Refuse to
    # synthesize a mention from unexpected API data.
    if not all(character.isalnum() or character == "-" for character in value):
        return None
    return value


def render_hygiene_comment(decision: dict, run_url: str | None = None) -> str:
    recommendations = [
        item for item in decision["recommendations"] if item["action"] in NUDGE_ACTIONS
    ]
    lines = [
        COMMENT_MARKER,
        "## Kyvernaut PR hygiene reminder",
        "",
        "> Comment-only shadow automation; no branch, review, label, or merge state was changed.",
        "",
    ]
    author = _login(decision["evidence"]["author"])
    reviewers = [
        login
        for login in (_login(value) for value in decision["evidence"]["requested_reviewers"])
        if login
    ]
    for recommendation in recommendations:
        if recommendation["action"] == "nudge_author":
            recipient = f"@{author}" if author else "PR author"
            lines.append(f"- {recipient}: this PR has had no recorded activity for "
                         f"**{decision['evidence']['age_days']} days**.")
        elif recommendation["action"] == "nudge_reviewers":
            recipients = ", ".join(f"@{login}" for login in reviewers) or "Requested reviewers"
            lines.append(
                f"- {recipients}: review has been waiting for "
                f"**{decision['evidence']['age_days']} days**."
            )
    lines.extend(
        [
            "",
            "If work is intentionally paused, add a configured hold/no-nudge label.",
            f"Decision ID: <code>{html.escape(decision['decision_id'])}</code>.",
        ]
    )
    if run_url:
        lines.append(f"[Open the audit run]({html.escape(run_url, quote=True)}).")
    return "\n".join(lines) + "\n"


def build_batch(
    pull_requests: list[dict],
    config: dict,
    *,
    now: datetime,
    environment: dict[str, str],
    run_id: str | None,
    run_url: str | None,
) -> dict:
    decisions = []
    candidates = []
    for pr in sorted(pull_requests, key=lambda item: item.get("updated_at") or ""):
        mergeable_state = pr.get("mergeable_state")
        if mergeable_state not in {"behind", "blocked", "clean", "dirty", "unstable"}:
            mergeable_state = "unknown"
        decision = evaluate_hygiene(
            {"number": pr.get("number"), "pull_request": pr},
            config,
            mergeable_state=mergeable_state,
            now=now,
            environment=environment,
            run_id=run_id,
        )
        decisions.append(decision)
        if any(item["action"] in NUDGE_ACTIONS for item in decision["recommendations"]):
            candidates.append(
                {
                    "pull_request_number": decision["evidence"]["pull_request_number"],
                    "decision_id": decision["decision_id"],
                    "body": render_hygiene_comment(decision, run_url),
                }
            )

    policy = config["pr_hygiene"]
    return {
        "schema_version": 1,
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "assistant_enabled": config["enabled"] and policy["enabled"],
        "cooldown_days": policy["nudge_cooldown_days"],
        "max_nudges_per_run": policy["max_nudges_per_run"],
        "evaluated_pull_requests": len(decisions),
        "eligible_nudges": len(candidates),
        "comments": candidates[: policy["max_nudges_per_run"]],
        "decisions": decisions,
    }


def main() -> int:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pull-requests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=root / ".github/ai-maintainer.yaml")
    parser.add_argument("--run-id")
    parser.add_argument("--run-url")
    parser.add_argument("--now", help="ISO-8601 test override; defaults to current UTC time")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    try:
        pull_requests = json.loads(args.pull_requests.read_text(encoding="utf-8"))
        if not isinstance(pull_requests, list):
            raise ValueError("pull request input must be a JSON list")
        config = load_config(args.config)
        now = (
            datetime.fromisoformat(args.now.replace("Z", "+00:00"))
            if args.now
            else datetime.now(timezone.utc)
        )
        if now.tzinfo is None:
            raise ValueError("--now must include a timezone")
        batch = build_batch(
            pull_requests,
            config,
            now=now,
            environment=dict(os.environ),
            run_id=args.run_id,
            run_url=args.run_url,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(batch, indent=2) + "\n", encoding="utf-8")
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write(f"assistant_enabled={str(batch['assistant_enabled']).lower()}\n")
                stream.write(f"comment_count={len(batch['comments'])}\n")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"hygiene batch generation failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
