import copy
from pathlib import Path

import pytest

from dependency_pr import classify_update, evaluate, load_config, validate_config
from scope_tests import load_rules


ROOT = Path(__file__).parents[1]
CONFIG = load_config(ROOT / ".github/ai-maintainer.yaml")
RULES = load_rules(Path(__file__).parent / "path-test-map.yaml")


def event(
    *,
    author="dependabot[bot]",
    title="Bump example.com/module from 1.2.3 to 1.3.0",
    base="main",
    labels=None,
    draft=False,
    state="open",
    body="",
    head_sha="a" * 40,
):
    return {
        "number": 42,
        "pull_request": {
            "number": 42,
            "html_url": "https://github.com/kyverno/kyverno/pull/42",
            "title": title,
            "body": body,
            "draft": draft,
            "state": state,
            "user": {"login": author},
            "base": {"ref": base},
            "head": {"sha": head_sha},
            "labels": [{"name": label} for label in (labels or [])],
        },
    }


def enabled_config(mode="shadow"):
    config = copy.deepcopy(CONFIG)
    config["enabled"] = True
    config["mode"] = mode
    return config


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Bump mod from v1.2.3 to v1.2.4", "patch"),
        ("chore(deps): bump mod from 1.2.3 to 1.4.0", "minor"),
        ("Bump mod from 1.2.3 to 2.0.0", "major"),
        ("Bump the kubernetes group with 7 updates", "unknown"),
        ("Bump mod from v0.0.0-20260101-abc to v0.0.0-20260202-def", "unknown"),
    ],
)
def test_classify_update(title, expected):
    assert classify_update(title)["type"] == expected


def test_repository_policy_is_enabled_for_shadow_only_by_default():
    assert CONFIG["enabled"] is True
    assert CONFIG["mode"] == "shadow"


def test_shadow_mode_emits_would_merge_but_never_authorizes_action():
    decision = evaluate(
        event(),
        ["go.mod", "go.sum"],
        enabled_config(),
        RULES,
        ci_state="success",
        mergeable_state="clean",
        run_id="test-run",
    )
    assert decision["policy_eligible"] is True
    assert decision["recommendation"] == "would_merge"
    assert decision["action_authorized"] is False
    assert decision["decision_id"]


def test_disabled_policy_blocks_otherwise_clean_dependency_pr():
    config = copy.deepcopy(CONFIG)
    config["enabled"] = False
    decision = evaluate(
        event(),
        ["go.mod", "go.sum"],
        config,
        RULES,
        ci_state="success",
        mergeable_state="clean",
    )
    assert decision["recommendation"] == "do_not_merge"
    assert "assistant_disabled" in {blocker["code"] for blocker in decision["blockers"]}


@pytest.mark.parametrize(
    ("kwargs", "files", "ci_state", "mergeable_state", "blocker"),
    [
        ({"author": "contributor"}, ["go.mod", "go.sum"], "success", "clean", "untrusted_actor"),
        ({"title": "Bump mod from 1.2.3 to 2.0.0"}, ["go.mod", "go.sum"], "success", "clean", "update_type_not_allowed"),
        ({}, ["go.mod", "go.sum", "pkg/engine/api.go"], "success", "clean", "files_not_allowed"),
        ({"labels": ["hold"]}, ["go.mod", "go.sum"], "success", "clean", "hold_label"),
        ({"state": "closed"}, ["go.mod", "go.sum"], "success", "clean", "pr_not_open"),
        ({"body": "Release notes: BREAKING CHANGE for callers."}, ["go.mod", "go.sum"], "success", "clean", "breaking_change_signal"),
        ({}, ["go.mod", "go.sum"], "failure", "clean", "ci_not_green"),
        ({}, ["go.mod", "go.sum"], "success", "dirty", "not_mergeable"),
        ({"draft": True}, ["go.mod", "go.sum"], "success", "clean", "draft_pr"),
    ],
)
def test_policy_fails_closed(kwargs, files, ci_state, mergeable_state, blocker):
    decision = evaluate(
        event(**kwargs),
        files,
        enabled_config(),
        RULES,
        ci_state=ci_state,
        mergeable_state=mergeable_state,
    )
    assert decision["action_authorized"] is False
    assert blocker in {item["code"] for item in decision["blockers"]}


def test_kill_switch_blocks_action():
    decision = evaluate(
        event(),
        ["go.mod", "go.sum"],
        enabled_config("active"),
        RULES,
        ci_state="success",
        mergeable_state="clean",
        environment={"KYVERNAUT_PAUSED": " TRUE "},
    )
    assert decision["action_authorized"] is False
    assert "kill_switch_active" in {item["code"] for item in decision["blockers"]}


def test_hold_label_matching_is_case_insensitive():
    decision = evaluate(
        event(labels=["Hold"]),
        ["go.mod", "go.sum"],
        enabled_config("active"),
        RULES,
        ci_state="success",
        mergeable_state="clean",
    )
    assert "hold_label" in {item["code"] for item in decision["blockers"]}


def test_active_mode_can_only_authorize_after_every_gate_passes():
    decision = evaluate(
        event(title="Bump mod from 1.2.3 to 1.2.4"),
        ["go.mod", "go.sum"],
        enabled_config("active"),
        RULES,
        ci_state="success",
        mergeable_state="clean",
    )
    assert decision["policy_eligible"] is True
    assert decision["action_authorized"] is True
    assert decision["recommendation"] == "merge"


def test_config_rejects_major_auto_merge_policy():
    config = enabled_config()
    config["dependency_updates"]["allowed_update_types"].append("major")
    assert any("unsafe/unknown" in error for error in validate_config(config))


def test_config_rejects_uncapped_or_unsupported_executor_settings():
    config = enabled_config()
    config["dependency_updates"]["max_merges_per_run"] = 11
    config["dependency_updates"]["merge_method"] = "merge"
    config["dependency_updates"]["actors"].append("some-app[bot]")
    errors = validate_config(config)
    assert any("max_merges_per_run" in error for error in errors)
    assert any("merge_method" in error for error in errors)
    assert any("unsupported by the executor" in error for error in errors)
