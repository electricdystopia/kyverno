#!/usr/bin/env python3
"""Classify PR change metadata from reviewed path rules and explicit labels."""

import argparse
import fnmatch
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath

import yaml


VALID_STRATEGIES = {"all-paths", "any-path", "explicit-label"}
REQUIRED_CLASSIFICATIONS = {
    "documentation_only",
    "generated_only",
    "api_change",
    "breaking_api",
    "non_breaking_api",
}


def _safe_pattern(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
    )


def _safe_changed_path(value: str) -> bool:
    return (
        _safe_pattern(value)
        and not any(character in value for character in "*?[]")
        and value != "."
    )


def validate_schema(data: dict) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return ["document must be a mapping"]
    if data.get("version") != 1:
        errors.append("version must be 1")
    classifications = data.get("classifications")
    if not isinstance(classifications, dict):
        return errors + ["classifications must be a mapping"]
    missing = REQUIRED_CLASSIFICATIONS - set(classifications)
    extra = set(classifications) - REQUIRED_CLASSIFICATIONS
    if missing:
        errors.append("missing classifications: " + ", ".join(sorted(missing)))
    if extra:
        errors.append("unsupported classifications: " + ", ".join(sorted(extra)))

    labels = set()
    for name, classification in classifications.items():
        where = f"classifications.{name}"
        if not isinstance(classification, dict):
            errors.append(f"{where} must be a mapping")
            continue
        label = classification.get("label")
        if not isinstance(label, str) or not label or "\n" in label:
            errors.append(f"{where}.label must be a safe non-empty string")
        elif label.casefold() in labels:
            errors.append(f"duplicate classification label: {label}")
        else:
            labels.add(label.casefold())
        strategy = classification.get("strategy")
        if strategy not in VALID_STRATEGIES:
            errors.append(f"{where}.strategy is invalid")
        paths = classification.get("paths")
        if strategy == "explicit-label":
            if paths is not None:
                errors.append(f"{where} cannot define paths for explicit-label")
        elif (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(value, str) and _safe_pattern(value) for value in paths)
        ):
            errors.append(f"{where}.paths must be a non-empty safe pattern list")

    constraints = data.get("constraints")
    if not isinstance(constraints, dict):
        return errors + ["constraints must be a mapping"]
    api_constraint = constraints.get("api_change_requires_exactly_one")
    if api_constraint != ["breaking_api", "non_breaking_api"]:
        errors.append(
            "constraints.api_change_requires_exactly_one must contain "
            "breaking_api then non_breaking_api"
        )
    return errors


def load_manifest(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid change-metadata YAML: {error}") from error
    errors = validate_schema(data)
    if errors:
        raise ValueError("invalid change-metadata manifest:\n  - " + "\n  - ".join(errors))
    return data


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _label_names(pull_request: dict) -> list[str]:
    labels = []
    for value in pull_request.get("labels", []):
        name = value.get("name") if isinstance(value, dict) else value
        if isinstance(name, str):
            labels.append(name)
    return labels


def classify(files: list[str], labels: list[str], manifest: dict) -> dict:
    normalized_labels = {label.casefold() for label in labels}
    invalid_files = [
        path
        for path in files
        if not isinstance(path, str) or not _safe_changed_path(path)
    ]
    string_files = [path for path in files if isinstance(path, str)]
    duplicate_files = len(string_files) != len(set(string_files))
    inference_allowed = not invalid_files and not duplicate_files
    classifications = {}
    suggested_labels = []
    for name, policy in manifest["classifications"].items():
        label = policy["label"]
        declared = label.casefold() in normalized_labels
        strategy = policy["strategy"]
        if strategy == "explicit-label":
            inferred = False
            value = declared
        else:
            matches = (
                [_matches(path, policy["paths"]) for path in files]
                if inference_allowed
                else []
            )
            inferred = inference_allowed and bool(matches) and (
                all(matches) if strategy == "all-paths" else any(matches)
            )
            value = inferred
            if inferred and not declared:
                suggested_labels.append(label)
        classifications[name] = {
            "value": value,
            "strategy": strategy,
            "label": label,
            "declared": declared,
            "inferred": inferred,
        }

    blockers = []
    if not files:
        blockers.append(
            {"code": "empty_diff", "detail": "change metadata requires changed files"}
        )
    if invalid_files:
        blockers.append(
            {
                "code": "invalid_changed_path",
                "detail": "changed paths must be safe repository-relative filenames",
            }
        )
    if duplicate_files:
        blockers.append(
            {
                "code": "duplicate_changed_file",
                "detail": "changed-file evidence contains duplicate paths",
            }
        )
    if classifications["api_change"]["value"]:
        declarations = [
            name
            for name in manifest["constraints"]["api_change_requires_exactly_one"]
            if classifications[name]["declared"]
        ]
        if not declarations:
            blockers.append(
                {
                    "code": "api_compatibility_undeclared",
                    "detail": (
                        "API changes require exactly one of "
                        "change/breaking-api or change/non-breaking-api"
                    ),
                }
            )
        elif len(declarations) > 1:
            blockers.append(
                {
                    "code": "conflicting_api_compatibility",
                    "detail": "breaking and non-breaking API labels are mutually exclusive",
                }
            )
    elif (
        classifications["breaking_api"]["declared"]
        or classifications["non_breaking_api"]["declared"]
    ):
        blockers.append(
            {
                "code": "api_compatibility_without_api_change",
                "detail": "API compatibility labels require an api/** changed path",
            }
        )

    evidence = {
        "changed_files": sorted(files),
        "observed_labels": sorted(labels),
        "classifications": classifications,
        "suggested_labels": suggested_labels,
    }
    material = {"version": manifest["version"], "evidence": evidence, "blockers": blockers}
    return {
        "schema_version": 1,
        "decision_id": hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20],
        "metadata_complete": not blockers,
        "requires_human_review": bool(
            blockers
            or classifications["api_change"]["value"]
            or classifications["breaking_api"]["value"]
            or classifications["generated_only"]["value"]
        ),
        "blockers": blockers,
        "evidence": evidence,
    }


def evaluate_event(event: dict, files: list[str], manifest: dict) -> dict:
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ValueError("event must contain a pull_request object")
    return classify(files, _label_names(pull_request), manifest)


def validate_repository(manifest: dict, repo_root: Path) -> list[str]:
    errors = []
    labels_path = repo_root / ".github/labels.yml"
    try:
        labels = yaml.safe_load(labels_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return [f"cannot read .github/labels.yml: {error}"]
    if not isinstance(labels, dict):
        return [".github/labels.yml must be a mapping"]
    known = {str(name).casefold() for name in labels}
    required = {
        policy["label"].casefold()
        for policy in manifest["classifications"].values()
    }
    missing = sorted(required - known)
    if missing:
        errors.append("change metadata labels missing from catalog: " + ", ".join(missing))

    template_path = repo_root / ".github/PULL_REQUEST_TEMPLATE.md"
    try:
        template = template_path.read_text(encoding="utf-8").casefold()
    except OSError as error:
        errors.append(f"cannot read pull request template: {error}")
    else:
        undeclared = sorted(
            policy["label"]
            for policy in manifest["classifications"].values()
            if policy["label"].casefold() not in template
        )
        if undeclared:
            errors.append(
                "change metadata labels missing from PR template: "
                + ", ".join(undeclared)
            )
    return errors


def main() -> int:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).parent / "change-metadata.yaml",
    )
    parser.add_argument("--repo", type=Path, default=root)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.manifest)
        if args.validate:
            errors = validate_repository(manifest, args.repo)
            if errors:
                raise ValueError(
                    "change-metadata repository validation failed:\n  - "
                    + "\n  - ".join(errors)
                )
            print("Structured PR change metadata is valid.")
    except (OSError, UnicodeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
