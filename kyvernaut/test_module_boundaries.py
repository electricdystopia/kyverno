import copy
from pathlib import Path

from module_boundaries import (
    discover_modules,
    load_manifest,
    validate_repository,
    validate_schema,
)


ROOT = Path(__file__).parents[1]
MANIFEST = load_manifest(Path(__file__).parent / "module-boundaries.yaml")


def test_manifest_matches_every_repository_go_module():
    assert discover_modules(ROOT) == {
        ".": "github.com/kyverno/kyverno",
        "hack/api-group-resources": "github.com/kyverno/kyverno/hack/api-group-resources",
        "hack/controller-gen": "github.com/kyverno/kyverno/hack/controller-gen",
    }
    assert validate_repository(MANIFEST, ROOT) == []


def test_external_api_and_sdk_are_explicit_versioned_boundaries():
    boundaries = {item["module"] for item in MANIFEST["external_boundaries"]}
    assert boundaries == {"github.com/kyverno/api", "github.com/kyverno/sdk"}
    assert MANIFEST["decision"]["repository_strategy"] == "federated"
    assert MANIFEST["decision"]["recommendation"] == "retain-current-boundaries"


def test_schema_rejects_duplicate_or_escaping_local_module():
    manifest = copy.deepcopy(MANIFEST)
    manifest["local_modules"].append(
        {
            "path": "../outside",
            "module": "github.com/kyverno/kyverno",
            "role": "product-runtime",
        }
    )
    errors = validate_schema(manifest)
    assert any("repository-relative" in error for error in errors)
    assert any("duplicate local module name" in error for error in errors)


def test_repository_validation_detects_undeclared_module(tmp_path):
    (tmp_path / "go.mod").write_text("module example.test/root\n", encoding="utf-8")
    nested = tmp_path / "tool"
    nested.mkdir()
    (nested / "go.mod").write_text("module example.test/tool\n", encoding="utf-8")
    (tmp_path / "rationale.md").write_text("decision", encoding="utf-8")
    manifest = {
        "version": 1,
        "decision": {
            "repository_strategy": "federated",
            "local_workspace_policy": "independent-modules",
            "recommendation": "retain-current-boundaries",
            "rationale_document": "rationale.md",
        },
        "local_modules": [
            {"path": ".", "module": "example.test/root", "role": "product-runtime"}
        ],
        "external_boundaries": [],
        "revisit_when": ["Evidence changes."],
    }
    # validate_repository accepts schema-valid manifests from load_manifest;
    # supply a harmless versioned external dependency to exercise drift.
    manifest["external_boundaries"] = [
        {"module": "example.test/external", "role": "published-api-contract"}
    ]
    (tmp_path / "go.mod").write_text(
        "module example.test/root\n\nrequire example.test/external v1.0.0\n",
        encoding="utf-8",
    )
    errors = validate_repository(manifest, tmp_path)
    assert any("undeclared local module" in error for error in errors)
