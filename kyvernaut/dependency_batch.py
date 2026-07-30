#!/usr/bin/env python3
"""Build a capped, auditable dependency-merge execution batch.

The input is trusted GitHub API evidence collected by the workflow. This
program never calls GitHub or performs a merge; it only emits authorized
actions when every dependency policy gate passes in active mode.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dependency_pr import (
    VALID_CI_STATES,
    VALID_MERGEABLE_STATES,
    evaluate,
    load_config,
)
from scope_tests import load_rules


def _candidate(value: object) -> tuple[dict, list[str], str, str]:
    if not isinstance(value, dict):
        raise ValueError("each candidate must be a JSON object")
    pr = value.get("pull_request")
    if not isinstance(pr, dict):
        raise ValueError("each candidate must contain a pull_request object")
    files = value.get("changed_files")
    if (
        not isinstance(files, list)
        or not files
        or not all(isinstance(path, str) and path and path.strip() == path for path in files)
    ):
        raise ValueError("each candidate changed_files value must be a non-empty clean string list")
    if len(files) != len(set(files)):
        raise ValueError("candidate changed_files must not contain duplicates")
    ci_state = value.get("ci_state")
    if ci_state not in VALID_CI_STATES:
        raise ValueError(f"invalid candidate CI state: {ci_state!r}")
    mergeable_state = value.get("mergeable_state")
    if mergeable_state not in VALID_MERGEABLE_STATES:
        raise ValueError(f"invalid candidate mergeable state: {mergeable_state!r}")
    return pr, sorted(files), ci_state, mergeable_state


def build_batch(
    candidates: list[object],
    config: dict,
    rules: list[dict],
    *,
    environment: dict[str, str],
    run_id: str | None,
    run_url: str | None,
) -> dict:
    if not isinstance(candidates, list):
        raise ValueError("candidate input must be a JSON list")
    if len(candidates) > 50:
        raise ValueError("candidate input exceeds the 50-PR collection cap")

    decisions = []
    authorized = []
    seen_numbers = set()
    for raw in candidates:
        pr, files, ci_state, mergeable_state = _candidate(raw)
        number = pr.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise ValueError("candidate pull request number must be a positive integer")
        if number in seen_numbers:
            raise ValueError(f"duplicate candidate pull request number: {number}")
        seen_numbers.add(number)

        decision = evaluate(
            {"number": number, "pull_request": pr},
            files,
            config,
            rules,
            ci_state=ci_state,
            mergeable_state=mergeable_state,
            environment=environment,
            run_id=run_id,
        )
        decisions.append(decision)
        if decision["action_authorized"]:
            evidence = decision["evidence"]
            head_sha = evidence["head_sha"]
            authorized.append(
                {
                    "pull_request_number": number,
                    "decision_id": decision["decision_id"],
                    "head_sha": head_sha,
                    "expected": {
                        "author": evidence["author"],
                        "base_branch": evidence["base_branch"],
                        "title": evidence["title"],
                        "body_sha256": evidence["body_sha256"],
                        "labels": sorted(evidence["labels"]),
                        "changed_files": evidence["changed_files"],
                        "dependency_update": evidence["dependency_update"],
                    },
                }
            )

    policy = config["dependency_updates"]
    limit = policy["max_merges_per_run"]
    actions = authorized[:limit]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "run_url": run_url,
        "mode": config["mode"],
        "assistant_enabled": config["enabled"] and policy["enabled"],
        "merge_method": policy["merge_method"],
        "max_merges_per_run": limit,
        "evaluated_pull_requests": len(decisions),
        "authorized_before_rate_limit": len(authorized),
        "rate_limited_actions": max(0, len(authorized) - len(actions)),
        "actions": actions,
        "decisions": decisions,
    }


def main() -> int:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=root / ".github/ai-maintainer.yaml")
    parser.add_argument("--map", type=Path, default=Path(__file__).parent / "path-test-map.yaml")
    parser.add_argument("--run-id")
    parser.add_argument("--run-url")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    try:
        candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
        config = load_config(args.config)
        rules = load_rules(args.map)
        batch = build_batch(
            candidates,
            config,
            rules,
            environment=dict(os.environ),
            run_id=args.run_id,
            run_url=args.run_url,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(batch, indent=2) + "\n", encoding="utf-8")
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write(f"assistant_enabled={str(batch['assistant_enabled']).lower()}\n")
                stream.write(f"action_count={len(batch['actions'])}\n")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"dependency batch generation failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
