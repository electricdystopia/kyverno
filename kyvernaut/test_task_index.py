import copy
from pathlib import Path

from task_index import load_index, make_targets, validate_repository, validate_schema


ROOT = Path(__file__).parents[1]
INDEX = load_index(Path(__file__).parent / "task-index.yaml")


def test_task_index_matches_repository():
    assert validate_repository(INDEX, ROOT) == []


def test_every_indexed_make_command_targets_makefile():
    targets = make_targets(ROOT / "Makefile")
    for task in INDEX["tasks"]:
        if task["argv"][0] == "make":
            assert task["argv"][1] in targets


def test_destructive_tasks_are_never_autonomous():
    for task in INDEX["tasks"]:
        if task["destructive"]:
            assert task["automation"] == "never"


def test_schema_rejects_destructive_safe_task():
    index = copy.deepcopy(INDEX)
    index["tasks"][0]["destructive"] = True
    index["tasks"][0]["automation"] = "safe"
    assert any("destructive tasks" in error for error in validate_schema(index))


def test_repository_validation_rejects_stale_make_target():
    index = copy.deepcopy(INDEX)
    index["tasks"][0]["argv"] = ["make", "target-that-does-not-exist"]
    assert any("does not exist" in error for error in validate_repository(index, ROOT))
