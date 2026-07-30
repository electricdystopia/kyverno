#!/usr/bin/env python3
"""Query and validate Kyvernaut's machine-readable developer task index."""

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

import yaml


VALID_CATEGORIES = {"automation", "build", "cluster", "codegen", "image", "lint", "setup", "test"}
VALID_AUTOMATION = {"safe", "review", "never"}
BOOLEAN_FIELDS = (
    "mutates_worktree",
    "destructive",
    "requires_network",
    "requires_cluster",
)
MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*):")


def load_index(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    errors = validate_schema(data)
    if errors:
        raise ValueError("invalid task index:\n  - " + "\n  - ".join(errors))
    return data


def validate_schema(data: dict) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return ["document must be a mapping"]
    if data.get("version") != 1:
        errors.append("version must be 1")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return errors + ["tasks must be a non-empty list"]

    ids = set()
    for index, task in enumerate(tasks):
        where = f"tasks[{index}]"
        if not isinstance(task, dict):
            errors.append(f"{where} must be a mapping")
            continue
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            errors.append(f"{where}.id must be a non-empty string")
            task_id = where
        elif task_id in ids:
            errors.append(f"duplicate task id: {task_id}")
        ids.add(task_id)

        if task.get("category") not in VALID_CATEGORIES:
            errors.append(f"task {task_id}: invalid category {task.get('category')!r}")
        argv = task.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and arg for arg in argv):
            errors.append(f"task {task_id}: argv must be a non-empty string list")
        if not isinstance(task.get("description"), str) or not task["description"]:
            errors.append(f"task {task_id}: description must be a non-empty string")
        if task.get("automation") not in VALID_AUTOMATION:
            errors.append(f"task {task_id}: invalid automation policy {task.get('automation')!r}")
        for field in BOOLEAN_FIELDS:
            if not isinstance(task.get(field), bool):
                errors.append(f"task {task_id}: {field} must be a boolean")
        if task.get("destructive") and task.get("automation") != "never":
            errors.append(f"task {task_id}: destructive tasks must set automation to 'never'")
    return errors


def make_targets(makefile: Path) -> set[str]:
    targets = set()
    for line in makefile.read_text(encoding="utf-8").splitlines():
        match = MAKE_TARGET.match(line)
        if match:
            targets.add(match.group(1))
    return targets


def validate_repository(index: dict, repo_root: Path) -> list[str]:
    errors = []
    targets = make_targets(repo_root / "Makefile")
    for task in index["tasks"]:
        argv = task["argv"]
        if argv[0] == "make":
            if len(argv) < 2:
                errors.append(f"task {task['id']}: make command has no target")
            elif argv[1] not in targets:
                errors.append(f"task {task['id']}: Makefile target {argv[1]!r} does not exist")
        elif argv[0] == "python3":
            for arg in argv[1:]:
                if arg.endswith(".py") and not (repo_root / arg).is_file():
                    errors.append(f"task {task['id']}: script {arg!r} does not exist")
        else:
            errors.append(f"task {task['id']}: unsupported executable {argv[0]!r}")
    return errors


def render(tasks: list[dict]) -> str:
    lines = []
    for task in tasks:
        command = shlex.join(task["argv"])
        flags = [task["automation"]]
        if task["mutates_worktree"]:
            flags.append("writes")
        if task["destructive"]:
            flags.append("destructive")
        if task["requires_network"]:
            flags.append("network")
        if task["requires_cluster"]:
            flags.append("cluster")
        lines.append(f"{task['id']:<28} {command}")
        lines.append(f"  [{', '.join(flags)}] {task['description']}")
    return "\n".join(lines)


def main() -> int:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=Path(__file__).parent / "task-index.yaml")
    parser.add_argument("--repo", type=Path, default=root)
    parser.add_argument("--category", choices=sorted(VALID_CATEGORIES))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()

    try:
        index = load_index(args.index)
        if args.validate:
            errors = validate_repository(index, args.repo)
            if errors:
                raise ValueError("task index repository validation failed:\n  - " + "\n  - ".join(errors))
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1

    tasks = index["tasks"]
    if args.category:
        tasks = [task for task in tasks if task["category"] == args.category]
    if args.json:
        print(json.dumps({"version": index["version"], "tasks": tasks}, indent=2))
    else:
        print(render(tasks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
