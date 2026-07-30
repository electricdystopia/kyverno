import copy
from pathlib import Path

import pytest

from dependency_batch import build_batch
from dependency_pr import load_config
from scope_tests import load_rules


ROOT = Path(__file__).parents[1]
CONFIG = load_config(ROOT / ".github/ai-maintainer.yaml")
RULES = load_rules(Path(__file__).parent / "path-test-map.yaml")


def candidate(number=42, *, files=None, ci_state="success", mergeable_state="clean", **pr):
    pull_request = {
        "number": number,
        "html_url": f"https://github.com/kyverno/kyverno/pull/{number}",
        "title": "Bump example.com/module from 1.2.3 to 1.2.4",
        "body": "Routine dependency update.",
        "state": "open",
        "draft": False,
        "user": {"login": "dependabot[bot]"},
        "base": {"ref": "main"},
        "head": {"sha": f"{number:040x}"},
        "labels": [],
    }
    pull_request.update(pr)
    return {
        "pull_request": pull_request,
        "changed_files": files or ["go.mod", "go.sum"],
        "ci_state": ci_state,
        "mergeable_state": mergeable_state,
    }


def active_config():
    config = copy.deepcopy(CONFIG)
    config["mode"] = "active"
    return config


def test_shadow_batch_audits_but_never_emits_merge_actions():
    batch = build_batch(
        [candidate()],
        CONFIG,
        RULES,
        environment={},
        run_id="123",
        run_url="https://example.test/run/123",
    )
    assert batch["actions"] == []
    assert batch["decisions"][0]["recommendation"] == "would_merge"
    assert batch["decisions"][0]["action_authorized"] is False


def test_active_batch_caps_actions_and_binds_each_to_head_sha():
    config = active_config()
    config["dependency_updates"]["max_merges_per_run"] = 2
    batch = build_batch(
        [candidate(number) for number in range(1, 5)],
        config,
        RULES,
        environment={},
        run_id="rate-limit",
        run_url=None,
    )
    assert [action["pull_request_number"] for action in batch["actions"]] == [1, 2]
    assert batch["authorized_before_rate_limit"] == 4
    assert batch["rate_limited_actions"] == 2
    assert all(len(action["head_sha"]) == 40 for action in batch["actions"])
    assert batch["merge_method"] == "squash"


def test_batch_keeps_unsafe_candidates_out_of_action_list():
    batch = build_batch(
        [
            candidate(1, ci_state="failure"),
            candidate(2, files=["go.mod", "pkg/engine/api.go"]),
            candidate(3, body="This contains a backward incompatible API change."),
            candidate(4, head={"sha": "not-a-sha"}),
        ],
        active_config(),
        RULES,
        environment={},
        run_id="unsafe",
        run_url=None,
    )
    assert batch["actions"] == []
    blocker_sets = [
        {blocker["code"] for blocker in decision["blockers"]}
        for decision in batch["decisions"]
    ]
    assert "ci_not_green" in blocker_sets[0]
    assert "files_not_allowed" in blocker_sets[1]
    assert "breaking_change_signal" in blocker_sets[2]
    assert "invalid_head_sha" in blocker_sets[3]


def test_kill_switch_blocks_every_action_in_active_mode():
    batch = build_batch(
        [candidate()],
        active_config(),
        RULES,
        environment={"KYVERNAUT_PAUSED": "yes"},
        run_id="paused",
        run_url=None,
    )
    assert batch["actions"] == []
    assert "kill_switch_active" in {
        blocker["code"] for blocker in batch["decisions"][0]["blockers"]
    }


@pytest.mark.parametrize(
    "candidates",
    [
        [{}],
        [{"pull_request": {}, "changed_files": [], "ci_state": "success", "mergeable_state": "clean"}],
        [candidate(), candidate()],
        [candidate(ci_state="maybe")],
    ],
)
def test_malformed_or_duplicate_candidate_evidence_fails_closed(candidates):
    with pytest.raises(ValueError):
        build_batch(
            candidates,
            active_config(),
            RULES,
            environment={},
            run_id="invalid",
            run_url=None,
        )
