#!/usr/bin/env python3
"""Plan a bounded draft documentation pull request in kyverno/website.

This module is a pure decision engine. It never calls GitHub and never
generates documentation prose. A maintainer supplies the target Markdown path
and complete draft content through a manual workflow; the planner validates
that request against an immutable source PR and website snapshot.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

import yaml

from dependency_pr import HEAD_SHA, load_config
from docs_requirement import evaluate_docs


WRITE_PERMISSIONS = {"admin", "maintain", "write"}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _label_names(pr: dict) -> list[str]:
    labels = []
    for value in pr.get("labels", []):
        name = value.get("name") if isinstance(value, dict) else value
        if isinstance(name, str):
            labels.append(name)
    return sorted(labels)


def _block(blockers: list[dict], code: str, detail: str) -> None:
    blockers.append({"code": code, "detail": detail})


def _validate_content(
    website_path: str,
    content: str,
    policy: dict,
    blockers: list[dict],
) -> dict:
    root = policy["content_root"]
    relative = PurePosixPath(website_path)
    safe_path = bool(
        website_path
        and not website_path.startswith("/")
        and "\\" not in website_path
        and "//" not in website_path
        and relative.as_posix() == website_path
        and "." not in relative.parts
        and ".." not in relative.parts
        and website_path.startswith(root)
        and len(relative.parts) > len(PurePosixPath(root).parts)
        and relative.suffix in policy["allowed_extensions"]
        and all(not part.startswith(".") for part in relative.parts)
    )
    if not safe_path:
        _block(
            blockers,
            "invalid_website_path",
            (
                f"target must be a normalized {policy['allowed_extensions']} file "
                f"below {root}"
            ),
        )

    encoded = content.encode("utf-8")
    if not content.strip():
        _block(blockers, "empty_content", "draft content is empty")
    if len(encoded) > policy["max_content_bytes"]:
        _block(
            blockers,
            "content_too_large",
            (
                f"draft is {len(encoded)} bytes; configured maximum is "
                f"{policy['max_content_bytes']}"
            ),
        )
    if "\x00" in content:
        _block(blockers, "nul_in_content", "draft content contains a NUL byte")
    if "\r" in content or (content and not content.endswith("\n")):
        _block(
            blockers,
            "noncanonical_content",
            "draft content must use LF line endings and end with a newline",
        )

    frontmatter = None
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        _block(
            blockers,
            "missing_frontmatter",
            "draft content must start with YAML frontmatter",
        )
    else:
        try:
            closing = lines.index("---", 1)
        except ValueError:
            _block(
                blockers,
                "missing_frontmatter",
                "draft content must close its YAML frontmatter",
            )
        else:
            try:
                frontmatter = yaml.safe_load("\n".join(lines[1:closing]))
            except yaml.YAMLError as error:
                _block(
                    blockers,
                    "invalid_frontmatter",
                    f"draft frontmatter is invalid YAML: {error}",
                )
            if not isinstance(frontmatter, dict):
                _block(
                    blockers,
                    "invalid_frontmatter",
                    "draft frontmatter must be a mapping",
                )
            elif (
                not isinstance(frontmatter.get("title"), str)
                or not frontmatter["title"].strip()
            ):
                _block(
                    blockers,
                    "missing_title",
                    "draft frontmatter must contain a non-empty title",
                )
            if not any(line.strip() for line in lines[closing + 1 :]):
                _block(
                    blockers,
                    "empty_document_body",
                    "draft content must contain documentation after frontmatter",
                )

    return {
        "path": website_path,
        "content_bytes": len(encoded),
        "content_sha256": _sha256(content),
        "frontmatter_title": (
            frontmatter.get("title")
            if isinstance(frontmatter, dict)
            and isinstance(frontmatter.get("title"), str)
            else None
        ),
    }


def evaluate_draft(
    event: dict,
    changed_files: list[str],
    target_snapshot: dict,
    website_path: str,
    content: str,
    config: dict,
    *,
    actor_permission: str,
    environment: dict[str, str] | None = None,
    run_id: str | None = None,
) -> dict:
    """Return an auditable plan without performing any external mutation."""
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        raise ValueError("event must contain a pull_request object")
    source_repository = (event.get("repository") or {}).get("full_name")
    if not isinstance(source_repository, str) or source_repository.count("/") != 1:
        raise ValueError("event.repository.full_name must be an owner/repository")

    documentation = config["documentation"]
    policy = documentation["draft_pull_requests"]
    blockers: list[dict] = []
    environment = environment or {}
    switch = config["kill_switch"]
    paused = environment.get(switch["environment_variable"], "").strip().casefold() in {
        value.casefold() for value in switch["truthy_values"]
    }

    if not config["enabled"]:
        _block(blockers, "assistant_disabled", "assistant is disabled")
    if config["mode"] != "active":
        _block(blockers, "shadow_mode", "global policy is not active")
    if not documentation["enabled"]:
        _block(blockers, "documentation_disabled", "documentation policy is disabled")
    if source_repository != documentation["source_repository"]:
        _block(
            blockers,
            "source_repository_mismatch",
            "source event is for an unexpected repository",
        )
    if not policy["enabled"]:
        _block(
            blockers,
            "draft_workflow_disabled",
            "documentation draft pull requests are disabled",
        )
    if paused:
        _block(blockers, "kill_switch_active", "repository kill switch is active")
    if actor_permission not in WRITE_PERMISSIONS:
        _block(
            blockers,
            "dispatcher_not_authorized",
            "manual dispatcher does not have write, maintain, or admin permission",
        )

    pr_number = pr.get("number")
    if (
        not isinstance(pr_number, int)
        or isinstance(pr_number, bool)
        or pr_number <= 0
    ):
        _block(blockers, "invalid_pr_number", "source PR number is invalid")
    if pr.get("state") != "open":
        _block(blockers, "source_pr_not_open", "source PR is not open")
    if bool(pr.get("draft")):
        _block(blockers, "source_pr_is_draft", "source PR is still a draft")

    base_ref = (pr.get("base") or {}).get("ref")
    if base_ref != "main":
        _block(blockers, "unsupported_source_base", "source PR must target main")
    head_sha = (pr.get("head") or {}).get("sha")
    if not isinstance(head_sha, str) or not HEAD_SHA.fullmatch(head_sha):
        _block(blockers, "invalid_source_head", "source PR head SHA is invalid")

    if (
        not isinstance(changed_files, list)
        or not all(isinstance(path, str) and path for path in changed_files)
    ):
        raise ValueError("changed files must be a string list")
    normalized_files = sorted(set(changed_files))
    if len(normalized_files) != len(changed_files):
        _block(blockers, "duplicate_changed_files", "changed-file evidence has duplicates")
    if len(normalized_files) > policy["max_changed_files"]:
        _block(
            blockers,
            "too_many_changed_files",
            (
                f"source PR has {len(normalized_files)} paths; configured maximum is "
                f"{policy['max_changed_files']}"
            ),
        )

    docs_decision = evaluate_docs(event, normalized_files, config, run_id=run_id)
    if not docs_decision["documentation_required"]:
        _block(
            blockers,
            "documentation_not_required",
            "source PR does not match a configured user-facing path",
        )
    elif docs_decision["status"] != "missing":
        _block(
            blockers,
            "documentation_already_satisfied",
            "source PR already contains documentation evidence or an exemption",
        )

    target_repository = target_snapshot.get("repository")
    if target_repository != documentation["website_repository"]:
        _block(
            blockers,
            "target_repository_mismatch",
            "website snapshot is for an unexpected repository",
        )
    target_base = target_snapshot.get("default_branch")
    if target_base != policy["target_base_branch"]:
        _block(
            blockers,
            "target_default_branch_mismatch",
            "website default branch does not match the reviewed policy",
        )
    target_base_sha = target_snapshot.get("base_sha")
    if not isinstance(target_base_sha, str) or not HEAD_SHA.fullmatch(target_base_sha):
        _block(blockers, "invalid_target_base", "website base SHA is invalid")

    target_file = target_snapshot.get("file")
    if not isinstance(target_file, dict) or not isinstance(
        target_file.get("exists"), bool
    ):
        raise ValueError("target snapshot must contain a file existence record")
    if target_file["exists"]:
        if (
            not isinstance(target_file.get("sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", target_file["sha"])
            or not isinstance(target_file.get("content_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", target_file["content_sha256"])
        ):
            _block(
                blockers,
                "invalid_target_file_snapshot",
                "existing website file snapshot is incomplete",
            )
    elif target_file.get("sha") is not None or target_file.get("content_sha256") is not None:
        _block(
            blockers,
            "invalid_target_file_snapshot",
            "absent website file snapshot unexpectedly contains content identity",
        )

    content_evidence = _validate_content(website_path, content, policy, blockers)
    title = pr.get("title") if isinstance(pr.get("title"), str) else ""
    body = pr.get("body") if isinstance(pr.get("body"), str) else ""
    labels = _label_names(pr)
    safe_head = head_sha if isinstance(head_sha, str) and HEAD_SHA.fullmatch(head_sha) else "invalid"
    safe_number = pr_number if isinstance(pr_number, int) and pr_number > 0 else 0
    branch = (
        f"{policy['branch_prefix']}-{safe_number}-{safe_head[:12]}-"
        f"{content_evidence['content_sha256'][:8]}"
    )
    signoff = policy["commit_signoff"]
    source_url = f"https://github.com/{source_repository}/pull/{safe_number}"
    action = {
        "action_authorized": not blockers,
        "target_repository": documentation["website_repository"],
        "target_base_branch": policy["target_base_branch"],
        "target_base_sha": target_base_sha,
        "target_path": website_path,
        "expected_target_file_sha": target_file.get("sha"),
        "expected_target_content_sha256": target_file.get("content_sha256"),
        "branch": branch,
        "content_sha256": content_evidence["content_sha256"],
        "commit_message": (
            f"docs: draft for {source_repository}#{safe_number}\n\n"
            f"Source: {source_url}\n"
            f"Source-Head: {safe_head}\n\n"
            f"Signed-off-by: {signoff['name']} <{signoff['email']}>"
        ),
        "pull_request_title": f"docs: draft for {source_repository}#{safe_number}",
        "pull_request_body": (
            "This is a maintainer-supplied documentation draft created by "
            "Kyvernaut for:\n\n"
            f"- Source PR: {source_url}\n"
            f"- Source head: `{safe_head}`\n"
            f"- Target file: `{website_path}`\n\n"
            "The pull request is intentionally a draft. Website maintainers "
            "must review the content, rendering, links, and release applicability "
            "before marking it ready."
        ),
        "draft": True,
    }
    evidence = {
        "dispatcher_permission": actor_permission,
        "source_repository": source_repository,
        "source_pr_number": pr_number,
        "source_state": pr.get("state"),
        "source_draft": bool(pr.get("draft")),
        "source_base_ref": base_ref,
        "source_head_sha": head_sha,
        "source_title_sha256": _sha256(title),
        "source_body_sha256": _sha256(body),
        "source_labels": labels,
        "changed_files": normalized_files,
        "changed_files_sha256": _sha256(
            json.dumps(normalized_files, separators=(",", ":"))
        ),
        "documentation_decision": docs_decision,
        "target_snapshot": target_snapshot,
        "content": content_evidence,
    }
    material = {
        "run_id": run_id,
        "mode": config["mode"],
        "blockers": blockers,
        "evidence": evidence,
        "action": action,
    }
    decision_id = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return {
        "schema_version": 1,
        "decision_id": decision_id,
        "run_id": run_id,
        "mode": config["mode"],
        "blockers": blockers,
        "evidence": evidence,
        "action": action,
    }


def main() -> int:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--target-snapshot", type=Path, required=True)
    parser.add_argument("--website-path-env", required=True)
    parser.add_argument("--content-env", required=True)
    parser.add_argument("--actor-permission", required=True)
    parser.add_argument(
        "--config", type=Path, default=root / ".github/ai-maintainer.yaml"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        event = json.loads(args.event.read_text(encoding="utf-8"))
        changed_files = json.loads(args.changed_files.read_text(encoding="utf-8"))
        target_snapshot = json.loads(
            args.target_snapshot.read_text(encoding="utf-8")
        )
        website_path = os.environ[args.website_path_env]
        content = os.environ[args.content_env]
        decision = evaluate_draft(
            event,
            changed_files,
            target_snapshot,
            website_path,
            content,
            load_config(args.config),
            actor_permission=args.actor_permission,
            environment=dict(os.environ),
            run_id=args.run_id,
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        output = args.output_dir / "docs-draft-decision.json"
        output.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write(
                    "action_authorized="
                    f"{str(decision['action']['action_authorized']).lower()}\n"
                )
                stream.write(f"decision_id={decision['decision_id']}\n")
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"documentation draft planning failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
