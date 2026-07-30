#!/usr/bin/env python3
"""Compile a diff-to-test scope into a bounded, executable shadow-CI plan."""

import argparse
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

import yaml

from dependency_pr import load_config
from scope_tests import load_manifest, scope


UNIT_TARGET = re.compile(r"^\./(?:[A-Za-z0-9_.-]+/)*\.\.\.$")
SUITE_NAME = re.compile(r"^(?:\*\*|[a-z0-9](?:[a-z0-9./-]*[a-z0-9])?)$")
PROFILE_FIELDS = {
    "kubernetes_version",
    "kind_config",
    "kyverno_configs",
    "quarantined_tests",
    "install_openreports",
    "install_kubectl_evict",
}


def _conformance_roots(manifest: dict) -> set[str]:
    return {
        value.removesuffix("/**")
        for rule in manifest["rules"]
        for value in rule.get("conformance", [])
    }


def validate_profiles(data: dict, manifest: dict, repo_root: Path) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return ["document must be a mapping"]
    if data.get("version") != 1:
        errors.append("version must be 1")
    defaults = data.get("defaults")
    profiles = data.get("profiles")
    unsupported = data.get("unsupported")
    if not isinstance(defaults, dict):
        return errors + ["defaults must be a mapping"]
    if not isinstance(profiles, dict):
        return errors + ["profiles must be a mapping"]
    if not isinstance(unsupported, dict):
        return errors + ["unsupported must be a mapping"]

    unknown_defaults = set(defaults) - PROFILE_FIELDS
    if unknown_defaults:
        errors.append("unknown default profile fields: " + ", ".join(sorted(unknown_defaults)))
    profile_names = set(profiles)
    unsupported_names = set(unsupported)
    overlap = profile_names & unsupported_names
    if overlap:
        errors.append("suite is both executable and unsupported: " + ", ".join(sorted(overlap)))
    expected = _conformance_roots(manifest)
    missing = expected - profile_names - unsupported_names
    extra = (profile_names | unsupported_names) - expected
    if missing:
        errors.append("mapped suites missing execution profiles: " + ", ".join(sorted(missing)))
    if extra:
        errors.append("stale execution profiles: " + ", ".join(sorted(extra)))

    for suite, reason in unsupported.items():
        if not isinstance(suite, str) or not SUITE_NAME.fullmatch(suite) or ".." in suite:
            errors.append(f"invalid unsupported suite name: {suite!r}")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"unsupported suite {suite!r} needs a reason")

    for suite, override in profiles.items():
        if not isinstance(suite, str) or not SUITE_NAME.fullmatch(suite) or ".." in suite:
            errors.append(f"invalid suite name: {suite!r}")
            continue
        if not isinstance(override, dict):
            errors.append(f"profile {suite!r} must be a mapping")
            continue
        unknown = set(override) - PROFILE_FIELDS
        if unknown:
            errors.append(f"profile {suite!r} has unknown fields: " + ", ".join(sorted(unknown)))
        merged = defaults | override
        for field in ("install_openreports", "install_kubectl_evict"):
            if not isinstance(merged.get(field), bool):
                errors.append(f"profile {suite!r}.{field} must be a boolean")
        for field in ("kind_config", "kyverno_configs", "quarantined_tests"):
            if not isinstance(merged.get(field), str):
                errors.append(f"profile {suite!r}.{field} must be a string")
        version = merged.get("kubernetes_version")
        if version is not None and (
            not isinstance(version, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", version)
        ):
            errors.append(f"profile {suite!r}.kubernetes_version must be a pinned vX.Y.Z")

        kind_config = merged.get("kind_config")
        if isinstance(kind_config, str):
            if not kind_config.startswith("./scripts/config/kind/") or not (
                repo_root / kind_config.removeprefix("./")
            ).is_file():
                errors.append(f"profile {suite!r} references missing/unsafe kind config")
        configs = merged.get("kyverno_configs")
        if isinstance(configs, str):
            names = configs.split(",")
            if not names or any(
                not name
                or not re.fullmatch(r"[a-z0-9-]+", name)
                or not (repo_root / "scripts/config" / name / "kyverno.yaml").is_file()
                for name in names
            ):
                errors.append(f"profile {suite!r} references missing/unsafe Kyverno config")
        quarantined = merged.get("quarantined_tests")
        if isinstance(quarantined, str) and quarantined and not re.fullmatch(
            r"[A-Za-z0-9_,.-]+", quarantined
        ):
            errors.append(f"profile {suite!r}.quarantined_tests is unsafe")
        suite_path = repo_root / "test/conformance/chainsaw" / suite
        if suite != "**" and not suite_path.is_dir():
            errors.append(f"profile suite directory does not exist: {suite}")
    return errors


def load_profiles(path: Path, manifest: dict, repo_root: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    errors = validate_profiles(data, manifest, repo_root)
    if errors:
        raise ValueError("invalid conformance profiles:\n  - " + "\n  - ".join(errors))
    return data


def load_changed_files(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("changed-files JSON must be a list")
    files = []
    for index, item in enumerate(data):
        filename = item.get("filename") if isinstance(item, dict) else item
        if not isinstance(filename, str) or not filename:
            raise ValueError(f"changed-files entry {index} has no filename")
        pure = PurePosixPath(filename)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or "\\" in filename
            or "\n" in filename
            or "\x00" in filename
        ):
            raise ValueError(f"unsafe changed path: {filename!r}")
        files.append(filename)
    if not files:
        raise ValueError("changed-files input must not be empty")
    if len(files) != len(set(files)):
        raise ValueError("changed-files input contains duplicates")
    return sorted(files)


def _validate_unit_target(target: str, repo_root: Path) -> None:
    if not isinstance(target, str) or not UNIT_TARGET.fullmatch(target):
        raise ValueError(f"unsafe Go test target from trusted map: {target!r}")
    directory = target.removeprefix("./").removesuffix("...")
    if directory and not (repo_root / directory).is_dir():
        raise ValueError(f"Go test target directory does not exist: {target!r}")


def _paused(config: dict, environment: dict[str, str]) -> bool:
    switch = config["kill_switch"]
    raw = environment.get(switch["environment_variable"])
    if raw is None:
        return False
    return raw.strip().casefold() in {
        value.casefold() for value in switch["truthy_values"]
    }


def compile_plan(
    changed_files: list[str],
    config: dict,
    manifest: dict,
    profiles: dict,
    repo_root: Path,
    *,
    environment: dict[str, str],
    run_id: str | None,
    base_sha: str | None = None,
    head_sha: str | None = None,
) -> dict:
    for name, value in (("base_sha", base_sha), ("head_sha", head_sha)):
        if value is not None and not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ValueError(f"{name} must be a 40-character lowercase hex SHA")
    selection = scope(changed_files, manifest["rules"])
    policy = config["scoped_ci"]
    uncertain = bool(
        selection["unmatched_files"]
        or selection["low_confidence_files"]
        or selection["ambiguous_matches"]
    )
    expansions = []
    unit_targets = list(selection["unit_test_packages"])
    requested_suites = [
        value.removesuffix("/**") for value in selection["conformance_suites"]
    ]
    if uncertain and policy["expand_uncertain_scope"]:
        unit_targets = ["./..."]
        requested_suites = ["**"]
        expansions.append(
            "uncertain path coverage expanded unit scope to ./... and requires full conformance"
        )
    if len(unit_targets) > policy["max_unit_jobs"]:
        unit_targets = ["./..."]
        expansions.append("unit target count exceeded cap and expanded to ./...")
    for target in unit_targets:
        _validate_unit_target(target, repo_root)

    unsupported = []
    conformance_jobs = []
    requested_suites = sorted(set(requested_suites))
    if len(requested_suites) > policy["max_conformance_jobs"]:
        unsupported.append(
            {
                "suite": "**",
                "reason": (
                    f"{len(requested_suites)} selected suites exceed the "
                    f"{policy['max_conformance_jobs']}-job cap; authoritative full conformance required"
                ),
            }
        )
        requested_suites = []
    for suite in requested_suites:
        if suite in profiles["unsupported"]:
            unsupported.append({"suite": suite, "reason": profiles["unsupported"][suite]})
            continue
        if suite not in profiles["profiles"]:
            raise ValueError(f"suite has no validated execution profile: {suite!r}")
        profile = profiles["defaults"] | profiles["profiles"][suite]
        conformance_jobs.append(
            {
                "suite": suite,
                "kubernetes_version": profile.get(
                    "kubernetes_version", policy["kubernetes_version"]
                ),
                "kind_config": profile["kind_config"],
                "kyverno_configs": profile["kyverno_configs"],
                "quarantined_tests": profile["quarantined_tests"],
                "install_openreports": str(profile["install_openreports"]).lower(),
                "install_kubectl_evict": str(profile["install_kubectl_evict"]).lower(),
            }
        )

    paused = _paused(config, environment)
    assistant_enabled = config["enabled"] and policy["enabled"] and not paused
    material = {
        "run_id": run_id,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "mode": config["mode"],
        "changed_files": changed_files,
        "selection": selection,
        "unit_targets": unit_targets,
        "conformance_jobs": conformance_jobs,
        "unsupported": unsupported,
        "expansions": expansions,
        "run_cli": bool(selection["cli_suites"]),
    }
    decision_id = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return {
        "schema_version": 1,
        "decision_id": decision_id,
        "run_id": run_id,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "mode": config["mode"],
        "execution_mode": "shadow_compare",
        "assistant_enabled": assistant_enabled,
        "kill_switch_active": paused,
        "selection_complete": not uncertain and not unsupported,
        "requires_full_ci": bool(unsupported),
        "expansions": expansions,
        "unsupported_conformance": unsupported,
        "run_cli": bool(selection["cli_suites"]),
        "codegen_required": selection["codegen_verify_required"],
        "unit_matrix": {"include": [{"package": target} for target in unit_targets]},
        "conformance_matrix": {"include": conformance_jobs},
        "scope": selection,
    }


def render_summary(plan: dict) -> str:
    lines = [
        "## Kyvernaut scoped CI",
        "",
        f"- Decision ID: `{html.escape(plan['decision_id'])}`",
        f"- Execution mode: **{html.escape(plan['execution_mode'])}**",
        f"- Unit jobs: **{len(plan['unit_matrix']['include'])}**",
        f"- Conformance jobs: **{len(plan['conformance_matrix']['include'])}**",
        f"- CLI suite selected: **{str(plan['run_cli']).lower()}**",
        f"- Existing authoritative codegen check required: "
        f"**{str(plan['codegen_required']).lower()}**",
        f"- Authoritative full CI still required: **{str(plan['requires_full_ci']).lower()}**",
    ]
    if plan["expansions"]:
        lines.extend(["", "Scope expansions:"])
        lines.extend(f"- {html.escape(value)}" for value in plan["expansions"])
    if plan["unsupported_conformance"]:
        lines.extend(["", "Scopes not executable by the generic runner:"])
        lines.extend(
            f"- `{html.escape(item['suite'])}`: {html.escape(item['reason'])}"
            for item in plan["unsupported_conformance"]
        )
    lines.extend(
        [
            "",
            "This workflow runs alongside existing full CI for comparison. "
            "It must not replace required checks until shadow evidence is sufficient.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed-files", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path, default=root / ".github/ai-maintainer.yaml")
    parser.add_argument("--map", type=Path, default=Path(__file__).parent / "path-test-map.yaml")
    parser.add_argument(
        "--profiles",
        type=Path,
        default=Path(__file__).parent / "conformance-profiles.yaml",
    )
    parser.add_argument("--repo", type=Path, default=root)
    parser.add_argument("--run-id")
    parser.add_argument("--base-sha")
    parser.add_argument("--head-sha")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate config, path map, profiles, and referenced repository paths",
    )
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        manifest = load_manifest(args.map)
        profiles = load_profiles(args.profiles, manifest, args.repo)
        if args.validate:
            print("Scoped CI configuration and execution profiles are valid.")
            return 0
        if args.changed_files is None or args.output is None:
            raise ValueError("--changed-files and --output are required unless --validate is used")
        plan = compile_plan(
            load_changed_files(args.changed_files),
            config,
            manifest,
            profiles,
            args.repo,
            environment=dict(os.environ),
            run_id=args.run_id,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        if args.summary:
            args.summary.parent.mkdir(parents=True, exist_ok=True)
            args.summary.write_text(render_summary(plan), encoding="utf-8")
        if args.github_output:
            unit_matrix = json.dumps(plan["unit_matrix"], separators=(",", ":"))
            conformance_matrix = json.dumps(
                plan["conformance_matrix"], separators=(",", ":")
            )
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write(f"assistant_enabled={str(plan['assistant_enabled']).lower()}\n")
                stream.write(f"decision_id={plan['decision_id']}\n")
                stream.write(f"unit_count={len(plan['unit_matrix']['include'])}\n")
                stream.write(
                    f"conformance_count={len(plan['conformance_matrix']['include'])}\n"
                )
                stream.write(f"run_cli={str(plan['run_cli']).lower()}\n")
                stream.write(f"codegen_required={str(plan['codegen_required']).lower()}\n")
                stream.write(f"requires_full_ci={str(plan['requires_full_ci']).lower()}\n")
                stream.write(f"unit_matrix={unit_matrix}\n")
                stream.write(f"conformance_matrix={conformance_matrix}\n")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"scoped CI planning failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
