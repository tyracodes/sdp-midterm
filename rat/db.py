"""SQLite schema and connection helpers."""
import os
import sqlite3

from flask import current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source TEXT,
    git_dir TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    head_sha TEXT,
    commit_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    UNIQUE (repo_id, name, email)
);

CREATE TABLE IF NOT EXISTS commits (
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    author_id INTEGER NOT NULL REFERENCES authors(id),
    committer_ts INTEGER NOT NULL,
    parent_sha TEXT,
    PRIMARY KEY (repo_id, sha)
);

CREATE INDEX IF NOT EXISTS idx_commits_repo_ts ON commits(repo_id, committer_ts);
CREATE INDEX IF NOT EXISTS idx_commits_author ON commits(repo_id, author_id);

CREATE TABLE IF NOT EXISTS file_stats (
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    path TEXT NOT NULL,
    added INTEGER NOT NULL,
    removed INTEGER NOT NULL,
    PRIMARY KEY (repo_id, sha, path)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    phase TEXT NOT NULL DEFAULT 'queued',
    message TEXT,
    processed INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    started_at INTEGER,
    finished_at INTEGER
);
"""


def db_path():
    return os.path.join(current_app.config["DATA_DIR"], "rat.sqlite3")


def connect():
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db():
    con = connect()
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()
