"""Repository ingestion: zip upload / clone URL, streamed into SQLite.

History is read in a single `git log --numstat -M50%` pass. Rename detection
is enabled at 50%; changes are attributed to the new path. Binary entries
(numbers reported as "-" by git) are skipped: the spec excludes binary files
from measurement. Committer dates, parent shas and raw author identity
(name + email) are stored for every non-merge commit reachable from HEAD.
"""
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile

from flask import current_app

from . import db

# One ingestion at a time; released by the worker thread (or by the request
# handler if the ingest never starts).
JOB_LOCK = threading.Lock()

SLUG_RE = re.compile(r"[^a-z0-9]+")

# %aN/%aE are mailmap-aware: author identities are canonicalised exactly as
# git-shortlog would (the repo's .mailmap is honoured, incl. bare mirrors).
LOG_FORMAT = "C%x00%H%x00%ct%x00%aN%x00%aE%x00%P"


class IngestError(Exception):
    """A user-facing ingestion failure."""


def slugify(name):
    slug = SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return slug or "repo"


def start_ingest(app, repo_id, job_id, source_type, payload):
    thread = threading.Thread(
        target=_worker,
        args=(app, repo_id, job_id, source_type, payload),
        daemon=True,
    )
    thread.start()


# ------------------------------------------------------------------ git stream

def _tokens(stream):
    """Yield NUL-separated tokens from the git log byte stream."""
    buf = b""
    while True:
        chunk = stream.read(1 << 20)
        if not chunk:
            break
        buf += chunk
        parts = buf.split(b"\x00")
        buf = parts.pop()
        for part in parts:
            yield part
    if buf:
        yield buf


def iter_log(stream):
    """Parse `git log --no-merges --root --numstat -z -M50%` output.

    Emits ("commit", sha, committer_ts, author_name, author_email, parent)
    followed by ("file", sha, path, added, removed) events. Renames are
    attributed to the new path; the old path is kept as a zero-metric object.
    Binary entries (numstat "-") are skipped.
    """
    tokens = _tokens(stream)
    token = next(tokens, None)
    while token is not None:
        if token != b"C":
            raise IngestError("git log stream malformed: expected a commit header")
        sha_b = next(tokens, None)
        ts_b = next(tokens, None)
        an_b = next(tokens, None)
        ae_b = next(tokens, None)
        parents_b = next(tokens, None)
        if None in (sha_b, ts_b, an_b, ae_b, parents_b):
            raise IngestError("git log stream truncated inside a commit header")
        try:
            committer_ts = int(ts_b)
        except ValueError:
            raise IngestError("git log stream malformed: bad committer timestamp")
        parent = parents_b.split(b" ", 1)[0].decode("utf-8", "replace") or None
        sha = sha_b.decode("ascii", "replace")
        yield (
            "commit",
            sha,
            committer_ts,
            an_b.decode("utf-8", "replace"),
            ae_b.decode("utf-8", "replace"),
            parent,
        )

        token = next(tokens, None)
        if token is not None and token.startswith(b"\n"):
            # git separates the format line from the first numstat entry with
            # a newline byte; it survives NUL splitting glued to the entry.
            token = token[1:]
        while token is not None:
            if token == b"":
                token = next(tokens, None)
                continue
            if token == b"C":
                break  # start of the next commit header
            parts = token.split(b"\t", 2)
            if len(parts) != 3:
                raise IngestError("git log stream malformed: bad numstat entry")
            added_b, removed_b, rest = parts
            if rest == b"":
                # rename entry: old and new paths follow, NUL separated
                old_b = next(tokens, None)
                new_b = next(tokens, None)
                if old_b is None or new_b is None:
                    raise IngestError("git log stream truncated inside a rename entry")
                path_b = new_b
                old_path_b = old_b
            else:
                path_b = rest
                old_path_b = None
            if added_b != b"-":
                try:
                    added = int(added_b)
                    removed = int(removed_b)
                except ValueError:
                    raise IngestError("git log stream malformed: bad numstat numbers")
                if old_path_b is not None and old_path_b != path_b:
                    # the rename source still exists in the numstat stream (its
                    # lines are attributed to the new path); keep it as an
                    # object with zero metrics, mirroring the reference output.
                    yield ("file", sha, old_path_b.decode("utf-8", "replace"), 0, 0)
                yield ("file", sha, path_b.decode("utf-8", "replace"), added, removed)
            token = next(tokens, None)


# ------------------------------------------------------------------ extraction

def _find_git_dir(dest):
    """Locate the repository inside `dest`.

    Returns the path of the repository root relative to `dest` ("" when the
    .git directory is directly in `dest`).
    """
    git_entry = os.path.join(dest, ".git")
    if os.path.isdir(git_entry):
        return ""
    if os.path.isfile(git_entry):
        raise IngestError(
            "The zip contains a .git file (a worktree pointer) instead of a full "
            ".git directory. Upload a zip of a normal clone."
        )
    candidates = []
    for name in sorted(os.listdir(dest)):
        candidate = os.path.join(dest, name)
        if os.path.isdir(candidate) and os.path.isdir(os.path.join(candidate, ".git")):
            candidates.append(name)
        elif os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, ".git")):
            raise IngestError(
                "The zip contains a .git file (a worktree pointer) instead of a full "
                ".git directory. Upload a zip of a normal clone."
            )
    if len(candidates) > 1:
        raise IngestError("The zip contains more than one repository. Upload a single repository.")
    if not candidates:
        raise IngestError("No .git directory found in the zip.")
    sub = candidates[0]
    sub_path = os.path.join(dest, sub)
    try:
        # flatten a single wrapping directory so the repo lives directly in dest
        for item in os.listdir(sub_path):
            shutil.move(os.path.join(sub_path, item), os.path.join(dest, item))
        os.rmdir(sub_path)
        return ""
    except OSError:
        return sub


def _extract_zip(zip_path, dest):
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise IngestError("The uploaded file is not a valid zip archive.")
    with archive:
        try:
            archive.extractall(dest)
        except (zipfile.BadZipFile, OSError) as exc:
            raise IngestError(f"The zip archive could not be extracted: {exc}")
    return _find_git_dir(dest)


# ------------------------------------------------------------------ worker

def _update_job(con, job_id, **fields):
    if not fields:
        return
    columns = ", ".join(f"{key}=?" for key in fields)
    con.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*fields.values(), job_id))
    con.commit()


def _fail(con, repo_id, job_id, message):
    con.execute("UPDATE repos SET status='error', error=? WHERE id=?", (message, repo_id))
    con.execute(
        "UPDATE jobs SET status='error', error=?, finished_at=? WHERE id=?",
        (message, int(time.time()), job_id),
    )
    con.commit()


def _author_id(con, cache, repo_id, name, email):
    key = (name, email)
    author_id = cache.get(key)
    if author_id is None:
        con.execute(
            "INSERT OR IGNORE INTO authors (repo_id, name, email) VALUES (?, ?, ?)",
            (repo_id, name, email),
        )
        row = con.execute(
            "SELECT id FROM authors WHERE repo_id=? AND name=? AND email=?",
            (repo_id, name, email),
        ).fetchone()
        author_id = row["id"]
        cache[key] = author_id
    return author_id


def _run_git(git_path, args):
    return subprocess.run(["git", "-C", git_path, *args], capture_output=True, text=True)


def _analyze(con, repo_id, job_id, git_path, total):
    command = [
        "git", "-C", git_path,
        "-c", "log.showSignature=false",
        "-c", "diff.renameLimit=10000",
        "log", "--no-merges", "--root", "--numstat", "-z", "-M50%",
        "--format=" + LOG_FORMAT,
    ]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    author_cache = {}
    pending_rows = []
    processed = 0
    last_tick = time.time()
    try:
        for event in iter_log(proc.stdout):
            if event[0] == "commit":
                _, sha, committer_ts, author_name, author_email, parent = event
                author_id = _author_id(con, author_cache, repo_id, author_name, author_email)
                con.execute(
                    "INSERT OR IGNORE INTO commits "
                    "(repo_id, sha, author_id, committer_ts, parent_sha) VALUES (?, ?, ?, ?, ?)",
                    (repo_id, sha, author_id, committer_ts, parent),
                )
                processed += 1
                if processed % 2000 == 0 or time.time() - last_tick > 0.7:
                    con.commit()
                    _update_job(
                        con, job_id, processed=processed,
                        message=f"Analyzing commits ({processed}/{total})...",
                    )
                    last_tick = time.time()
            else:
                _, sha, path, added, removed = event
                pending_rows.append((repo_id, sha, path, added, removed))
                if len(pending_rows) >= 5000:
                    con.executemany(
                        "INSERT OR IGNORE INTO file_stats "
                        "(repo_id, sha, path, added, removed) VALUES (?, ?, ?, ?, ?)",
                        pending_rows,
                    )
                    pending_rows.clear()
        if pending_rows:
            con.executemany(
                "INSERT OR IGNORE INTO file_stats "
                "(repo_id, sha, path, added, removed) VALUES (?, ?, ?, ?, ?)",
                pending_rows,
            )
            pending_rows.clear()
        con.commit()
        stderr_text = proc.stderr.read().decode("utf-8", "replace")
        returncode = proc.wait()
        if returncode != 0:
            tail = stderr_text.strip().splitlines()
            raise IngestError("git log failed: " + (tail[-1] if tail else f"exit code {returncode}"))
    finally:
        proc.stdout.close()
        proc.stderr.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _run_worker(repo_id, job_id, source_type, payload):
    data_dir = current_app.config["DATA_DIR"]
    zip_to_remove = payload if source_type == "zip" else None
    con = db.connect()
    try:
        row = con.execute("SELECT slug FROM repos WHERE id=?", (repo_id,)).fetchone()
        if row is None:
            raise IngestError("Repository record disappeared.")
        dest = os.path.join(data_dir, "repos", row["slug"])
        os.makedirs(dest, exist_ok=True)

        if source_type == "url":
            _update_job(con, job_id, phase="clone",
                        message="Cloning repository (full history)...")
            proc = subprocess.run(
                ["git", "clone", "--mirror", "--", payload, dest],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                tail = (proc.stderr or "").strip().splitlines()
                raise IngestError("git clone failed: " + (tail[-1] if tail else "unknown error"))
            git_dir_rel = ""
        else:
            _update_job(con, job_id, phase="extract", message="Extracting archive...")
            git_dir_rel = _extract_zip(payload, dest)

        git_path = os.path.join(dest, git_dir_rel) if git_dir_rel else dest
        head = _run_git(git_path, ["rev-parse", "--verify", "HEAD"])
        if head.returncode != 0:
            raise IngestError("This git repository has no commits, or its .git data is unusable.")
        head_sha = head.stdout.strip()
        con.execute("UPDATE repos SET git_dir=? WHERE id=?", (git_dir_rel, repo_id))
        con.commit()

        count = _run_git(git_path, ["rev-list", "--count", "--no-merges", "HEAD"])
        total = 0
        if count.returncode == 0 and count.stdout.strip():
            total = int(count.stdout.strip())
        _update_job(con, job_id, phase="analyze", total=total, processed=0,
                    message=f"Analyzing {total} commits...")

        _analyze(con, repo_id, job_id, git_path, total)

        con.execute(
            "UPDATE repos SET status='ready', head_sha=?, commit_count=?, error=NULL WHERE id=?",
            (head_sha, total, repo_id),
        )
        con.commit()
        _update_job(con, job_id, phase="done", status="done", processed=total,
                    message="Ingestion complete.", finished_at=int(time.time()))
    except IngestError as exc:
        _fail(con, repo_id, job_id, str(exc))
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        current_app.logger.exception("ingestion failed")
        _fail(con, repo_id, job_id, f"Unexpected error: {type(exc).__name__}: {exc}")
    finally:
        con.close()
        if zip_to_remove and os.path.exists(zip_to_remove):
            try:
                os.unlink(zip_to_remove)
            except OSError:
                pass


def _worker(app, repo_id, job_id, source_type, payload):
    try:
        with app.app_context():
            _run_worker(repo_id, job_id, source_type, payload)
    except Exception:  # noqa: BLE001 - never let the worker die silently
        app.logger.exception("ingestion worker crashed")
        try:
            with app.app_context():
                con = db.connect()
                try:
                    _fail(con, repo_id, job_id,
                          "Internal error during ingestion; see the server log.")
                finally:
                    con.close()
        except Exception:  # noqa: BLE001
            app.logger.exception("failed to record ingestion failure")
    finally:
        try:
            JOB_LOCK.release()
        except RuntimeError:
            pass
