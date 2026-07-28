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
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: pip install pyyaml --break-system-packages")


def load_rules(map_path: Path):
    with open(map_path) as f:
        data = yaml.safe_load(f)
    return data["rules"]


class AmbiguousMatch(Exception):
    """Raised when two different rules claim a path via equal-length prefixes.
    This must never be resolved silently: a coin-flip between e.g. a
    low-risk rule and a requires_human_review rule is exactly the kind of
    bug that would let a security-sensitive change slip through unreviewed."""


def match_rule(path: str, rules: list):
    """Return the rule whose match prefix is the longest (most specific) fit
    for this path -- same principle as CODEOWNERS/gitignore precedence.
    Raises AmbiguousMatch if two different rules tie at the same prefix length."""
    candidates = []
    for rule in rules:
        for prefix in rule["match"]:
            if path.startswith(prefix):
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
        "auto_merge_eligible": (
            not review_reasons and risk == "low"
            and not low_confidence and not unmatched
        ),
    }


def render(plan: dict) -> str:
    lines = []
    lines.append(f"Changed files: {len(plan['changed_files'])}")
    lines.append(f"Overall risk: {plan['risk'].upper()}")
    lines.append(f"Auto-merge eligible (by scope alone, still gated on green CI): {plan['auto_merge_eligible']}")
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
    args = ap.parse_args()

    rules = load_rules(Path(args.map))
    changed_files = get_changed_files(args)
    plan = scope(changed_files, rules)

    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        print(render(plan))


if __name__ == "__main__":
    main()
