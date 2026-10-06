#!/usr/bin/env python3
"""Independently recompute key metrics from git and compare with the RAT database.

Checks:
  * |H| (number of non-merge commits), total added/removed lines over the full
    history reachable from HEAD
  * added/removed lines for a small sample of individual commits

The git-side numbers are computed with separate commands from the ingest path,
so a parser bug in RAT surfaces here. Exit code 0 = all checks passed.
"""
import argparse
import os
import sqlite3
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_git(git_path, args):
    return subprocess.run(["git", "-C", git_path, *args], capture_output=True, text=True)


def totals_from_git(git_path):
    proc = run_git(git_path, [
        "log", "--no-merges", "--root", "--numstat", "-M50%", "--format=@@@%H",
    ])
    if proc.returncode != 0:
        raise SystemExit("git log failed: " + proc.stderr.strip())
    added = removed = commits = 0
    for line in proc.stdout.splitlines():
        if line.startswith("@@@"):
            commits += 1
            continue
        parts = line.split("\t", 2)
        if len(parts) < 2 or parts[0] == "-":
            continue
        added += int(parts[0])
        removed += int(parts[1])
    return commits, added, removed


def per_commit_from_git(git_path, sha):
    proc = run_git(git_path, [
        "diff-tree", "--no-commit-id", "-r", "--numstat", "-M50%", "--root", sha,
    ])
    if proc.returncode != 0:
        raise SystemExit(f"git diff-tree failed for {sha}: " + proc.stderr.strip())
    added = removed = 0
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 2 or parts[0] == "-":
            continue
        added += int(parts[0])
        removed += int(parts[1])
    return added, removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="repository name as shown in RAT")
    parser.add_argument("--db", default=os.path.join(REPO_ROOT, "data", "rat.sqlite3"))
    parser.add_argument("--sample", type=int, default=3, help="individual commits to verify")
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    repo = con.execute("SELECT * FROM repos WHERE name=?", (args.repo,)).fetchone()
    if repo is None:
        raise SystemExit(f"no repository named '{args.repo}' in {args.db}")
    if repo["status"] != "ready":
        raise SystemExit(f"repository '{args.repo}' has status '{repo['status']}', not ready")

    git_path = os.path.join(
        os.path.dirname(os.path.abspath(args.db)), "repos", repo["slug"], repo["git_dir"] or ""
    )
    repo_id = repo["id"]
    failures = []

    def check(label, expected, actual):
        ok = expected == actual
        print(("PASS" if ok else "FAIL") + f"  {label}: git={expected} db={actual}")
        if not ok:
            failures.append(label)

    g_commits, g_added, g_removed = totals_from_git(git_path)
    d_commits = con.execute(
        "SELECT COUNT(*) AS c FROM commits WHERE repo_id=?", (repo_id,)
    ).fetchone()["c"]
    d_added, d_removed = con.execute(
        "SELECT COALESCE(SUM(added),0) AS a, COALESCE(SUM(removed),0) AS r "
        "FROM file_stats WHERE repo_id=?",
        (repo_id,),
    ).fetchone()

    check("commit count |H|", g_commits, d_commits)
    check("total added lines", g_added, d_added)
    check("total removed lines", g_removed, d_removed)

    shas = [
        row["sha"]
        for row in con.execute(
            "SELECT sha FROM commits WHERE repo_id=? ORDER BY sha LIMIT ?",
            (repo_id, args.sample),
        )
    ]
    for sha in shas:
        expected_added, expected_removed = per_commit_from_git(git_path, sha)
        row = con.execute(
            "SELECT COALESCE(SUM(added),0) AS a, COALESCE(SUM(removed),0) AS r "
            "FROM file_stats WHERE repo_id=? AND sha=?",
            (repo_id, sha),
        ).fetchone()
        check(f"commit {sha[:12]} added", expected_added, row["a"])
        check(f"commit {sha[:12]} removed", expected_removed, row["r"])

    con.close()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
