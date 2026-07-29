#!/usr/bin/env python3
"""
backtest_offline.py — Track A of the shadow-mode evaluation.

For a sample of real, already-merged commits on kyverno/kyverno main, this
computes what scope_tests.py would have predicted for that diff and reports:
  - how many of the 47 real conformance CI jobs the scope would have run
  - the % reduction vs. running the full matrix
  - risk tier / auto-merge-eligibility / human-review-required distribution
  - how many commits triggered a low_confidence or unmatched-file warning
    (i.e. cases the manifest doesn't confidently cover yet)

This does NOT tell us whether the reduction is *safe* -- that requires
Track B (backtest_ci_outcomes.py), which checks predicted scope against
real CI job failures. This script answers a narrower, still-useful
question: "if we shipped this today, how much CI time would it plausibly
save, and how often is it flying blind (low-confidence)?"

Usage:
    python3 backtest_offline.py --repo /path/to/kyverno --count 150 --out results.json
"""
import argparse
import json
import subprocess
from pathlib import Path

from scope_tests import load_rules, scope

REAL_CI_JOB_COUNT = 47  # from .github/workflows/tests-conformance.yaml, see README


def get_commits(repo: str, count: int):
    out = subprocess.run(
        ["git", "log", f"-{count}", "--pretty=format:%H|%s"],
        capture_output=True, text=True, check=True, cwd=repo,
    )
    return [line.split("|", 1) for line in out.stdout.splitlines() if line.strip()]


def changed_files(repo: str, sha: str):
    out = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", sha],
        capture_output=True, text=True, check=True, cwd=repo,
    )
    return [l.strip() for l in out.stdout.splitlines() if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--count", type=int, default=150)
    ap.add_argument("--map", default=str(Path(__file__).parent / "path-test-map.yaml"))
    ap.add_argument("--out", default="backtest_offline_results.json")
    args = ap.parse_args()

    rules = load_rules(Path(args.map))
    commits = get_commits(args.repo, args.count)

    rows = []
    for sha, subject in commits:
        files = changed_files(args.repo, sha)
        if not files:
            continue
        plan = scope(files, rules)
        n_suites = len(plan["conformance_suites"])
        rows.append({
            "sha": sha[:10],
            "subject": subject[:80],
            "n_files": len(files),
            "n_conformance_suites_predicted": n_suites,
            "pct_of_full_ci_matrix": round(100 * n_suites / REAL_CI_JOB_COUNT, 1),
            "risk": plan["risk"],
            "auto_merge_eligible": plan["auto_merge_eligible"],
            "requires_human_review": plan["requires_human_review"],
            "has_low_confidence_files": bool(plan["low_confidence_files"]),
            "has_unmatched_files": bool(plan["unmatched_files"]),
        })

    n = len(rows)
    avg_pct = sum(r["pct_of_full_ci_matrix"] for r in rows) / n
    low_conf_rate = sum(r["has_low_confidence_files"] for r in rows) / n
    unmatched_rate = sum(r["has_unmatched_files"] for r in rows) / n
    review_rate = sum(r["requires_human_review"] for r in rows) / n
    automerge_rate = sum(r["auto_merge_eligible"] for r in rows) / n

    summary = {
        "sample_size": n,
        "avg_pct_of_full_ci_matrix_triggered": round(avg_pct, 1),
        "avg_implied_ci_reduction_pct": round(100 - avg_pct, 1),
        "pct_commits_with_low_confidence_gap": round(100 * low_conf_rate, 1),
        "pct_commits_with_fully_unmatched_files": round(100 * unmatched_rate, 1),
        "pct_commits_requiring_human_review": round(100 * review_rate, 1),
        "pct_commits_auto_merge_eligible_by_scope": round(100 * automerge_rate, 1),
    }

    Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"\nFull per-commit results written to {args.out}")


if __name__ == "__main__":
    main()
