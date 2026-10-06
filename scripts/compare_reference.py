#!/usr/bin/env python3
"""Compare live RAT metrics (via its JSON API) against a reference CSV.

Reference CSV format:
  repo,ref_sha,commit_set,commit_count,object_type,path,author,added,removed,
  growth,churn,modifications,modification_frequency,churn_rate,ownership

Usage:
  python3 scripts/compare_reference.py --repo cjson-fix \
      --csv repo-references/cJSON_6d9f2443ab07.csv [--base-url http://localhost:5000]

Exit code 0 = all rows match (within float tolerance), otherwise the number
of mismatches (capped at 20 reported).
"""
import argparse
import csv
import json
import sys
import urllib.request

FLOAT_KEYS = ("modification_frequency", "churn_rate", "ownership")
INT_KEYS = ("added", "removed", "growth", "churn", "modifications")


def get(url):
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def norm_path(p):
    p = (p or "").strip()
    p = p.strip("/")
    return p


def to_num(s):
    s = (s or "").strip()
    if s == "":
        return None
    f = float(s)
    return int(f) if f.is_integer() else f


def close(a, b):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    scale = max(1.0, abs(a), abs(b))
    return abs(a - b) <= 1e-9 * scale


def split_author(who):
    """Parse 'Name <email>' into (name, email)."""
    name, email = who, ""
    if "<" in who and who.endswith(">"):
        name, email = who.rsplit("<", 1)
        name, email = name.strip(), email[:-1].strip()
    return name, email


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="RAT repo name")
    ap.add_argument("--csv", required=True, help="reference CSV path")
    ap.add_argument("--base-url", default="http://localhost:5000")
    args = ap.parse_args()

    with open(args.csv, newline="") as fh:
        rows = list(csv.DictReader(fh))
    print(f"reference rows: {len(rows)}")

    base = args.base_url.rstrip("/")
    repos = {r["name"]: r["id"] for r in get(base + "/api/repos")}

    rid = repos.get(args.repo)
    if rid is None:
        sys.exit(f"repo {args.repo!r} not found in app")

    summary = get(f"{base}/api/repos/{rid}")["summary"]
    files = {norm_path(f["path"]): f for f in get(f"{base}/api/repos/{rid}/files")}
    dirs = {norm_path(d["path"]): d for d in get(f"{base}/api/repos/{rid}/dirs")}
    authors = {}
    for a in get(f"{base}/api/repos/{rid}/authors"):
        key = a.get("email") or ""
        authors[(a["name"], key)] = a
    file_authors = {
        (norm_path(f["path"]), f["name"], f.get("email") or ""): f
        for f in get(f"{base}/api/repos/{rid}/file-authors")
    }
    dir_authors = {
        (norm_path(f["path"]), f["name"], f.get("email") or ""): f
        for f in get(f"{base}/api/repos/{rid}/dir-authors")
    }

    mismatches = 0

    def check(label, expected, actual):
        nonlocal mismatches
        if not close(expected, actual):
            mismatches += 1
            if mismatches <= 20:
                print(f"MISMATCH {label}: ref={expected!r} app={actual!r}")

    for row in rows:
        if row["commit_set"] != "all":
            continue
        otype = row["object_type"]
        path = row["path"]
        who = row["author"]

        for k in INT_KEYS + FLOAT_KEYS:
            exp = to_num(row.get(k))
            if exp is None:
                continue

            if otype == "repository" and who == "ALL":
                got = summary.get(k)
            elif otype == "directory" and who == "ALL":
                got = dirs.get(norm_path(path), {}).get(k)
            elif otype == "file" and who == "ALL":
                got = files.get(norm_path(path), {}).get(k)
            elif otype == "repository" and who not in ("ALL", ""):
                got = authors.get(split_author(who), {}).get(k)
            elif otype == "file" and who not in ("ALL", ""):
                got = file_authors.get((norm_path(path),) + split_author(who), {}).get(
                    k
                )
            elif otype == "directory" and who not in ("ALL", ""):
                got = dir_authors.get((norm_path(path),) + split_author(who), {}).get(k)
            else:
                continue

            label = f"{otype} {path or '/'} [{who}] {k}"
            check(label, exp, got)

    print(f"total mismatches: {mismatches}")
    if mismatches:
        sys.exit(min(mismatches, 120))
    print("All reference rows match.")


if __name__ == "__main__":
    main()
