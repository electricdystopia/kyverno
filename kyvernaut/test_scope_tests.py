"""
Sanity tests for scope_tests.py's rule-matching logic. Run with:
    python3 -m pytest test_scope_tests.py -q
"""
from pathlib import Path
from scope_tests import load_rules, scope

RULES = load_rules(Path(__file__).parent / "path-test-map.yaml")


def test_security_sensitive_path_requires_review():
    plan = scope(["pkg/cosign/verify.go"], RULES)
    assert plan["requires_human_review"] is True
    assert plan["risk"] == "high"
    assert plan["auto_merge_eligible"] is False


def test_dependency_only_diff_is_low_risk_and_mergeable():
    plan = scope(["go.mod", "go.sum"], RULES)
    assert plan["risk"] == "low"
    assert plan["requires_human_review"] is False
    assert plan["auto_merge_eligible"] is True


def test_api_type_change_forces_codegen_and_review():
    plan = scope(["api/kyverno/v1/clusterpolicy_types.go"], RULES)
    assert plan["codegen_verify_required"] is True
    assert plan["requires_human_review"] is True


def test_ivpol_change_maps_to_correct_conformance_suite():
    plan = scope(["pkg/cel/policies/ivpol/engine/reconciler.go"], RULES)
    assert "image-validating-policies/**" in plan["conformance_suites"]
    assert plan["requires_human_review"] is True


def test_pkg_fallback_still_produces_unit_tests_for_unmapped_package():
    plan = scope(["pkg/toggle/toggle.go"], RULES)
    assert "./pkg/toggle/..." in plan["unit_test_packages"]


def test_most_specific_rule_wins_over_broad_prefix():
    # pkg/engine/validate should hit engine-validate, not the broader engine-core rule
    plan = scope(["pkg/engine/validate/validate.go"], RULES)
    assert "validate/**" in plan["conformance_suites"]
    assert "validating-policies" not in " ".join(plan["conformance_suites"])


def test_generic_shared_utility_is_flagged_as_low_confidence():
    # A file that hits pkg-fallback with no mapped conformance suite must
    # never look like a "clean" plan -- it has to be surfaced as a gap.
    plan = scope(["pkg/utils/kube/labels.go"], RULES)
    assert "pkg/utils/kube/labels.go" in plan["low_confidence_files"]
    assert plan["auto_merge_eligible"] is False


def test_low_confidence_file_blocks_auto_merge_regardless_of_risk_tier():
    plan = scope(["pkg/utils/strings/strings.go"], RULES)
    assert plan["low_confidence_files"]  # unmapped conformance coverage
    assert plan["auto_merge_eligible"] is False


def test_unmatched_file_blocks_auto_merge():
    plan = scope(["totally/new/top-level/dir/file.go"], RULES)
    assert plan["unmatched_files"]
    assert plan["auto_merge_eligible"] is False


def test_engine_api_resolves_without_ambiguity():
    # Regression: pkg/engine/api was previously listed under both
    # engine-generate and engine-core, causing an AmbiguousMatch at runtime.
    # Found via the offline backtest against real commit history, not by
    # hand-inspection -- exactly the kind of gap this backtest is for.
    plan = scope(["pkg/engine/api/client.go"], RULES)
    assert plan["ambiguous_matches"] == []
