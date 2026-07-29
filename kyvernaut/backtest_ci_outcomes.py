#!/usr/bin/env python3
"""
backtest_ci_outcomes.py — Track B of the shadow-mode evaluation.

Track A (backtest_offline.py) tells us how much CI we'd skip. This tells us
whether that's *safe*: for real merged PRs, did any conformance job that
actually failed during the PR's CI history fall INSIDE or OUTSIDE our
predicted scope?

  - Inside scope + failed  -> tool would have caught it. Good.
  - Outside scope + failed -> tool would have MISSED it. This is the
    dangerous case and the whole reason Phase 2 stays comment-only /
    shadow-mode until this number is proven to be zero (or explicitly
    accepted) across a large sample.

Requires a GitHub token (unauthenticated rate limit is 60/hr and gets
exhausted almost immediately on a shared IP -- this is expected to run
with GITHUB_TOKEN from a GitHub Action, or a personal access token locally):

    export GITHUB_TOKEN=ghp_...
    python3 backtest_ci_outcomes.py --count 50 --out ci_backtest.json

Each PR costs ~2-4 API calls (files, list check-runs per relevant SHA), so
budget accordingly: 50 PRs ~= 150-200 calls, well within the 5000/hr
authenticated limit but well over the 60/hr unauthenticated one.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import urllib.request
import urllib.error

from scope_tests import load_rules, scope

API = "https://api.github.com"
REPO = "kyverno/kyverno"


def gh_get(path: str, token: str | None):
    url = f"{API}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        print(f"  [warn] GET {path} -> HTTP {e.code}: {body[:200]}", file=sys.stderr)
        return None


def get_merged_prs(token, count):
    prs = []
    page = 1
    while len(prs) < count:
        data = gh_get(f"/repos/{REPO}/pulls?state=closed&base=main&sort=updated&direction=desc&per_page=50&page={page}", token)
        if not data:
            break
        for pr in data:
            if pr.get("merged_at"):
                prs.append(pr)
            if len(prs) >= count:
                break
        page += 1
        if page > 10:
            break
    return prs


def get_pr_files(pr_number, token):
    files = []
    page = 1
    while True:
        data = gh_get(f"/repos/{REPO}/pulls/{pr_number}/files?per_page=100&page={page}", token)
        if not data:
            break
        files.extend(f["filename"] for f in data)
        if len(data) < 100:
            break
        page += 1
    return files


def get_check_runs(sha, token):
    data = gh_get(f"/repos/{REPO}/commits/{sha}/check-runs?per_page=100", token)
    if not data:
        return []
    return data.get("check_runs", [])


def base_job_name(check_run_name: str) -> str:
    """Strip matrix suffixes like 'assert (v1.33.7)' -> 'assert'."""
    return re.split(r"\s*[\(/]", check_run_name, maxsplit=1)[0].strip()


def load_job_to_path_map():
    # Built from .github/workflows/tests-conformance.yaml -- see README for
    # how this was extracted. Kept as a static map here since it changes
    # rarely and re-parsing the workflow on every run adds another file dep.
    p = Path(__file__).parent / "ci_job_to_path.json"
    return json.loads(p.read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    ap.add_argument("--map", default=str(Path(__file__).parent / "path-test-map.yaml"))
    ap.add_argument("--out", default="ci_backtest_results.json")
    args = ap.parse_args()

    if not args.token:
        print("WARNING: no GITHUB_TOKEN set. Unauthenticated limit is 60 req/hr and is "
              "commonly already exhausted on shared CI/sandbox IPs. Proceeding anyway; "
              "expect early failures if the budget is gone.", file=sys.stderr)

    rules = load_rules(Path(args.map))
    job_to_path = load_job_to_path_map()
    prs = get_merged_prs(args.token, args.count)
    print(f"Fetched {len(prs)} merged PRs", file=sys.stderr)

    rows = []
    for pr in prs:
        number = pr["number"]
        files = get_pr_files(number, args.token)
        if not files:
            continue
        plan = scope(files, rules)
        predicted = {re.sub(r"/\*+$", "", s).rstrip("/") for s in plan["conformance_suites"]}

        sha = pr["head"]["sha"]
        check_runs = get_check_runs(sha, args.token)

        failed_jobs_in_scope, failed_jobs_out_of_scope, failed_jobs_unmapped = [], [], []
        for cr in check_runs:
            if cr.get("conclusion") not in ("failure", "timed_out"):
                continue
            base = base_job_name(cr["name"])
            job_path = job_to_path.get(base)
            if job_path is None:
                failed_jobs_unmapped.append(base)
                continue
            if job_path in predicted:
                failed_jobs_in_scope.append(base)
            else:
                failed_jobs_out_of_scope.append(base)

        rows.append({
            "pr_number": number,
            "title": pr["title"][:80],
            "n_files": len(files),
            "predicted_suites": sorted(predicted),
            "auto_merge_eligible": plan["auto_merge_eligible"],
            "requires_human_review": plan["requires_human_review"],
            "failed_jobs_caught_by_scope": failed_jobs_in_scope,
            "failed_jobs_MISSED_by_scope": failed_jobs_out_of_scope,
            "failed_jobs_unmapped": failed_jobs_unmapped,
        })
        time.sleep(0.2)  # be polite even when authenticated

    n = len(rows)
    total_failures_observed = sum(len(r["failed_jobs_caught_by_scope"]) + len(r["failed_jobs_MISSED_by_scope"]) for r in rows)
    total_missed = sum(len(r["failed_jobs_MISSED_by_scope"]) for r in rows)
    prs_with_a_miss = sum(1 for r in rows if r["failed_jobs_MISSED_by_scope"])

    summary = {
        "sample_size_prs": n,
        "total_ci_job_failures_observed": total_failures_observed,
        "total_missed_by_scoped_plan": total_missed,
        "prs_where_scope_would_have_missed_a_real_failure": prs_with_a_miss,
        "miss_rate_pct": round(100 * total_missed / total_failures_observed, 1) if total_failures_observed else None,
        "note": "miss_rate_pct is the number that should gate whether scoped CI ever becomes a real gate, not just a comment.",
    }

    Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"\nFull per-PR results written to {args.out}")


if __name__ == "__main__":
    main()
