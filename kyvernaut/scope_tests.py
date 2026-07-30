#!/usr/bin/env python3
"""
scope_tests.py — diff-to-test-scope mapper (Phase 2 prototype for the
AI Maintainer Assistant proposal).

Given a set of changed file paths (from a PR diff), this tool consults
path-test-map.yaml and produces:
  - the Go unit test packages to run
  - the chainsaw conformance suites to run
  - whether a codegen verification pass is required
  - a risk assessment, including whether the diff touches paths that must
    never be auto-merged by an agent

This is intentionally dependency-light (PyYAML only) so it's easy to port
into a Go CLI subcommand later, or call from a GitHub Action.

Usage:
    # From a list of changed files (one per line, e.g. `git diff --name-only`)
    python3 scope_tests.py --diff-file changed_files.txt

    # Directly from git, comparing against a base ref
    python3 scope_tests.py --git-diff main...HEAD

    # From a specific commit (useful for demoing against real history)
    python3 scope_tests.py --commit e9deadc

Output: human-readable plan on stdout, machine-readable JSON with --json.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: pip install pyyaml --break-system-packages")


VALID_RISKS = {"low", "medium", "high"}
UNIT_TARGET = re.compile(r"^\./(?:[A-Za-z0-9_.-]+/)*\.\.\.$")


def load_manifest(map_path: Path) -> dict:
    with map_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    errors = validate_manifest(data)
    if errors:
        raise ValueError("invalid path-test map:\n  - " + "\n  - ".join(errors))
    return data


def load_rules(map_path: Path):
    return load_manifest(map_path)["rules"]


def validate_manifest(data: dict) -> list[str]:
    """Validate safety-relevant map invariants before using its decisions."""
    errors = []
    if not isinstance(data, dict):
        return ["document must be a mapping"]
    if data.get("version") != 1:
        errors.append("version must be 1")

    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        return errors + ["rules must be a non-empty list"]

    ids = set()
    prefixes = {}
    for index, rule in enumerate(rules):
        where = f"rules[{index}]"
        if not isinstance(rule, dict):
            errors.append(f"{where} must be a mapping")
            continue
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            errors.append(f"{where}.id must be a non-empty string")
            rule_id = where
        elif rule_id in ids:
            errors.append(f"duplicate rule id: {rule_id}")
        ids.add(rule_id)

        matches = rule.get("match")
        if not isinstance(matches, list) or not matches or not all(isinstance(item, str) and item for item in matches):
            errors.append(f"rule {rule_id}: match must be a non-empty string list")
            matches = []
        for prefix in matches:
            owner = prefixes.get(prefix)
            if owner and owner != rule_id:
                errors.append(f"match prefix {prefix!r} is claimed by both {owner!r} and {rule_id!r}")
            prefixes[prefix] = rule_id

        risk = rule.get("risk", "low")
        if risk not in VALID_RISKS:
            errors.append(f"rule {rule_id}: invalid risk {risk!r}")
        for field in ("unit", "conformance", "cli"):
            values = rule.get(field, [])
            if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
                errors.append(f"rule {rule_id}: {field} must be a string list")

        if rule.get("auto_merge_candidate"):
            if risk != "low":
                errors.append(f"rule {rule_id}: auto-merge candidates must be low risk")
            if rule.get("requires_human_review"):
                errors.append(f"rule {rule_id}: auto-merge candidates cannot require human review")
            if rule.get("codegen_check"):
                errors.append(f"rule {rule_id}: auto-merge candidates cannot require code generation")

    coverage = data.get("coverage")
    if not isinstance(coverage, dict):
        errors.append("coverage must be a mapping")
    else:
        for field in ("pkg_fallback_allowlist", "conformance_unmapped_allowlist"):
            values = coverage.get(field)
            if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
                errors.append(f"coverage.{field} must be a string list")
            elif len(values) != len(set(values)):
                errors.append(f"coverage.{field} contains duplicates")

    return errors


def _top_level_directories(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {entry.name for entry in root.iterdir() if entry.is_dir()}


def validate_repository_coverage(repo_root: Path, manifest: dict) -> list[str]:
    """Detect new or stale top-level source/test areas in the checked-out repo.

    Explicitly mapped directories are accepted. Existing gaps must be named in
    an allowlist, which makes a newly added directory fail closed instead of
    silently inheriting the broad fallback forever.
    """
    rules = manifest["rules"]
    coverage = manifest["coverage"]
    errors = []

    actual_pkg = _top_level_directories(repo_root / "pkg")
    explicit_pkg = set()
    for rule in rules:
        if rule["id"] == "pkg-fallback":
            continue
        for prefix in rule["match"]:
            if prefix.startswith("pkg/"):
                name = prefix.removeprefix("pkg/").split("/", 1)[0]
                if name:
                    explicit_pkg.add(name)
    pkg_allowlist = set(coverage["pkg_fallback_allowlist"])
    missing_pkg = actual_pkg - explicit_pkg - pkg_allowlist
    stale_pkg = pkg_allowlist - actual_pkg
    if missing_pkg:
        errors.append(
            "new pkg/ directories need an explicit rule or fallback approval: "
            + ", ".join(sorted(missing_pkg))
        )
    if stale_pkg:
        errors.append("stale pkg fallback allowlist entries: " + ", ".join(sorted(stale_pkg)))

    conformance_root = repo_root / "test/conformance/chainsaw"
    actual_conformance = _top_level_directories(conformance_root)
    mapped_conformance = set()
    for rule in rules:
        for suite in rule.get("conformance", []):
            name = suite.split("/", 1)[0]
            if name not in {"*", "**"}:
                mapped_conformance.add(name)
    conformance_allowlist = set(coverage["conformance_unmapped_allowlist"])
    missing_conformance = actual_conformance - mapped_conformance - conformance_allowlist
    stale_conformance = conformance_allowlist - actual_conformance
    nonexistent_conformance = mapped_conformance - actual_conformance
    if missing_conformance:
        errors.append(
            "new conformance suites need a mapping or explicit no-mapping approval: "
            + ", ".join(sorted(missing_conformance))
        )
    if stale_conformance:
        errors.append(
            "stale conformance no-mapping allowlist entries: "
            + ", ".join(sorted(stale_conformance))
        )
    if nonexistent_conformance:
        errors.append(
            "mapped conformance suites do not exist: "
            + ", ".join(sorted(nonexistent_conformance))
        )

    missing_unit_targets = []
    for rule in rules:
        for target in rule.get("unit", []):
            if target == "./..." or "<dir>" in target:
                continue
            if not UNIT_TARGET.fullmatch(target):
                missing_unit_targets.append(
                    f"{rule['id']} has unsafe target {target!r}"
                )
                continue
            directory = target.removeprefix("./").removesuffix("...")
            if directory and not (repo_root / directory).is_dir():
                missing_unit_targets.append(
                    f"{rule['id']} target does not exist: {target}"
                )
    if missing_unit_targets:
        errors.append("invalid mapped unit targets: " + "; ".join(missing_unit_targets))

    return errors


class AmbiguousMatch(Exception):
    """Raised when two different rules claim a path via equal-length prefixes.
    This must never be resolved silently: a coin-flip between e.g. a
    low-risk rule and a requires_human_review rule is exactly the kind of
    bug that would let a security-sensitive change slip through unreviewed."""


def _prefix_matches(path: str, prefix: str) -> bool:
    """Match a repository path prefix without crossing a path boundary."""
    if prefix.endswith("/"):
        return path.startswith(prefix)
    return path == prefix or path.startswith(prefix + "/")


def match_rule(path: str, rules: list):
    """Return the rule whose match prefix is the longest (most specific) fit
    for this path -- same principle as CODEOWNERS/gitignore precedence.
    Raises AmbiguousMatch if two different rules tie at the same prefix length."""
    candidates = []
    for rule in rules:
        for prefix in rule["match"]:
            if _prefix_matches(path, prefix):
                candidates.append((len(prefix), rule))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    top_len = candidates[0][0]
    tied = {id(r) for length, r in candidates if length == top_len}
    if len(tied) > 1:
        rule_ids = sorted({r["id"] for length, r in candidates if length == top_len})
        raise AmbiguousMatch(f"path '{path}' matched multiple rules at equal specificity: {rule_ids}")
    return candidates[0][1]


def get_changed_files(args) -> list:
    if args.diff_file:
        return [l.strip() for l in Path(args.diff_file).read_text().splitlines() if l.strip()]
    if args.commit:
        out = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:", args.commit],
            capture_output=True, text=True, check=True, cwd=args.repo,
        )
        return [l.strip() for l in out.stdout.splitlines() if l.strip()]
    if args.git_diff:
        out = subprocess.run(
            ["git", "diff", "--name-only", args.git_diff],
            capture_output=True, text=True, check=True, cwd=args.repo,
        )
        return [l.strip() for l in out.stdout.splitlines() if l.strip()]
    sys.exit("Provide one of --diff-file, --commit, or --git-diff")


def scope(changed_files: list, rules: list):
    unit, conformance, cli = set(), set(), set()
    codegen_required = False
    risk = "low"
    review_reasons = []
    matched, unmatched, low_confidence, ambiguous = [], [], [], []
    auto_merge_blockers = []

    risk_rank = {"low": 0, "medium": 1, "high": 2}

    for path in changed_files:
        try:
            rule = match_rule(path, rules)
        except AmbiguousMatch as e:
            # Fail safe, not silent: treat as unmatched (so the fallback
            # unit-test path still fires) AND force a human-review flag,
            # since we cannot know which rule's risk tier actually applies.
            ambiguous.append(str(e))
            review_reasons.append(f"{path}: {e}")
            rule = None
        if rule is None:
            unmatched.append(path)
            continue
        matched.append((path, rule["id"]))
        if not rule.get("auto_merge_candidate", False):
            auto_merge_blockers.append(
                f"{path} -> rule '{rule['id']}' is not approved for autonomous merge"
            )

        # A rule can match (so the file isn't "unmatched") while still giving
        # zero conformance coverage -- this is the dangerous case: the plan
        # LOOKS complete but a shared/generic path with real cross-cutting
        # blast radius (e.g. pkg/utils/kube) got only unit tests. Surface it
        # explicitly rather than let it disappear into a clean-looking plan.
        if rule["id"] == "pkg-fallback" and not rule.get("conformance"):
            low_confidence.append(path)

        for u in rule.get("unit", []):
            if "<dir>" in u:
                # pkg-fallback: substitute the actual second-level package dir
                parts = path.split("/")
                if len(parts) >= 2:
                    u = u.replace("<dir>", parts[1])
                else:
                    continue
            unit.add(u)
        conformance.update(rule.get("conformance", []))
        cli.update(rule.get("cli", []))

        if rule.get("codegen_check"):
            codegen_required = True
        rule_risk = rule.get("risk", "low")
        if risk_rank[rule_risk] > risk_rank[risk]:
            risk = rule_risk
        if rule.get("requires_human_review"):
            review_reasons.append(f"{path} -> rule '{rule['id']}': {rule.get('note', 'flagged as human-review-required')}")

    return {
        "changed_files": changed_files,
        "matched": matched,
        "unmatched_files": unmatched,
        "low_confidence_files": low_confidence,
        "ambiguous_matches": ambiguous,
        "unit_test_packages": sorted(unit),
        "conformance_suites": sorted(conformance),
        "cli_suites": sorted(cli),
        "codegen_verify_required": codegen_required,
        "risk": risk,
        "requires_human_review": bool(review_reasons),
        "review_reasons": review_reasons,
        # low-confidence coverage gaps and unmatched files both mean "we are
        # not actually sure this scope is complete" -- neither should be
        # allowed to slide through as auto-merge eligible just because no
        # explicit high-risk rule fired.
        "auto_merge_blockers": auto_merge_blockers,
        # This is only scope-level candidacy. The caller must additionally
        # authenticate the bot actor, classify the version bump as patch/minor,
        # require green CI, and honor hold/kill-switch controls.
        "auto_merge_eligible": (
            bool(changed_files)
            and not review_reasons
            and risk == "low"
            and not low_confidence
            and not unmatched
            and not auto_merge_blockers
        ),
    }


def render(plan: dict) -> str:
    lines = []
    lines.append(f"Changed files: {len(plan['changed_files'])}")
    lines.append(f"Overall risk: {plan['risk'].upper()}")
    lines.append(
        "Auto-merge candidate (scope only; actor, semver, CI, hold label, and "
        f"kill switch still gate): {plan['auto_merge_eligible']}"
    )
    if plan["requires_human_review"]:
        lines.append("\n⚠️  HUMAN REVIEW REQUIRED — do not auto-merge:")
        for r in plan["review_reasons"]:
            lines.append(f"   - {r}")

    if plan["low_confidence_files"]:
        lines.append("\n⚠️  LOW-CONFIDENCE COVERAGE — matched a rule but no conformance suite was mapped:")
        for f in plan["low_confidence_files"]:
            lines.append(f"   - {f}  (unit tests only; consider a manual conformance pass or a new rule)")

    lines.append("\nUnit test packages to run:")
    if plan["unit_test_packages"]:
        lines.append("  go test " + " ".join(plan["unit_test_packages"]))
    else:
        lines.append("  (none matched)")

    lines.append("\nChainsaw conformance suites to run:")
    if plan["conformance_suites"]:
        for s in plan["conformance_suites"]:
            lines.append(f"  chainsaw test test/conformance/chainsaw/{s}")
    else:
        lines.append("  (none matched)")

    if plan["cli_suites"]:
        lines.append("\nCLI test targets to run:")
        lines.append("  make test-cli")

    if plan["codegen_verify_required"]:
        lines.append("\nCodegen verification required:")
        lines.append("  make codegen-all-code && make verify-codegen")

    if plan["unmatched_files"]:
        lines.append("\nFiles with no explicit rule (add to path-test-map.yaml, or fall back to full suite):")
        for f in plan["unmatched_files"]:
            lines.append(f"  - {f}")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--diff-file", help="File with one changed path per line")
    ap.add_argument("--git-diff", help="git diff ref range, e.g. 'main...HEAD'")
    ap.add_argument("--commit", help="Single commit SHA to inspect (demo/testing convenience)")
    ap.add_argument("--repo", default=".", help="Path to the kyverno git repo (for --git-diff/--commit)")
    ap.add_argument("--map", default=str(Path(__file__).parent / "path-test-map.yaml"))
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text")
    ap.add_argument(
        "--validate",
        action="store_true",
        help="Validate the manifest and repository directory coverage; may be used without a diff input",
    )
    args = ap.parse_args()

    manifest = load_manifest(Path(args.map))
    rules = manifest["rules"]
    if args.validate:
        errors = validate_repository_coverage(Path(args.repo), manifest)
        if errors:
            print("Path-test map validation failed:", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            return 1
        print("Path-test map and repository coverage are valid.")
        if not (args.diff_file or args.commit or args.git_diff):
            return 0

    changed_files = get_changed_files(args)
    plan = scope(changed_files, rules)

    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        print(render(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
