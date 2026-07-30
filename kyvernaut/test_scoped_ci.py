import copy
import json
from pathlib import Path

import pytest

from dependency_pr import load_config, validate_config
from scope_tests import load_manifest
from scoped_ci import (
    compile_plan,
    load_changed_files,
    load_profiles,
    validate_profiles,
)


ROOT = Path(__file__).parents[1]
CONFIG = load_config(ROOT / ".github/ai-maintainer.yaml")
MANIFEST = load_manifest(Path(__file__).parent / "path-test-map.yaml")
PROFILES = load_profiles(
    Path(__file__).parent / "conformance-profiles.yaml",
    MANIFEST,
    ROOT,
)


def plan(files, config=None, manifest=None):
    return compile_plan(
        files,
        config or CONFIG,
        manifest or MANIFEST,
        PROFILES,
        ROOT,
        environment={},
        run_id="scope-run",
    )


def test_engine_diff_compiles_real_unit_cli_and_conformance_jobs():
    result = plan(["pkg/engine/validate/validate.go"])
    packages = {
        item["package"] for item in result["unit_matrix"]["include"]
    }
    suites = {
        item["suite"] for item in result["conformance_matrix"]["include"]
    }
    assert "./pkg/engine/validate/..." in packages
    assert {"validate", "policy-validation"} == suites
    assert result["run_cli"] is True
    assert result["selection_complete"] is True
    assert result["execution_mode"] == "shadow_compare"
    assert result["decision_id"]


def test_unknown_path_expands_instead_of_silently_under_scoping():
    result = plan(["new-runtime/location.go"])
    assert result["unit_matrix"] == {"include": [{"package": "./..."}]}
    assert result["conformance_matrix"] == {"include": []}
    assert result["requires_full_ci"] is True
    assert result["selection_complete"] is False
    assert result["unsupported_conformance"][0]["suite"] == "**"
    assert any("uncertain path coverage" in reason for reason in result["expansions"])


def test_api_change_requires_codegen_and_authoritative_full_conformance():
    result = plan(["api/kyverno/v1/policy_types.go"])
    assert result["codegen_required"] is True
    assert result["unit_matrix"]["include"] == [{"package": "./..."}]
    assert result["requires_full_ci"] is True
    assert result["unsupported_conformance"][0]["suite"] == "**"


def test_special_profiles_match_existing_ci_requirements():
    result = plan(["pkg/controllers/report/controller.go"])
    jobs = {
        item["suite"]: item for item in result["conformance_matrix"]["include"]
    }
    assert jobs["openreports"]["install_openreports"] == "true"
    assert jobs["openreports"]["kyverno_configs"] == "openreports"
    assert jobs["reports-exclude-result"]["kyverno_configs"] == "exclude-result"
    assert jobs["reports-exclude-result"]["kubernetes_version"] == "v1.34.0"


def test_specialized_sigstore_scope_is_explicitly_not_faked_by_generic_runner():
    result = plan(["pkg/image/verify.go"])
    assert result["requires_full_ci"] is True
    unsupported = {
        item["suite"]: item["reason"] for item in result["unsupported_conformance"]
    }
    assert "custom-sigstore" in unsupported
    assert "Sigstore scaffolding" in unsupported["custom-sigstore"]
    assert "verify-images" in {
        item["suite"] for item in result["conformance_matrix"]["include"]
    }


def test_conformance_job_cap_fails_closed_without_running_partial_selection():
    config = copy.deepcopy(CONFIG)
    config["scoped_ci"]["max_conformance_jobs"] = 1
    result = plan(["pkg/engine/validate/validate.go"], config=config)
    assert result["conformance_matrix"]["include"] == []
    assert result["requires_full_ci"] is True
    assert "exceed" in result["unsupported_conformance"][0]["reason"]


def test_disabled_or_paused_policy_keeps_plan_but_blocks_jobs():
    disabled = copy.deepcopy(CONFIG)
    disabled["scoped_ci"]["enabled"] = False
    assert plan(["pkg/engine/validate/validate.go"], config=disabled)[
        "assistant_enabled"
    ] is False
    paused = compile_plan(
        ["pkg/engine/validate/validate.go"],
        CONFIG,
        MANIFEST,
        PROFILES,
        ROOT,
        environment={"KYVERNAUT_PAUSED": "true"},
        run_id="paused",
    )
    assert paused["assistant_enabled"] is False
    assert paused["kill_switch_active"] is True


@pytest.mark.parametrize(
    "value",
    [
        [{"filename": "../outside"}],
        [{"filename": "/absolute"}],
        [{"filename": "pkg\\windows.go"}],
        [{"filename": "pkg/x.go\nsecond"}],
        [],
    ],
)
def test_changed_file_input_rejects_unsafe_or_empty_paths(tmp_path, value):
    path = tmp_path / "files.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        load_changed_files(path)


def test_untrusted_unit_argv_in_manifest_is_rejected_before_matrix_output():
    manifest = copy.deepcopy(MANIFEST)
    rule = next(rule for rule in manifest["rules"] if rule["id"] == "engine-validate")
    rule["unit"] = ["./pkg/engine/...;curl"]
    with pytest.raises(ValueError, match="unsafe Go test target"):
        plan(["pkg/engine/validate/validate.go"], manifest=manifest)


def test_profile_manifest_must_cover_every_mapped_suite_once():
    profiles = copy.deepcopy(PROFILES)
    profiles["profiles"].pop("validate")
    errors = validate_profiles(profiles, MANIFEST, ROOT)
    assert any("missing execution profiles" in error for error in errors)


def test_scoped_ci_config_is_bounded_and_pinned():
    config = copy.deepcopy(CONFIG)
    config["scoped_ci"]["max_unit_jobs"] = 21
    config["scoped_ci"]["kubernetes_version"] = "latest"
    errors = validate_config(config)
    assert any("max_unit_jobs" in error for error in errors)
    assert any("kubernetes_version" in error for error in errors)


def test_plan_binds_selection_to_base_and_head_shas():
    result = compile_plan(
        ["pkg/engine/validate/validate.go"],
        CONFIG,
        MANIFEST,
        PROFILES,
        ROOT,
        environment={},
        run_id="bound",
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    assert result["base_sha"] == "a" * 40
    assert result["head_sha"] == "b" * 40
    with pytest.raises(ValueError, match="head_sha"):
        compile_plan(
            ["pkg/engine/validate/validate.go"],
            CONFIG,
            MANIFEST,
            PROFILES,
            ROOT,
            environment={},
            run_id="bad",
            head_sha="not-a-sha",
        )
