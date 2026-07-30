import copy
from pathlib import Path

from change_metadata import (
    classify,
    load_manifest,
    validate_repository,
    validate_schema,
)


ROOT = Path(__file__).parents[1]
MANIFEST = load_manifest(Path(__file__).parent / "change-metadata.yaml")


def test_repository_declares_every_stable_change_label():
    assert validate_repository(MANIFEST, ROOT) == []


def test_docs_only_requires_every_file_to_be_documentation():
    decision = classify(["README.md", "docs/dev/logging/logging.md"], [], MANIFEST)
    assert decision["evidence"]["classifications"]["documentation_only"]["value"] is True
    assert "change/docs-only" in decision["evidence"]["suggested_labels"]

    mixed = classify(["README.md", "pkg/engine/engine.go"], [], MANIFEST)
    assert mixed["evidence"]["classifications"]["documentation_only"]["value"] is False


def test_generated_only_requires_every_file_to_match_reviewed_paths():
    decision = classify(
        ["config/crds/kyverno/kyverno.io_clusterpolicies.yaml", "pkg/client/clientset/versioned/clientset.go"],
        [],
        MANIFEST,
    )
    assert decision["evidence"]["classifications"]["generated_only"]["value"] is True
    assert decision["requires_human_review"] is True

    mixed = classify(["pkg/client/clientset/versioned/clientset.go", "api/kyverno/v1/spec_types.go"], [], MANIFEST)
    assert mixed["evidence"]["classifications"]["generated_only"]["value"] is False


def test_api_change_requires_exactly_one_explicit_compatibility_label():
    files = ["api/kyverno/v1/spec_types.go"]
    missing = classify(files, [], MANIFEST)
    assert missing["metadata_complete"] is False
    assert "api_compatibility_undeclared" in {
        blocker["code"] for blocker in missing["blockers"]
    }
    assert "kind/api-change" in missing["evidence"]["suggested_labels"]

    complete = classify(files, ["change/non-breaking-api"], MANIFEST)
    assert complete["metadata_complete"] is True
    assert complete["evidence"]["classifications"]["non_breaking_api"]["value"] is True

    conflict = classify(
        files,
        ["change/breaking-api", "change/non-breaking-api"],
        MANIFEST,
    )
    assert "conflicting_api_compatibility" in {
        blocker["code"] for blocker in conflict["blockers"]
    }


def test_non_api_change_does_not_require_compatibility_declaration():
    decision = classify(["pkg/engine/engine.go"], [], MANIFEST)
    assert decision["metadata_complete"] is True
    assert decision["requires_human_review"] is False


def test_api_compatibility_label_without_api_path_is_rejected():
    decision = classify(
        ["pkg/engine/engine.go"],
        ["change/breaking-api"],
        MANIFEST,
    )
    assert decision["metadata_complete"] is False
    assert "api_compatibility_without_api_change" in {
        item["code"] for item in decision["blockers"]
    }


def test_classifier_is_deterministic_and_does_not_trust_declared_inferred_label():
    files = ["pkg/engine/engine.go"]
    first = classify(files, ["change/docs-only"], MANIFEST)
    second = classify(files, ["change/docs-only"], MANIFEST)
    docs = first["evidence"]["classifications"]["documentation_only"]
    assert first == second
    assert docs["declared"] is True
    assert docs["value"] is False


def test_unsafe_or_duplicate_file_evidence_fails_closed():
    unsafe = classify(["../README.md"], [], MANIFEST)
    assert unsafe["metadata_complete"] is False
    assert unsafe["evidence"]["classifications"]["documentation_only"]["value"] is False
    assert "invalid_changed_path" in {item["code"] for item in unsafe["blockers"]}

    duplicate = classify(["README.md", "README.md"], [], MANIFEST)
    assert duplicate["evidence"]["classifications"]["documentation_only"]["value"] is False
    assert "duplicate_changed_file" in {item["code"] for item in duplicate["blockers"]}


def test_schema_rejects_unsafe_patterns_and_duplicate_labels():
    manifest = copy.deepcopy(MANIFEST)
    manifest["classifications"]["documentation_only"]["paths"].append("../outside/**")
    manifest["classifications"]["generated_only"]["label"] = "change/docs-only"
    errors = validate_schema(manifest)
    assert any("safe pattern" in error for error in errors)
    assert any("duplicate classification label" in error for error in errors)
