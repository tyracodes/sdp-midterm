"""HTTP endpoints: pages and JSON API."""
import os
import shutil
import time
import uuid

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
)

from . import db, ingest
from . import metrics as metrics_mod

bp = Blueprint("main", __name__)


def _err(message, code):
    return jsonify(error=message), code


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
        context = {"repo": repo, "job": job}
        if repo["status"] == "ready":
            data = metrics_mod.compute(con, repo_id, repo["head_sha"])
            context["summary"] = data["summary"]
            context["files"] = data["files"][:500]
            context["files_total"] = len(data["files"])
            context["dirs"] = data["dirs"]
            context["authors"] = data["authors"]
        return render_template("repo.html", **context)
    finally:
        con.close()


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
