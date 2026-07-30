#!/usr/bin/env python3
"""Render Kyvernaut's comment-only PR scope and dependency decision report."""

import argparse
import html
import json
import os
import sys
from pathlib import Path

from change_metadata import evaluate_event as evaluate_change_metadata
from change_metadata import load_manifest as load_change_metadata
from dependency_pr import evaluate, load_config
from docs_requirement import evaluate_docs
from pr_hygiene import evaluate_hygiene
from scope_tests import load_manifest, render, scope


COMMENT_MARKER = "<!-- kyvernaut:pr-advisor:v1 -->"


def load_changed_files(path: Path) -> list[str]:
    """Read GitHub's list-files JSON without passing filenames through a shell."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("files JSON must be a list")
    files = []
    for index, item in enumerate(data):
        filename = item.get("filename") if isinstance(item, dict) else item
        if not isinstance(filename, str) or not filename:
            raise ValueError(f"files JSON entry {index} has no valid filename")
        files.append(filename)
    return files


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def _escaped_lines(values: list[str], empty: str) -> list[str]:
    if not values:
        return [empty]
    return [f"- <code>{html.escape(value)}</code>" for value in values]


def render_comment(
    plan: dict,
    decision: dict,
    hygiene: dict,
    documentation: dict,
    change_metadata: dict,
    *,
    run_url: str | None = None,
) -> str:
    """Render untrusted repository paths as escaped HTML, never raw Markdown."""
    lines = [
        COMMENT_MARKER,
        "## Kyvernaut shadow report",
        "",
        "> Advisory only. This workflow cannot merge, push, approve, or change labels.",
        "",
        "| Signal | Result |",
        "|---|---|",
        f"| Overall risk | **{plan['risk'].upper()}** |",
        f"| Human review required | {_yes_no(plan['requires_human_review'])} |",
        f"| Codegen verification required | {_yes_no(plan['codegen_verify_required'])} |",
        f"| Dependency scope candidate | {_yes_no(plan['auto_merge_eligible'])} |",
        f"| Dependency recommendation | **{html.escape(decision['recommendation'])}** |",
        f"| Action authorized | **{_yes_no(decision['action_authorized'])}** |",
        f"| Decision ID | <code>{html.escape(decision['decision_id'])}</code> |",
        f"| PR hygiene recommendations | **{len(hygiene['recommendations'])}** |",
        f"| Hygiene decision ID | <code>{html.escape(hygiene['decision_id'])}</code> |",
        f"| Documentation requirement | **{html.escape(documentation['status'])}** |",
        f"| Documentation decision ID | <code>{html.escape(documentation['decision_id'])}</code> |",
        f"| Documentation-only diff | "
        f"{_yes_no(change_metadata['evidence']['classifications']['documentation_only']['value'])} |",
        f"| Generated-only diff | "
        f"{_yes_no(change_metadata['evidence']['classifications']['generated_only']['value'])} |",
        f"| API change | "
        f"{_yes_no(change_metadata['evidence']['classifications']['api_change']['value'])} |",
        f"| Change metadata complete | **{_yes_no(change_metadata['metadata_complete'])}** |",
        f"| Change metadata decision ID | "
        f"<code>{html.escape(change_metadata['decision_id'])}</code> |",
        "",
        "<details>",
        "<summary>Suggested unit-test packages</summary>",
        "",
        *_escaped_lines(plan["unit_test_packages"], "No unit-test package was selected."),
        "",
        "</details>",
        "",
        "<details>",
        "<summary>Suggested chainsaw suites</summary>",
        "",
        *_escaped_lines(plan["conformance_suites"], "No chainsaw suite was selected."),
        "",
        "</details>",
    ]
    if plan["cli_suites"]:
        lines.extend(["", "- Run <code>make test-cli</code>."])
    if plan["codegen_verify_required"]:
        lines.extend(
            [
                "",
                "- Run <code>make codegen-all-code &amp;&amp; make verify-codegen</code>.",
            ]
        )
    coverage_warnings = plan["low_confidence_files"] + plan["unmatched_files"]
    if coverage_warnings:
        lines.extend(
            [
                "",
                "<details>",
                "<summary>Coverage warnings (human confirmation required)</summary>",
                "",
                *_escaped_lines(coverage_warnings, ""),
                "",
                "</details>",
            ]
        )
    if decision["blockers"]:
        lines.extend(
            [
                "",
                "<details>",
                "<summary>Dependency-policy blockers</summary>",
                "",
            ]
        )
        for blocker in decision["blockers"]:
            code = html.escape(blocker["code"])
            detail = html.escape(blocker["detail"])
            lines.append(f"- <code>{code}</code>: {detail}")
        lines.extend(["", "</details>"])
    if hygiene["recommendations"]:
        lines.extend(
            [
                "",
                "<details>",
                "<summary>PR hygiene recommendations</summary>",
                "",
            ]
        )
        for recommendation in hygiene["recommendations"]:
            action = html.escape(recommendation["action"])
            reason = html.escape(recommendation["reason"])
            lines.append(f"- <code>{action}</code>: {reason}")
        lines.extend(["", "</details>"])
    if documentation["status"] == "missing":
        lines.extend(
            [
                "",
                "### Documentation follow-up required",
                "",
                "This diff touches configured user-facing paths but includes no in-repo "
                "documentation change, `kyverno/website` issue/PR link, or reviewed exemption.",
                "Follow `.github/pr_documentation.md` and add the resulting link to the PR.",
            ]
        )
    if change_metadata["blockers"]:
        lines.extend(
            [
                "",
                "<details>",
                "<summary>Structured change-metadata blockers</summary>",
                "",
            ]
        )
        for blocker in change_metadata["blockers"]:
            lines.append(
                f"- <code>{html.escape(blocker['code'])}</code>: "
                f"{html.escape(blocker['detail'])}"
            )
        lines.extend(["", "</details>"])
    if change_metadata["evidence"]["suggested_labels"]:
        lines.extend(
            [
                "",
                "<details>",
                "<summary>Derived stable change labels</summary>",
                "",
                *_escaped_lines(
                    change_metadata["evidence"]["suggested_labels"],
                    "",
                ),
                "",
                "</details>",
            ]
        )

    lines.extend(
        [
            "",
            f"Changed files evaluated: **{len(plan['changed_files'])}**.",
            "The complete machine-readable plan and decision are retained as workflow artifacts.",
        ]
    )
    if run_url:
        lines.append(f"[Open the audit run]({html.escape(run_url, quote=True)}).")
    return "\n".join(lines) + "\n"


def build_report(
    event: dict,
    files: list[str],
    config: dict,
    manifest: dict,
    change_manifest: dict,
    *,
    ci_state: str,
    mergeable_state: str,
    environment: dict[str, str],
    run_id: str | None,
    run_url: str | None,
) -> tuple[dict, dict, dict, dict, dict, str]:
    plan = scope(files, manifest["rules"])
    decision = evaluate(
        event,
        files,
        config,
        manifest["rules"],
        ci_state=ci_state,
        mergeable_state=mergeable_state,
        environment=environment,
        run_id=run_id,
    )
    hygiene = evaluate_hygiene(
        event,
        config,
        mergeable_state=mergeable_state,
        environment=environment,
        run_id=run_id,
    )
    documentation = evaluate_docs(event, files, config, run_id=run_id)
    change_metadata = evaluate_change_metadata(event, files, change_manifest)
    return plan, decision, hygiene, documentation, change_metadata, render_comment(
        plan,
        decision,
        hygiene,
        documentation,
        change_metadata,
        run_url=run_url,
    )


def main() -> int:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--files-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=root / ".github/ai-maintainer.yaml")
    parser.add_argument("--map", type=Path, default=Path(__file__).parent / "path-test-map.yaml")
    parser.add_argument(
        "--change-metadata",
        type=Path,
        default=Path(__file__).parent / "change-metadata.yaml",
    )
    parser.add_argument("--ci-state", default="unknown")
    parser.add_argument("--mergeable-state", default="unknown")
    parser.add_argument("--run-id")
    parser.add_argument("--run-url")
    parser.add_argument(
        "--github-output",
        type=Path,
        help="Optional GitHub Actions output file used to expose assistant_enabled",
    )
    args = parser.parse_args()

    try:
        event = json.loads(args.event.read_text(encoding="utf-8"))
        files = load_changed_files(args.files_json)
        config = load_config(args.config)
        manifest = load_manifest(args.map)
        change_manifest = load_change_metadata(args.change_metadata)
        plan, decision, hygiene, documentation, change_metadata, comment = build_report(
            event,
            files,
            config,
            manifest,
            change_manifest,
            ci_state=args.ci_state,
            mergeable_state=args.mergeable_state,
            environment=dict(os.environ),
            run_id=args.run_id,
            run_url=args.run_url,
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "scope-plan.json").write_text(
            json.dumps(plan, indent=2) + "\n",
            encoding="utf-8",
        )
        (args.output_dir / "dependency-decision.json").write_text(
            json.dumps(decision, indent=2) + "\n",
            encoding="utf-8",
        )
        (args.output_dir / "pr-hygiene-decision.json").write_text(
            json.dumps(hygiene, indent=2) + "\n",
            encoding="utf-8",
        )
        (args.output_dir / "documentation-decision.json").write_text(
            json.dumps(documentation, indent=2) + "\n",
            encoding="utf-8",
        )
        (args.output_dir / "change-metadata-decision.json").write_text(
            json.dumps(change_metadata, indent=2) + "\n",
            encoding="utf-8",
        )
        (args.output_dir / "comment.md").write_text(comment, encoding="utf-8")
        (args.output_dir / "scope-plan.txt").write_text(render(plan) + "\n", encoding="utf-8")
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write(f"assistant_enabled={str(config['enabled']).lower()}\n")
                stream.write(f"decision_id={decision['decision_id']}\n")
                stream.write(f"hygiene_decision_id={hygiene['decision_id']}\n")
                stream.write(f"documentation_decision_id={documentation['decision_id']}\n")
                stream.write(f"change_metadata_decision_id={change_metadata['decision_id']}\n")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"shadow report generation failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
