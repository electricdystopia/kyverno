import copy
import json
import sys
from pathlib import Path

from change_metadata import load_manifest as load_change_metadata
from dependency_pr import load_config
from pr_shadow import COMMENT_MARKER, build_report, load_changed_files, main, render_comment
from scope_tests import load_manifest


ROOT = Path(__file__).parents[1]
CONFIG = load_config(ROOT / ".github/ai-maintainer.yaml")
MANIFEST = load_manifest(Path(__file__).parent / "path-test-map.yaml")
CHANGE_MANIFEST = load_change_metadata(Path(__file__).parent / "change-metadata.yaml")


def event(title="Bump example.com/module from 1.2.3 to 1.2.4"):
    return {
        "number": 7,
        "pull_request": {
            "number": 7,
            "html_url": "https://github.com/kyverno/kyverno/pull/7",
            "title": title,
            "body": "",
            "state": "open",
            "updated_at": "2026-07-30T00:00:00Z",
            "draft": False,
            "maintainer_can_modify": True,
            "user": {"login": "dependabot[bot]"},
            "base": {"ref": "main", "repo": {"full_name": "kyverno/kyverno"}},
            "head": {
                "ref": "deps",
                "sha": "a" * 40,
                "repo": {"full_name": "kyverno/kyverno"},
            },
            "labels": [],
            "requested_reviewers": [],
        },
    }


def test_shadow_report_never_authorizes_action():
    plan, decision, hygiene, documentation, change_metadata, comment = build_report(
        event(),
        ["go.mod", "go.sum"],
        CONFIG,
        MANIFEST,
        CHANGE_MANIFEST,
        ci_state="success",
        mergeable_state="clean",
        environment={},
        run_id="123",
        run_url="https://github.com/kyverno/kyverno/actions/runs/123",
    )
    assert plan["auto_merge_eligible"] is True
    assert decision["recommendation"] == "would_merge"
    assert decision["action_authorized"] is False
    assert hygiene["action_authorized"] is False
    assert documentation["status"] == "not_required"
    assert change_metadata["metadata_complete"] is True
    assert COMMENT_MARKER in comment
    assert "Action authorized | **No**" in comment
    assert decision["decision_id"] in comment


def test_report_escapes_untrusted_paths_and_blocker_text():
    malicious = "pkg/new/<img src=x onerror=alert(1)>.go"
    plan, decision, hygiene, documentation, change_metadata, _ = build_report(
        event(),
        [malicious],
        CONFIG,
        MANIFEST,
        CHANGE_MANIFEST,
        ci_state="failure",
        mergeable_state="dirty",
        environment={},
        run_id=None,
        run_url=None,
    )
    comment = render_comment(plan, decision, hygiene, documentation, change_metadata)
    assert malicious not in comment
    assert "&lt;img src=x onerror=alert(1)&gt;" in comment


def test_load_changed_files_accepts_github_objects(tmp_path):
    path = tmp_path / "files.json"
    path.write_text(json.dumps([{"filename": "go.mod"}, {"filename": "go.sum"}]))
    assert load_changed_files(path) == ["go.mod", "go.sum"]


def test_disabled_policy_still_reports_but_cannot_authorize():
    config = copy.deepcopy(CONFIG)
    config["enabled"] = False
    _, decision, _, _, _, _ = build_report(
        event(),
        ["go.mod", "go.sum"],
        config,
        MANIFEST,
        CHANGE_MANIFEST,
        ci_state="success",
        mergeable_state="clean",
        environment={},
        run_id=None,
        run_url=None,
    )
    assert decision["action_authorized"] is False
    assert "assistant_disabled" in {blocker["code"] for blocker in decision["blockers"]}


def test_kill_switch_is_present_in_audit_decision():
    _, decision, _, _, _, _ = build_report(
        event(),
        ["go.mod", "go.sum"],
        CONFIG,
        MANIFEST,
        CHANGE_MANIFEST,
        ci_state="success",
        mergeable_state="clean",
        environment={"KYVERNAUT_PAUSED": "yes"},
        run_id=None,
        run_url=None,
    )
    assert decision["evidence"]["kill_switch"]["active"] is True
    assert decision["action_authorized"] is False


def test_api_diff_surfaces_missing_compatibility_metadata():
    api_event = event("feat(api): add a field")
    (
        _,
        _,
        _,
        _,
        change_metadata,
        comment,
    ) = build_report(
        api_event,
        ["api/kyverno/v1/spec_types.go"],
        CONFIG,
        MANIFEST,
        CHANGE_MANIFEST,
        ci_state="success",
        mergeable_state="clean",
        environment={},
        run_id="api-metadata",
        run_url=None,
    )
    assert change_metadata["metadata_complete"] is False
    assert "api_compatibility_undeclared" in comment
    assert "kind/api-change" in comment


def test_cli_writes_complete_audit_bundle(tmp_path, monkeypatch):
    event_path = tmp_path / "event.json"
    files_path = tmp_path / "files.json"
    output_dir = tmp_path / "audit"
    github_output = tmp_path / "github-output"
    event_path.write_text(json.dumps(event()), encoding="utf-8")
    files_path.write_text(
        json.dumps([{"filename": "go.mod"}, {"filename": "go.sum"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pr_shadow.py",
            "--event",
            str(event_path),
            "--files-json",
            str(files_path),
            "--output-dir",
            str(output_dir),
            "--ci-state",
            "success",
            "--mergeable-state",
            "clean",
            "--run-id",
            "99",
            "--github-output",
            str(github_output),
        ],
    )
    assert main() == 0
    assert {
        "scope-plan.json",
        "dependency-decision.json",
        "pr-hygiene-decision.json",
        "documentation-decision.json",
        "change-metadata-decision.json",
        "comment.md",
        "scope-plan.txt",
    } == {path.name for path in output_dir.iterdir()}
    decision = json.loads((output_dir / "dependency-decision.json").read_text())
    assert decision["recommendation"] == "would_merge"
    assert decision["action_authorized"] is False
    assert "assistant_enabled=true" in github_output.read_text()
