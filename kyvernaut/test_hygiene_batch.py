import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dependency_pr import load_config
from hygiene_batch import COMMENT_MARKER, build_batch, render_hygiene_comment


ROOT = Path(__file__).parents[1]
CONFIG = load_config(ROOT / ".github/ai-maintainer.yaml")
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def pr(number: int, age_days: int, *, author="contributor", reviewers=None):
    updated = NOW - timedelta(days=age_days)
    return {
        "number": number,
        "html_url": f"https://github.com/kyverno/kyverno/pull/{number}",
        "updated_at": updated.isoformat(),
        "draft": False,
        "maintainer_can_modify": True,
        "user": {"login": author},
        "base": {"ref": "main", "repo": {"full_name": "kyverno/kyverno"}},
        "head": {"ref": f"feature-{number}", "repo": {"full_name": "kyverno/kyverno"}},
        "labels": [],
        "requested_reviewers": [{"login": value} for value in (reviewers or [])],
    }


def test_batch_selects_oldest_and_caps_comments():
    config = copy.deepcopy(CONFIG)
    config["pr_hygiene"]["max_nudges_per_run"] = 2
    batch = build_batch(
        [pr(1, 15), pr(2, 40), pr(3, 20)],
        config,
        now=NOW,
        environment={},
        run_id="run",
        run_url=None,
    )
    assert batch["eligible_nudges"] == 3
    assert [item["pull_request_number"] for item in batch["comments"]] == [2, 3]


def test_batch_ignores_active_prs():
    batch = build_batch(
        [pr(1, 1)],
        CONFIG,
        now=NOW,
        environment={},
        run_id=None,
        run_url=None,
    )
    assert batch["comments"] == []


def test_comment_mentions_valid_github_logins_only():
    batch = build_batch(
        [pr(1, 20, author="<script>")],
        CONFIG,
        now=NOW,
        environment={},
        run_id=None,
        run_url=None,
    )
    comment = batch["comments"][0]["body"]
    assert COMMENT_MARKER in comment
    assert "@<script>" not in comment
    assert "PR author" in comment


def test_reviewer_nudge_mentions_requested_reviewer():
    batch = build_batch(
        [pr(1, 8, reviewers=["reviewer-name"])],
        CONFIG,
        now=NOW,
        environment={},
        run_id=None,
        run_url=None,
    )
    assert "@reviewer-name" in batch["comments"][0]["body"]


def test_rendered_comment_states_shadow_boundary():
    batch = build_batch(
        [pr(1, 20)],
        CONFIG,
        now=NOW,
        environment={},
        run_id=None,
        run_url=None,
    )
    decision = batch["decisions"][0]
    comment = render_hygiene_comment(decision)
    assert "no branch, review, label, or merge state was changed" in comment
