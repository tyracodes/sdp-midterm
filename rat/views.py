"""HTTP endpoints: pages and JSON API."""
import calendar
import os
import shutil
import time
import uuid
from datetime import datetime

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import db, ingest
from . import metrics as metrics_mod

bp = Blueprint("main", __name__)


def _err(message, code):
    return jsonify(error=message), code


@bp.app_errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return _err("Not found.", 404)
    return (
        render_template(
            "error.html",
            code=404,
            title="Page not found",
            detail="The page or object you requested does not exist.",
        ),
        404,
    )


@bp.app_errorhandler(500)
def server_error(error):
    if request.path.startswith("/api/"):
        return _err("Internal server error.", 500)
    return (
        render_template(
            "error.html",
            code=500,
            title="Internal server error",
            detail="Something went wrong while processing the request.",
        ),
        500,
    )


# ------------------------------------------------------------------ pages

@bp.get("/health")
def health():
    con = db.connect()
    try:
        count = con.execute("SELECT COUNT(*) AS c FROM repos").fetchone()["c"]
    finally:
        con.close()
    return jsonify(status="ok", repos=count)


@bp.get("/")
def index():
    con = db.connect()
    try:
        repos = con.execute(
            """
            SELECT r.*, j.id AS job_id, j.message AS job_message
            FROM repos r
            LEFT JOIN jobs j ON j.id = (
                SELECT id FROM jobs WHERE repo_id = r.id ORDER BY id DESC LIMIT 1
            )
            ORDER BY r.id DESC
            """
        ).fetchall()
    finally:
        con.close()
    return render_template("index.html", repos=repos)


def _author_filter():
    """Parse the ?author= page parameter: 'Name <email>' or a bare name."""
    raw = (request.args.get("author") or "").strip()
    if not raw:
        return "", None, None
    if raw.endswith(">") and " <" in raw:
        name, _, email = raw[:-1].rpartition(" <")
        return raw, name, email
    return raw, raw, None


def _commit_range():
    """Parse ?since=/?until= (YYYY-MM-DD); since inclusive, until exclusive."""
    def _parse(name):
        raw = (request.args.get(name) or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return None
        return calendar.timegm(parsed.timetuple())

    since_ts = _parse("since")
    until_ts = _parse("until")
    if since_ts is None and until_ts is None:
        return None
    return (since_ts, until_ts)


_timeline_cache = {}


def _timeline(con, repo_id, version):
    """Monthly added/removed line totals (read-only, memoised per HEAD)."""
    key = (repo_id, version)
    cached = _timeline_cache.get(key)
    if cached is not None:
        return cached
    rows = con.execute(
        "SELECT strftime('%Y-%m', c.committer_ts, 'unixepoch') AS month, "
        "SUM(fs.added) AS added, SUM(fs.removed) AS removed "
        "FROM file_stats fs "
        "JOIN commits c ON c.repo_id = fs.repo_id AND c.sha = fs.sha "
        "WHERE fs.repo_id=? GROUP BY month ORDER BY month",
        (repo_id,),
    ).fetchall()
    timeline = [[row["month"], row["added"] or 0, row["removed"] or 0] for row in rows]
    _timeline_cache[key] = timeline
    return timeline


@bp.get("/repo/<int:repo_id>")
def repo_page(repo_id):
    con = db.connect()
    try:
        repo = con.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
        if repo is None:
            abort(404)
        job = con.execute(
            "SELECT * FROM jobs WHERE repo_id=? ORDER BY id DESC LIMIT 1", (repo_id,)
        ).fetchone()
        context = {"repo": repo, "job": job, "author_filter": ""}
        if repo["status"] == "ready":
            commit_range = _commit_range()
            data = metrics_mod.compute(con, repo_id, repo["head_sha"], commit_range)
            context["summary"] = data["summary"]
            context["authors_options"] = data["authors"]
            context["filter_since"] = request.args.get("since", "")
            context["filter_until"] = request.args.get("until", "")
            context["commit_set_active"] = commit_range is not None
            context["merges"] = con.execute(
                "SELECT id, from_name, from_email, to_name, to_email "
                "FROM author_merges WHERE repo_id=? ORDER BY id",
                (repo_id,),
            ).fetchall()
            author_filter, author_name, author_email = _author_filter()
            context["author_filter"] = author_filter
            context["author_summary"] = None
            if author_filter:
                # Author-scoped view: reuse the per-author-by-object rollups
                # already computed instead of recomputing metrics.
                for author in data["authors"]:
                    if author["name"] == author_name and (
                        author_email is None or author["email"] == author_email
                    ):
                        context["author_summary"] = author
                        break
                files = [
                    row
                    for row in data["file_authors"]
                    if row["name"] == author_name
                    and (author_email is None or row["email"] == author_email)
                ]
                dirs = [
                    row
                    for row in data["dir_authors"]
                    if row["name"] == author_name
                    and (author_email is None or row["email"] == author_email)
                ]
                files.sort(key=lambda row: (-row["churn"], row["path"]))
                dirs.sort(key=lambda row: row["path"])
                context["files"] = files[:500]
                context["files_total"] = len(files)
                context["dirs"] = dirs
                context["authors"] = (
                    [context["author_summary"]] if context["author_summary"] else []
                )
            else:
                context["files"] = data["files"][:500]
                context["files_total"] = len(data["files"])
                context["dirs"] = data["dirs"]
                context["authors"] = data["authors"]
                if commit_range is None:
                    # charts are repository-wide, so they only render in the
                    # unfiltered view
                    context["chart_data"] = {
                        "files": [
                            {"path": row["path"], "churn": row["churn"]}
                            for row in data["files"][:10]
                            if row["churn"] > 0
                        ],
                        "authors": [
                            {
                                "name": row["name"],
                                "churn": row["churn"],
                                "ownership": row["ownership"],
                            }
                            for row in data["authors"][:10]
                            if row["churn"] > 0
                        ],
                        "timeline": _timeline(con, repo_id, repo["head_sha"]),
                    }
        return render_template("repo.html", **context)
    finally:
        con.close()


@bp.get("/repo/<int:repo_id>/browse")
def browse_page(repo_id):
    """Read-only drill-down page for one file or directory."""
    obj_type = request.args.get("type") or "file"
    path = request.args.get("path") or ""
    if obj_type not in ("file", "dir"):
        abort(404)
    con = db.connect()
    try:
        repo = con.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
        if repo is None:
            abort(404)
        if repo["status"] != "ready":
            return redirect(url_for("main.repo_page", repo_id=repo_id))
        data = metrics_mod.compute(con, repo_id, repo["head_sha"])
    finally:
        con.close()

    if obj_type == "dir" and path == "":
        return redirect(url_for("main.repo_page", repo_id=repo_id))

    pool = data["files"] if obj_type == "file" else data["dirs"]
    row = next((item for item in pool if item["path"] == path), None)
    if row is None:
        return (
            render_template(
                "object.html", repo=repo, obj_type=obj_type, path=path, not_found=True
            ),
            404,
        )

    authored = data["file_authors"] if obj_type == "file" else data["dir_authors"]
    author_rows = [item for item in authored if item["path"] == path]
    author_rows.sort(key=lambda item: (-item["churn"], item["name"]))

    child_files = []
    child_dirs = []
    if obj_type == "dir":
        prefix = path + "/"
        for item in data["files"]:
            if item["path"].startswith(prefix):
                rest = item["path"][len(prefix):]
                if rest and "/" not in rest:
                    child_files.append(item)
        for item in data["dirs"]:
            if item["path"].startswith(prefix):
                rest = item["path"][len(prefix):]
                if rest and "/" not in rest:
                    child_dirs.append(item)
        child_files.sort(key=lambda item: (-item["churn"], item["path"]))
        child_dirs.sort(key=lambda item: item["path"])

    segments = path.split("/")[:-1] if obj_type == "file" else path.split("/")
    crumbs = ["/".join(segments[: i + 1]) for i in range(len(segments))]

    return render_template(
        "object.html",
        repo=repo,
        obj_type=obj_type,
        path=path,
        not_found=False,
        row=row,
        author_rows=author_rows,
        child_files=child_files[:500],
        child_files_total=len(child_files),
        child_dirs=child_dirs,
        crumbs=crumbs,
    )


def _identity(raw):
    """Parse 'Name <email>' into (name, email); None when malformed."""
    raw = (raw or "").strip()
    if raw.endswith(">") and " <" in raw:
        name, _, email = raw[:-1].rpartition(" <")
        return name, email
    return None


@bp.post("/repo/<int:repo_id>/merge")
def merge_authors(repo_id):
    """Manually merge one author identity into another (dashboard action)."""
    source = _identity(request.form.get("from"))
    target = _identity(request.form.get("to"))
    if source is None or target is None or source == target:
        return redirect(url_for("main.repo_page", repo_id=repo_id))
    con = db.connect()
    try:
        repo = con.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
        if repo is None:
            abort(404)
        # both identities must belong to this repository
        count = con.execute(
            "SELECT COUNT(*) AS c FROM authors WHERE repo_id=? AND "
            "((name=? AND email=?) OR (name=? AND email=?))",
            (repo_id, source[0], source[1], target[0], target[1]),
        ).fetchone()["c"]
        if count != 2:
            return redirect(url_for("main.repo_page", repo_id=repo_id))
        # reject merges that would create a cycle in the alias chain
        seen = {source}
        key = target
        for _ in range(100):
            if key in seen:
                return redirect(url_for("main.repo_page", repo_id=repo_id))
            seen.add(key)
            link = con.execute(
                "SELECT to_name, to_email FROM author_merges WHERE repo_id=? "
                "AND from_name=? AND from_email=?",
                (repo_id, key[0], key[1]),
            ).fetchone()
            if link is None:
                break
            key = (link["to_name"], link["to_email"])
        with con:
            con.execute(
                "INSERT OR REPLACE INTO author_merges "
                "(repo_id, from_name, from_email, to_name, to_email) "
                "VALUES (?,?,?,?,?)",
                (repo_id, source[0], source[1], target[0], target[1]),
            )
    finally:
        con.close()
    metrics_mod.invalidate(repo_id)
    return redirect(url_for("main.repo_page", repo_id=repo_id))


@bp.post("/repo/<int:repo_id>/unmerge")
def unmerge_authors(repo_id):
    """Remove one manual author merge (dashboard action)."""
    merge_id = request.form.get("merge_id", type=int)
    con = db.connect()
    try:
        with con:
            con.execute(
                "DELETE FROM author_merges WHERE id=? AND repo_id=?",
                (merge_id, repo_id),
            )
    finally:
        con.close()
    metrics_mod.invalidate(repo_id)
    return redirect(url_for("main.repo_page", repo_id=repo_id))


# ------------------------------------------------------------------ ingestion

@bp.post("/api/repos")
def create_repo():
    name = (request.form.get("name") or "").strip()
    url = (request.form.get("url") or "").strip()
    file = request.files.get("file")
    has_file = bool(file and file.filename)

    if has_file and url:
        return _err("Provide either a zip file or a URL, not both.", 400)
    if not has_file and not url:
        return _err("Provide a zip file or a repository URL.", 400)
    if url and url.startswith("-"):
        return _err("Invalid repository URL.", 400)
    if has_file:
        if not file.filename.lower().endswith(".zip"):
            return _err("Only .zip archives are accepted.", 400)
        source_type = "zip"
        source = os.path.basename(file.filename)
        default_name = os.path.splitext(source)[0]
    else:
        source_type = "url"
        source = url
        default_name = url.rstrip("/").split("/")[-1]
        if default_name.endswith(".git"):
            default_name = default_name[:-4]
    name = name or default_name or "repo"

    if not ingest.JOB_LOCK.acquire(blocking=False):
        return _err("Another ingestion is already running. Try again when it finishes.", 409)

    started = False
    try:
        response, started = _create_repo_locked(name, source_type, source, file)
        return response
    finally:
        if not started:
            ingest.JOB_LOCK.release()


def _create_repo_locked(name, source_type, source, file):
    """Create repo + job rows and start the worker. Returns (response, started)."""
    con = db.connect()
    try:
        existing = con.execute(
            "SELECT id, status FROM repos WHERE name=?", (name,)
        ).fetchone()
        if existing is not None:
            if existing["status"] != "error":
                return _err(f"A repository named '{name}' already exists.", 409), False
            # allow retrying a failed ingest under the same name
            con.execute("DELETE FROM repos WHERE id=?", (existing["id"],))
            con.commit()

        slug = ingest.slugify(name)
        repo_dest = os.path.join(current_app.config["DATA_DIR"], "repos", slug)
        if os.path.isdir(repo_dest):
            shutil.rmtree(repo_dest, ignore_errors=True)

        cursor = con.execute(
            "INSERT INTO repos (name, slug, source_type, source, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (name, slug, source_type, source, int(time.time())),
        )
        repo_id = cursor.lastrowid
        job_cursor = con.execute(
            "INSERT INTO jobs (repo_id, phase, status, message, started_at) "
            "VALUES (?, 'queued', 'running', 'Queued', ?)",
            (repo_id, int(time.time())),
        )
        job_id = job_cursor.lastrowid
        con.commit()

        if source_type == "zip":
            tmp_dir = os.path.join(current_app.config["DATA_DIR"], "tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            payload = os.path.join(tmp_dir, f"upload-{uuid.uuid4().hex}.zip")
            file.save(payload)
        else:
            payload = source

        ingest.start_ingest(
            current_app._get_current_object(), repo_id, job_id, source_type, payload
        )
        return jsonify(repo_id=repo_id, job_id=job_id), 201
    finally:
        con.close()


# ------------------------------------------------------------------ JSON API

@bp.get("/api/repos")
def list_repos():
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT id, name, slug, source_type, source, status, error, head_sha, commit_count "
            "FROM repos ORDER BY id DESC"
        ).fetchall()
    finally:
        con.close()
    return jsonify([dict(row) for row in rows])


def _repo_or_404(con, repo_id):
    repo = con.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
    if repo is None:
        abort(404)
    return repo


def _repo_metrics_or_409(con, repo_id):
    repo = _repo_or_404(con, repo_id)
    if repo["status"] != "ready":
        return repo, None
    return repo, metrics_mod.compute(con, repo_id, repo["head_sha"])


@bp.get("/api/repos/<int:repo_id>")
def repo_json(repo_id):
    con = db.connect()
    try:
        repo, data = _repo_metrics_or_409(con, repo_id)
        payload = dict(repo)
        if data is not None:
            payload["summary"] = data["summary"]
        return jsonify(payload)
    finally:
        con.close()


@bp.get("/api/repos/<int:repo_id>/files")
def repo_files(repo_id):
    con = db.connect()
    try:
        repo, data = _repo_metrics_or_409(con, repo_id)
        if data is None:
            return _err("Repository is not ready yet.", 409)
        files = data["files"]
        limit = request.args.get("limit", type=int)
        if limit is not None and limit >= 0:
            files = files[:limit]
        return jsonify(files)
    finally:
        con.close()


@bp.get("/api/repos/<int:repo_id>/dirs")
def repo_dirs(repo_id):
    con = db.connect()
    try:
        repo, data = _repo_metrics_or_409(con, repo_id)
        if data is None:
            return _err("Repository is not ready yet.", 409)
        return jsonify(data["dirs"])
    finally:
        con.close()


@bp.get("/api/repos/<int:repo_id>/file-authors")
def repo_file_authors(repo_id):
    con = db.connect()
    try:
        repo, data = _repo_metrics_or_409(con, repo_id)
        if data is None:
            return _err("Repository is not ready yet.", 409)
        return jsonify(data["file_authors"])
    finally:
        con.close()


@bp.get("/api/repos/<int:repo_id>/dir-authors")
def repo_dir_authors(repo_id):
    con = db.connect()
    try:
        repo, data = _repo_metrics_or_409(con, repo_id)
        if data is None:
            return _err("Repository is not ready yet.", 409)
        return jsonify(data["dir_authors"])
    finally:
        con.close()


@bp.get("/api/repos/<int:repo_id>/authors")
def repo_authors(repo_id):
    con = db.connect()
    try:
        repo, data = _repo_metrics_or_409(con, repo_id)
        if data is None:
            return _err("Repository is not ready yet.", 409)
        return jsonify(data["authors"])
    finally:
        con.close()


@bp.get("/api/jobs/<int:job_id>")
def job_json(job_id):
    con = db.connect()
    try:
        job = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return _err("Job not found.", 404)
        return jsonify(dict(job))
    finally:
        con.close()
