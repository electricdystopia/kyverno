#!/usr/bin/env python3
"""Validate Kyverno's reviewed Go module-boundary decision against the tree."""

import argparse
import re
import sys
from pathlib import Path, PurePosixPath

import yaml


MODULE_LINE = re.compile(r"^module\s+(\S+)\s*$", re.MULTILINE)
VALID_STRATEGIES = {"federated", "monorepo"}
VALID_WORKSPACE_POLICIES = {"independent-modules", "go-workspace"}
VALID_RECOMMENDATIONS = {"retain-current-boundaries", "migrate"}
VALID_ROLES = {
    "product-runtime",
    "isolated-build-tool",
    "published-api-contract",
    "published-integration-sdk",
}
IGNORED_PARTS = {".git", ".tools", "vendor"}


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
    )


def validate_schema(data: dict) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return ["document must be a mapping"]
    if data.get("version") != 1:
        errors.append("version must be 1")
    decision = data.get("decision")
    if not isinstance(decision, dict):
        return errors + ["decision must be a mapping"]
    if decision.get("repository_strategy") not in VALID_STRATEGIES:
        errors.append("decision.repository_strategy is invalid")
    if decision.get("local_workspace_policy") not in VALID_WORKSPACE_POLICIES:
        errors.append("decision.local_workspace_policy is invalid")
    if decision.get("recommendation") not in VALID_RECOMMENDATIONS:
        errors.append("decision.recommendation is invalid")
    rationale = decision.get("rationale_document")
    if not isinstance(rationale, str) or not _safe_relative_path(rationale):
        errors.append("decision.rationale_document must be a repository-relative path")

    local_modules = data.get("local_modules")
    if not isinstance(local_modules, list) or not local_modules:
        return errors + ["local_modules must be a non-empty list"]
    paths = set()
    module_names = set()
    for index, module in enumerate(local_modules):
        where = f"local_modules[{index}]"
        if not isinstance(module, dict):
            errors.append(f"{where} must be a mapping")
            continue
        path = module.get("path")
        name = module.get("module")
        if not isinstance(path, str) or not _safe_relative_path(path):
            errors.append(f"{where}.path must be repository-relative")
        elif path in paths:
            errors.append(f"duplicate local module path: {path}")
        paths.add(path)
        if not isinstance(name, str) or not name:
            errors.append(f"{where}.module must be non-empty")
        elif name in module_names:
            errors.append(f"duplicate local module name: {name}")
        module_names.add(name)
        if module.get("role") not in VALID_ROLES:
            errors.append(f"{where}.role is invalid")

    external = data.get("external_boundaries")
    if not isinstance(external, list) or not external:
        return errors + ["external_boundaries must be a non-empty list"]
    external_names = set()
    for index, boundary in enumerate(external):
        where = f"external_boundaries[{index}]"
        if not isinstance(boundary, dict):
            errors.append(f"{where} must be a mapping")
            continue
        name = boundary.get("module")
        if not isinstance(name, str) or not name:
            errors.append(f"{where}.module must be non-empty")
        elif name in external_names:
            errors.append(f"duplicate external module: {name}")
        external_names.add(name)
        if boundary.get("role") not in VALID_ROLES:
            errors.append(f"{where}.role is invalid")
    if module_names & external_names:
        errors.append("a module cannot be both local and external")

    triggers = data.get("revisit_when")
    if (
        not isinstance(triggers, list)
        or not triggers
        or not all(isinstance(value, str) and value for value in triggers)
    ):
        errors.append("revisit_when must be a non-empty string list")
    return errors


def load_manifest(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid module-boundary YAML: {error}") from error
    errors = validate_schema(data)
    if errors:
        raise ValueError("invalid module-boundary manifest:\n  - " + "\n  - ".join(errors))
    return data


def _module_name(go_mod: Path) -> str | None:
    match = MODULE_LINE.search(go_mod.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def discover_modules(repo_root: Path) -> dict[str, str | None]:
    root = repo_root.resolve()
    modules = {}
    for go_mod in root.rglob("go.mod"):
        relative = go_mod.relative_to(root)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        module_path = relative.parent.as_posix()
        modules["." if module_path == "." else module_path] = _module_name(go_mod)
    return dict(sorted(modules.items()))


def validate_repository(data: dict, repo_root: Path) -> list[str]:
    errors = []
    expected = {
        module["path"]: module["module"]
        for module in data["local_modules"]
    }
    observed = discover_modules(repo_root)
    if observed != expected:
        missing = sorted(set(expected) - set(observed))
        unexpected = sorted(set(observed) - set(expected))
        mismatched = sorted(
            path
            for path in set(expected) & set(observed)
            if expected[path] != observed[path]
        )
        if missing:
            errors.append("declared local module(s) missing: " + ", ".join(missing))
        if unexpected:
            errors.append("undeclared local module(s): " + ", ".join(unexpected))
        for path in mismatched:
            errors.append(
                f"module name mismatch at {path}: "
                f"expected {expected[path]!r}, observed {observed[path]!r}"
            )

    root = repo_root.resolve()
    rationale = root / data["decision"]["rationale_document"]
    if not rationale.is_file():
        errors.append("module-boundary rationale document does not exist")
    workspace_exists = (root / "go.work").exists()
    policy = data["decision"]["local_workspace_policy"]
    if policy == "independent-modules" and workspace_exists:
        errors.append("go.work exists but policy requires independent modules")
    if policy == "go-workspace" and not workspace_exists:
        errors.append("go.work is missing but policy requires a workspace")

    root_go_mod = (root / "go.mod").read_text(encoding="utf-8")
    for boundary in data["external_boundaries"]:
        name = re.escape(boundary["module"])
        if not re.search(rf"^\s*{name}\s+v\S+", root_go_mod, re.MULTILINE):
            errors.append(
                f"external boundary is not a versioned root dependency: "
                f"{boundary['module']}"
            )
    return errors


def main() -> int:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).parent / "module-boundaries.yaml",
    )
    parser.add_argument("--repo", type=Path, default=root)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.manifest)
        errors = validate_repository(manifest, args.repo)
        if errors:
            raise ValueError(
                "module-boundary repository validation failed:\n  - "
                + "\n  - ".join(errors)
            )
        if args.validate:
            print(
                f"Module boundaries are valid "
                f"({len(manifest['local_modules'])} local, "
                f"{len(manifest['external_boundaries'])} external)."
            )
    except (OSError, UnicodeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
