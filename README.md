# RAT — Repository Analysis Tool

A web dashboard that ingests Git repositories and computes per-file,
per-directory, per-repository, per-commit-set and per-author metrics.

## Prerequisites

- Python 3.10+
- `git` on the PATH

## Setup (fresh clone)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
flask --app rat run --port 5000
```

Open <http://localhost:5000>.

Configuration (environment variables):

| Variable       | Default           | Meaning                                        |
| -------------- | ----------------- | ---------------------------------------------- |
| `RAT_DATA_DIR` | `./data`          | SQLite database + cloned/extracted repositories |

`data/` is created automatically and is gitignored; it holds `rat.sqlite3`
and `repos/<slug>/`.

## Usage

1. **Upload a zip** — a zip of a repository *including its `.git` directory*
   at the top level or inside a single wrapping folder.
2. **Clone a repository** — paste a Git URL; it is cloned with its full
   history (`git clone --mirror`).
3. Ingestion runs in the background with live progress (clone → analyze).
4. Open the repository page for the metric dashboard.
5. **Filter** the dashboard by author (dropdown, deep-linkable via
   `?author=<name> <email>`); the file/directory tables then show that
   author's per-object metrics and ownership.
6. **Drill down**: click any file or directory to open its detail page with
   per-author breakdown (breadcrumbs, immediate children for directories).
7. Tables can be **searched** (text box above each table) and **sorted**
   (click a column header).

## Metrics

All metrics are computed over the commit set **H = every non-merge commit
reachable from HEAD**, following the test specification:

- diffs are taken against the parent commit; the initial commit is diffed
  against the empty tree;
- rename detection is enabled at 50% (`-M50%`) and changes are attributed to
  the **new path**; deleted objects are recorded as removed lines on their path;
- **binary files are not measured** (Git's own detection);
- merge commits are excluded.

Per commit and file: added lines, removed lines, growth (added − removed),
churn (added + removed). Directory metrics are the roll-up of the same
quantities over all files in the subtree; repository metrics are the root
directory's metrics. Commit-set metrics: `added/removed/growth/churn` sums,
modifications `n` (commits with churn > 0 on the object), modification
frequency `n / |H|`, churn rate `churn / |H|`. Per author: modifications,
churn and ownership (share of total churn).

## JSON API

Useful for scripting and verification:

- `GET /health`
- `GET /api/repos` · `GET /api/repos/<id>` (includes `summary` when ready)
- `GET /api/repos/<id>/files?limit=N` · `/dirs` · `/authors` · `/file-authors` · `/dir-authors`
- `GET /api/jobs/<id>` (progress)
- `POST /api/repos` (multipart: `name`? + `file` OR `url`)

## Verification

```bash
python3 scripts/verify_metrics.py --repo <name>
```

Recomputes totals and a sample of commits directly from git with independent
commands and compares them against the database.

```bash
python3 scripts/compare_reference.py --repo <name> --csv repo-references/<file>.csv
```

Compares every row of a reference CSV (`repo,ref_sha,commit_set,object_type,
path,author,added,removed,...`) against the live app API, including per-author
rows for file and directory objects. Exit code 0 means the whole file matches.

## Architecture

Flask (single process) + SQLite + the `git` CLI.

- Ingestion streams `git log --no-merges --root --numstat -z -M50%` once per
  repository into two SQLite tables (`commits` — non-merge commits with
  committer date and mailmap-canonicalised author identity (via `%aN`/`%aE`);
  `file_stats` — per commit, per path added/removed line counts).
- All dashboard metrics are derived from those tables (single pass over
  `file_stats` with roll-ups) and memoised per repository.
- Ingestion runs on a background thread with a job row for progress; one
  ingestion at a time.

## Current limitations (planned next steps)

- Filtering: author and file/directory (drill-down) are supported; time range
  and manual commit-list commit sets are not implemented yet.
- Author merging: the repository's `.mailmap` is honoured automatically
  (via `%aN`/`%aE`); manual author merging is not available yet.
- File table shows the top 500 rows by churn in the UI (API returns all).
