"""Metric computation from the ingested database.

All metrics are defined over the commit set H stored at ingestion time: every
non-merge commit reachable from HEAD (the reference commit). Directory and
repository metrics are roll-ups of the per-file values over the subtree, as
defined in the spec (sum over immediate children, applied recursively).

Computed quantities per object o (file, directory, root):
    added / removed lines, growth, churn               (commit-set sums)
    modifications n_H,o, modification frequency n/|H|, churn rate lambda/|H|
Per author a and object o:
    author modifications n_H,o,a, author line counts, ownership (churn share).
These are computed for the whole repository as well as per file and directory.
"""
import threading

_cache = {}
_cache_lock = threading.Lock()


def _metrics(added, removed, modifications, commits):
    churn = added + removed
    return {
        "added": added,
        "removed": removed,
        "growth": added - removed,
        "churn": churn,
        "modifications": modifications,
        "modification_frequency": (modifications / commits) if commits else 0.0,
        "churn_rate": (churn / commits) if commits else 0.0,
    }


def compute(con, repo_id, version, commit_range=None):
    """Compute (and memoise) all metrics for a repository.

    commit_range is an optional (since_ts, until_ts) pair selecting a subset
    of H: since is inclusive, until exclusive, matching the specification's
    H_i,j / H_t commit sets. None means the full commit set.
    """
    key = (repo_id, version, commit_range)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached
    result = _compute(con, repo_id, commit_range)
    with _cache_lock:
        _cache[key] = result
    return result


def _compute(con, repo_id, commit_range=None):
    where = ""
    params = [repo_id]
    if commit_range is not None:
        since_ts, until_ts = commit_range
        if since_ts is not None:
            where += " AND committer_ts >= ?"
            params.append(since_ts)
        if until_ts is not None:
            where += " AND committer_ts < ?"
            params.append(until_ts)

    commits = con.execute(
        "SELECT COUNT(*) AS c FROM commits WHERE repo_id=?" + where, params
    ).fetchone()["c"]

    member_author_ids = None
    if commit_range is not None:
        member_author_ids = {
            row["author_id"]
            for row in con.execute(
                "SELECT DISTINCT author_id FROM commits WHERE repo_id=?" + where,
                params,
            )
        }

    authors = {}
    for row in con.execute(
        "SELECT id, name, email FROM authors WHERE repo_id=?", (repo_id,)
    ):
        if member_author_ids is not None and row["id"] not in member_author_ids:
            continue
        authors[row["id"]] = {
            "name": row["name"],
            "email": row["email"],
            "commits": 0,
            "added": 0,
            "removed": 0,
            "modified_shas": set(),
        }
    for row in con.execute(
        "SELECT author_id, COUNT(*) AS c FROM commits WHERE repo_id=?"
        + where
        + " GROUP BY author_id",
        params,
    ):
        if row["author_id"] in authors:
            authors[row["author_id"]]["commits"] = row["c"]

    sha_author = {
        row["sha"]: row["author_id"]
        for row in con.execute(
            "SELECT sha, author_id FROM commits WHERE repo_id=?" + where, params
        )
    }

    files = {}  # path -> [added, removed, modifications]
    dirs = {}   # directory path ("" = root) -> [added, removed, modifications]
    file_authored = {}  # (path, author_id) -> [added, removed, modifications]
    dir_authored = {}   # (directory, author_id) -> [added, removed, modifications]
    total_added = 0
    total_removed = 0
    live_sha = None
    changed_files = set()
    changed_dirs = set()

    def close_commit():
        if not changed_files:
            return
        for path in changed_files:
            files[path][2] += 1
        changed_dirs.add("")  # the root directory changes when any file changes
        for directory in changed_dirs:
            dirs[directory][2] += 1
        author_id = sha_author.get(live_sha)
        if author_id is not None:
            for path in changed_files:
                file_authored[(path, author_id)][2] += 1
            for directory in changed_dirs:
                dir_authored[(directory, author_id)][2] += 1

    rows = con.execute(
        "SELECT sha, path, added, removed FROM file_stats WHERE repo_id=? ORDER BY sha",
        (repo_id,),
    )
    for row in rows:
        sha = row["sha"]
        if commit_range is not None and sha not in sha_author:
            continue
        if sha != live_sha:
            close_commit()
            live_sha = sha
            changed_files = set()
            changed_dirs = set()
        added = row["added"]
        removed = row["removed"]
        path = row["path"]
        total_added += added
        total_removed += removed
        author_id = sha_author.get(sha)

        entry = files.get(path)
        if entry is None:
            entry = files[path] = [0, 0, 0]
        entry[0] += added
        entry[1] += removed

        if author_id is not None:
            file_entry = file_authored.get((path, author_id))
            if file_entry is None:
                file_entry = file_authored[(path, author_id)] = [0, 0, 0]
            file_entry[0] += added
            file_entry[1] += removed

        parts = path.split("/")
        for depth in range(1, len(parts)):
            directory = "/".join(parts[:depth])
            dir_entry = dirs.get(directory)
            if dir_entry is None:
                dir_entry = dirs[directory] = [0, 0, 0]
            dir_entry[0] += added
            dir_entry[1] += removed
            if author_id is not None:
                dir_entry_a = dir_authored.get((directory, author_id))
                if dir_entry_a is None:
                    dir_entry_a = dir_authored[(directory, author_id)] = [0, 0, 0]
                dir_entry_a[0] += added
                dir_entry_a[1] += removed
        root_entry = dirs.get("")
        if root_entry is None:
            root_entry = dirs[""] = [0, 0, 0]
        root_entry[0] += added
        root_entry[1] += removed
        if author_id is not None:
            root_entry_a = dir_authored.get(("", author_id))
            if root_entry_a is None:
                root_entry_a = dir_authored[("", author_id)] = [0, 0, 0]
            root_entry_a[0] += added
            root_entry_a[1] += removed

        if added + removed > 0:
            changed_files.add(path)
            for depth in range(1, len(parts)):
                changed_dirs.add("/".join(parts[:depth]))

        if author_id is not None:
            author = authors[author_id]
            author["added"] += added
            author["removed"] += removed
            if added + removed > 0:
                author["modified_shas"].add(sha)
    close_commit()

    root_modifications = dirs[""][2] if "" in dirs else 0
    summary = _metrics(total_added, total_removed, root_modifications, commits)
    summary.update(
        {
            "commits": commits,
            "authors": len(authors),
            "files": len(files),
            "directories": len(dirs),
        }
    )

    file_list = []
    for path, (added, removed, modifications) in files.items():
        row = _metrics(added, removed, modifications, commits)
        row["path"] = path
        file_list.append(row)
    file_list.sort(key=lambda item: (-item["churn"], item["path"]))

    dir_list = []
    for path, (added, removed, modifications) in dirs.items():
        row = _metrics(added, removed, modifications, commits)
        row["path"] = path
        dir_list.append(row)
    dir_list.sort(key=lambda item: item["path"])

    churn_total = total_added + total_removed
    author_list = []
    for author in authors.values():
        row = _metrics(
            author["added"], author["removed"], len(author["modified_shas"]), commits
        )
        author_churn = author["added"] + author["removed"]
        row.update(
            {
                "name": author["name"],
                "email": author["email"],
                "commits": author["commits"],
                "ownership": (author_churn / churn_total) if churn_total else 0.0,
            }
        )
        author_list.append(row)
    author_list.sort(key=lambda item: (-item["churn"], item["name"]))

    def object_author_rows(authored, totals):
        rows = []
        for (path, author_id), (added, removed, modifications) in authored.items():
            churn = added + removed
            author = authors.get(author_id)
            if churn <= 0 or author is None:
                continue
            total = totals[path][0] + totals[path][1]
            row = _metrics(added, removed, modifications, commits)
            row.update(
                {
                    "path": path,
                    "name": author["name"],
                    "email": author["email"],
                    "ownership": (churn / total) if total else 0.0,
                }
            )
            rows.append(row)
        rows.sort(key=lambda item: (item["path"], item["name"]))
        return rows

    return {
        "summary": summary,
        "files": file_list,
        "dirs": dir_list,
        "authors": author_list,
        "file_authors": object_author_rows(file_authored, files),
        "dir_authors": [
            row for row in object_author_rows(dir_authored, dirs) if row["path"]
        ],
    }
