"""RAT — Repository Analysis Tool (Flask application factory)."""
import os

from flask import Flask


def create_app(test_config=None):
    app = Flask(__name__)
    # Default data directory: <repo root>/data. Override with the RAT_DATA_DIR
    # environment variable. Holds the SQLite database and ingested repositories.
    default_data = os.path.join(os.path.dirname(app.root_path), "data")
    app.config.from_mapping(
        DATA_DIR=os.environ.get("RAT_DATA_DIR", default_data),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    from . import db

    with app.app_context():
        db.init_db()

    from . import views

    app.register_blueprint(views.bp)
    return app
