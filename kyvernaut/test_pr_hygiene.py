import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dependency_pr import load_config, validate_config
from pr_hygiene import evaluate_hygiene, parse_github_time


ROOT = Path(__file__).parents[1]
CONFIG = load_config(ROOT / ".github/ai-maintainer.yaml")
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def event(
    *,
    updated_at="2026-07-01T12:00:00Z",
    mergeable=True,
    maintainer_can_modify=True,
    labels=None,
    reviewers=None,
    draft=False,
    same_repo=True,
):
    base_repo = {"full_name": "kyverno/kyverno"}
    head_repo = {"full_name": "kyverno/kyverno" if same_repo else "contributor/kyverno"}
    return {
        "number": 9,
        "pull_request": {
            "number": 9,
            "html_url": "https://github.com/kyverno/kyverno/pull/9",
            "updated_at": updated_at,
            "draft": draft,
            "mergeable": mergeable,
            "maintainer_can_modify": maintainer_can_modify,
            "user": {"login": "contributor"},
            "base": {"ref": "main", "repo": base_repo},
            "head": {"ref": "feature", "repo": head_repo},
            "labels": [{"name": label} for label in (labels or [])],
            "requested_reviewers": [{"login": reviewer} for reviewer in (reviewers or [])],
        },
    }


def actions(decision):
    return {item["action"] for item in decision["recommendations"]}


def test_behind_stale_pr_recommends_update_and_author_nudge():
    decision = evaluate_hygiene(event(), CONFIG, mergeable_state="behind", now=NOW)
    assert actions(decision) == {"update_branch", "nudge_author"}
    assert decision["action_authorized"] is False


def test_fork_without_maintainer_permission_requests_author_update():
    decision = evaluate_hygiene(
        event(same_repo=False, maintainer_can_modify=False),
        CONFIG,
        mergeable_state="behind",
        now=NOW,
    )
    assert "request_author_update" in actions(decision)
    recommendation = next(
        item for item in decision["recommendations"] if item["action"] == "request_author_update"
    )
    assert recommendation["automatable"] is False


def test_requested_reviewers_are_nudged_before_author_stale_threshold():
    decision = evaluate_hygiene(
        event(updated_at="2026-07-20T12:00:00Z", reviewers=["reviewer"]),
        CONFIG,
        mergeable_state="clean",
        now=NOW,
    )
    assert actions(decision) == {"nudge_reviewers"}


@pytest.mark.parametrize("kwargs", [{"draft": True}, {"labels": ["Do-Not-Merge"]}])
def test_draft_or_hold_label_blocks_all_recommendations(kwargs):
    decision = evaluate_hygiene(event(**kwargs), CONFIG, mergeable_state="behind", now=NOW)
    assert decision["recommendations"] == []
    assert decision["blockers"]


def test_kill_switch_blocks_all_recommendations():
    decision = evaluate_hygiene(
        event(),
        CONFIG,
        mergeable_state="behind",
        now=NOW,
        environment={"KYVERNAUT_PAUSED": "on"},
    )
    assert decision["recommendations"] == []
    assert {item["code"] for item in decision["blockers"]} == {"kill_switch_active"}


def test_active_mode_only_authorizes_recommendation_after_gates():
    config = copy.deepcopy(CONFIG)
    config["mode"] = "active"
    decision = evaluate_hygiene(event(), config, mergeable_state="behind", now=NOW)
    assert decision["action_authorized"] is True


def test_config_rejects_nonpositive_hygiene_limits():
    config = copy.deepcopy(CONFIG)
    config["pr_hygiene"]["max_nudges_per_run"] = 0
    assert any("max_nudges_per_run" in error for error in validate_config(config))


def test_timestamp_must_have_timezone():
    with pytest.raises(ValueError):
        parse_github_time("2026-07-30T12:00:00")
