from __future__ import annotations

import os
import re
import secrets
import sqlite3
import mimetypes
import json
from datetime import datetime, timezone
from functools import wraps
from pathlib import PurePosixPath
from threading import Lock
from urllib.parse import urlsplit
from uuid import uuid4

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, generate_csrf
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "notes.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "data", "uploads")
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "pdf", "txt", "csv", "docx", "xlsx",
    "webm", "ogg", "mp4", "m4a", "mp3", "wav",
}
AUDIO_MIME_TYPES = {
    "webm": "audio/webm",
    "ogg": "audio/ogg",
    "mp4": "audio/mp4",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
THEMES = {
    "red", "orange", "yellow", "green", "blue", "indigo", "violet", "black", "white"
}
IS_PRODUCTION = os.environ.get("APP_ENV") == "production"
if IS_PRODUCTION and not os.environ.get("SECRET_KEY"):
    raise RuntimeError("SECRET_KEY must be configured in production.")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("APP_ENV") == "production",
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,
)
csrf = CSRFProtect(app)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
)
_initialized_db_paths: set[str] = set()
_database_init_lock = Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_error: Exception | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    user_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
    }
    if "email" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "theme" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'white'")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique "
        "ON users(email COLLATE NOCASE) WHERE email IS NOT NULL"
    )
    columns = {
        row["name"] for row in db.execute("PRAGMA table_info(notes)").fetchall()
    }
    migrations = {
        "is_pinned": "ALTER TABLE notes ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0",
        "archived_at": "ALTER TABLE notes ADD COLUMN archived_at TEXT",
        "deleted_at": "ALTER TABLE notes ADD COLUMN deleted_at TEXT",
        "revision": "ALTER TABLE notes ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
    }
    for column, statement in migrations.items():
        if column not in columns:
            db.execute(statement)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(user_id, name),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS note_tags (
            note_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY(note_id, tag_id),
            FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE,
            FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_notes_owner_updated ON notes(user_id, updated_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tags_owner_name ON tags(user_id, name)")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_attachments_note_owner ON attachments(note_id, user_id)")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS note_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            version_number INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(note_id, version_number),
            FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_note_revisions_owner_note "
        "ON note_revisions(user_id, note_id, version_number DESC)"
    )
    notes_without_history = db.execute(
        "SELECT notes.id, notes.user_id, notes.title, notes.content "
        "FROM notes LEFT JOIN note_revisions ON note_revisions.note_id = notes.id "
        "WHERE note_revisions.id IS NULL"
    ).fetchall()
    for note in notes_without_history:
        current_tags = [row["name"] for row in db.execute(
            "SELECT tags.name FROM tags JOIN note_tags ON note_tags.tag_id = tags.id "
            "WHERE note_tags.note_id = ? ORDER BY tags.name COLLATE NOCASE",
            (note["id"],),
        ).fetchall()]
        db.execute(
            "INSERT INTO note_revisions "
            "(note_id, user_id, version_number, title, content, tags_json, created_at) "
            "VALUES (?, ?, 1, ?, ?, ?, ?)",
            (note["id"], note["user_id"], note["title"], note["content"],
             json.dumps(current_tags, ensure_ascii=False), utc_now()),
        )
    db.commit()


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("請先登入。", "warning")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped


def note_tags(db: sqlite3.Connection, note_id: int) -> list[str]:
    rows = db.execute(
        """
        SELECT tags.name
        FROM tags JOIN note_tags ON note_tags.tag_id = tags.id
        WHERE note_tags.note_id = ?
        ORDER BY tags.name COLLATE NOCASE
        """,
        (note_id,),
    ).fetchall()
    return [row["name"] for row in rows]


def create_note_revision(
    db: sqlite3.Connection,
    note_id: int,
    user_id: int,
    title: str,
    content: str,
    tags: list[str],
    created_at: str | None = None,
) -> int:
    latest = db.execute(
        "SELECT COALESCE(MAX(version_number), 0) AS version_number "
        "FROM note_revisions WHERE note_id = ? AND user_id = ?",
        (note_id, user_id),
    ).fetchone()
    version_number = latest["version_number"] + 1
    db.execute(
        "INSERT INTO note_revisions "
        "(note_id, user_id, version_number, title, content, tags_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (note_id, user_id, version_number, title, content,
         json.dumps(sorted(set(tags), key=str.casefold), ensure_ascii=False),
         created_at or utc_now()),
    )
    return version_number


def save_note_tags(
    db: sqlite3.Connection, user_id: int, note_id: int, raw_tags: str
) -> None:
    names = list(dict.fromkeys(
        name.strip()[:30] for name in raw_tags.split(",") if name.strip()
    ))[:10]
    db.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
    for name in names:
        db.execute(
            "INSERT OR IGNORE INTO tags (user_id, name) VALUES (?, ?)",
            (user_id, name),
        )
        tag = db.execute(
            "SELECT id FROM tags WHERE user_id = ? AND name = ?", (user_id, name)
        ).fetchone()
        db.execute(
            "INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?, ?)",
            (note_id, tag["id"]),
        )


@app.before_request
def ensure_db_ready() -> None:
    path = os.path.abspath(DB_PATH)
    if path not in _initialized_db_paths:
        with _database_init_lock:
            if path not in _initialized_db_paths:
                init_db()
                _initialized_db_paths.add(path)


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": generate_csrf}


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("notes"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if len(username) < 3 or len(username) > 40:
            flash("使用者名稱長度需為 3 至 40 個字元。", "danger")
            return render_template("register.html")

        if len(password) < 6 or len(password) > 128:
            flash("密碼長度需為 6 至 128 個字元。", "danger")
            return render_template("register.html")

        if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
            flash("請輸入有效的電子郵件地址。", "danger")
            return render_template("register.html")

        db = get_db()
        existing = db.execute(
            "SELECT id FROM users WHERE username = ? OR email = ? COLLATE NOCASE",
            (username, email),
        ).fetchone()

        if existing:
            flash("使用者名稱或電子郵件已被使用。", "danger")
            return render_template("register.html")

        db.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
            (username, email, generate_password_hash(password)),
        )
        db.commit()
        flash("註冊成功，請登入。", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        login_identifier = request.form.get("email", "").strip()
        normalized_email = login_identifier.lower()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE "
            "OR (email IS NULL AND username = ?)",
            (normalized_email, login_identifier),
        ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["theme"] = user["theme"] if user["theme"] in THEMES else "white"
            flash("登入成功。", "success")
            return redirect(url_for("notes"))

        flash("帳號或密碼錯誤。", "danger")

    return render_template("login.html")


@app.route("/settings/theme", methods=["POST"])
@login_required
def update_theme():
    theme = request.form.get("theme", "")
    if theme not in THEMES:
        flash("無效的背景主題。", "danger")
        return redirect(url_for("notes"))

    db = get_db()
    db.execute(
        "UPDATE users SET theme = ? WHERE id = ?",
        (theme, session["user_id"]),
    )
    db.commit()
    session["theme"] = theme
    return_to = request.form.get("return_to", "")
    parsed_return_to = urlsplit(return_to)
    if (
        not return_to.startswith("/")
        or return_to.startswith("//")
        or parsed_return_to.scheme
        or parsed_return_to.netloc
        or "\\" in return_to
    ):
        return_to = url_for("notes")
    return redirect(return_to)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("已登出。", "info")
    return redirect(url_for("login"))


@app.route("/notes")
@login_required
def notes():
    query = request.args.get("q", "").strip()
    view = request.args.get("view", "active")
    sort = request.args.get("sort", "updated")
    user_id = session["user_id"]
    db = get_db()
    view_conditions = {
        "active": "deleted_at IS NULL AND archived_at IS NULL",
        "archive": "deleted_at IS NULL AND archived_at IS NOT NULL",
        "trash": "deleted_at IS NOT NULL",
    }
    if view not in view_conditions:
        view = "active"
    order_by = {
        "updated": "updated_at DESC",
        "created": "created_at DESC",
        "title": "title COLLATE NOCASE ASC",
    }.get(sort, "updated_at DESC")
    sort = sort if sort in {"updated", "created", "title"} else "updated"
    conditions = ["user_id = ?", view_conditions[view]]
    params: list[object] = [user_id]
    selected_tag = request.args.get("tag", "").strip()
    if query:
        conditions.append("(title LIKE ? OR content LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    if selected_tag and view == "active":
        conditions.append(
            "id IN (SELECT note_id FROM note_tags JOIN tags ON tags.id = note_tags.tag_id "
            "WHERE tags.user_id = ? AND tags.name = ?)"
        )
        params.extend([user_id, selected_tag])
    notes_list = db.execute(
        f"SELECT * FROM notes WHERE {' AND '.join(conditions)} "
        f"ORDER BY is_pinned DESC, {order_by}",
        params,
    ).fetchall()
    tags = db.execute(
        "SELECT name FROM tags WHERE user_id = ? ORDER BY name COLLATE NOCASE", (user_id,)
    ).fetchall()
    tag_map = {note["id"]: note_tags(db, note["id"]) for note in notes_list}
    return render_template(
        "notes.html", notes=notes_list, query=query, view=view, sort=sort,
        tags=tags, tag_map=tag_map, selected_tag=selected_tag,
    )


@app.route("/notes/new", methods=["POST"])
@login_required
def new_note():
    db = get_db()
    now = utc_now()
    cursor = db.execute(
        "INSERT INTO notes (user_id, title, content, created_at, updated_at) VALUES (?, '', '', ?, ?)",
        (session["user_id"], now, now),
    )
    create_note_revision(db, cursor.lastrowid, session["user_id"], "", "", [], now)
    db.commit()
    return redirect(url_for("edit_note", note_id=cursor.lastrowid))


@app.route("/notes/<int:note_id>/edit", methods=["GET", "POST"])
@login_required
def edit_note(note_id: int):
    db = get_db()
    note = db.execute(
        "SELECT * FROM notes WHERE id = ? AND user_id = ?",
        (note_id, session["user_id"]),
    ).fetchone()

    if not note:
        flash("找不到筆記。", "danger")
        return redirect(url_for("notes"))

    tags = ", ".join(note_tags(db, note_id))
    attachments = db.execute(
        "SELECT * FROM attachments WHERE note_id = ? AND user_id = ? ORDER BY created_at",
        (note_id, session["user_id"]),
    ).fetchall()
    return render_template("note_form.html", note=note, tags=tags, attachments=attachments)


@app.route("/notes/<int:note_id>/history")
@login_required
def note_history(note_id: int):
    db = get_db()
    note = db.execute(
        "SELECT id, title FROM notes WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (note_id, session["user_id"]),
    ).fetchone()
    if not note:
        abort(404)
    revisions = db.execute(
        "SELECT * FROM note_revisions WHERE note_id = ? AND user_id = ? "
        "ORDER BY version_number DESC",
        (note_id, session["user_id"]),
    ).fetchall()
    revision_items = []
    for revision in revisions:
        item = dict(revision)
        item["tags"] = json.loads(item["tags_json"])
        revision_items.append(item)
    return render_template("note_history.html", note=note, revisions=revision_items)


@app.route("/notes/<int:note_id>/history/<int:revision_id>/restore", methods=["POST"])
@login_required
def restore_note_revision(note_id: int, revision_id: int):
    db = get_db()
    note = db.execute(
        "SELECT * FROM notes WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (note_id, session["user_id"]),
    ).fetchone()
    revision = db.execute(
        "SELECT * FROM note_revisions WHERE id = ? AND note_id = ? AND user_id = ?",
        (revision_id, note_id, session["user_id"]),
    ).fetchone()
    if not note or not revision:
        abort(404)

    restored_tags = json.loads(revision["tags_json"])
    now = utc_now()
    update = db.execute(
        "UPDATE notes SET title = ?, content = ?, updated_at = ?, revision = revision + 1 "
        "WHERE id = ? AND user_id = ? AND revision = ?",
        (revision["title"], revision["content"], now, note_id,
         session["user_id"], note["revision"]),
    )
    if update.rowcount != 1:
        db.rollback()
        flash("筆記已在其他位置更新，請重新載入後再還原。", "danger")
        return redirect(url_for("note_history", note_id=note_id))
    save_note_tags(db, session["user_id"], note_id, ",".join(restored_tags))
    create_note_revision(
        db, note_id, session["user_id"], revision["title"],
        revision["content"], restored_tags, now,
    )
    db.commit()
    flash(f"已還原版本 {revision['version_number']}；還原前的版本也已保留。", "success")
    return redirect(url_for("edit_note", note_id=note_id))


@app.route("/notes/<int:note_id>/attachments", methods=["POST"])
@login_required
def upload_attachment(note_id: int):
    db = get_db()
    note = db.execute(
        "SELECT id FROM notes WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (note_id, session["user_id"]),
    ).fetchone()
    if not note:
        abort(404)

    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        flash("請先選擇圖片或檔案。", "danger")
        return redirect(url_for("edit_note", note_id=note_id))

    original_name = uploaded.filename.replace("\\", "/").rsplit("/", 1)[-1]
    original_name = "".join(character for character in original_name if character.isprintable())[:255]
    safe_name = secure_filename(original_name)
    extension = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
    if not safe_name or extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
        flash("不支援此檔案格式。可上傳圖片、常見文件或 WEBM、OGG、MP4、M4A、MP3、WAV 音訊。", "danger")
        return redirect(url_for("edit_note", note_id=note_id))

    file_content = uploaded.stream.read(MAX_ATTACHMENT_SIZE + 1)
    size_bytes = len(file_content)
    if size_bytes > MAX_ATTACHMENT_SIZE:
        flash("檔案超過 10 MB 限制。", "danger")
        return redirect(url_for("edit_note", note_id=note_id))
    if not file_content:
        flash("空白檔案無法上傳。", "danger")
        return redirect(url_for("edit_note", note_id=note_id))

    user_directory = os.path.join(UPLOAD_DIR, str(session["user_id"]))
    os.makedirs(user_directory, exist_ok=True)
    stored_name = f"{uuid4().hex}.{extension}"
    storage_path = os.path.join(user_directory, stored_name)
    try:
        with open(storage_path, "xb") as stored_file:
            stored_file.write(file_content)
        mime_type = AUDIO_MIME_TYPES.get(extension) or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        db.execute(
            "INSERT INTO attachments "
            "(note_id, user_id, original_name, stored_name, mime_type, size_bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (note_id, session["user_id"], original_name, stored_name, mime_type, size_bytes, utc_now()),
        )
        db.commit()
    except (OSError, sqlite3.Error):
        db.rollback()
        if os.path.exists(storage_path):
            os.remove(storage_path)
        flash("檔案儲存失敗，請稍後重試。", "danger")
        return redirect(url_for("edit_note", note_id=note_id))

    flash("附件已加入筆記。", "success")
    return redirect(url_for("edit_note", note_id=note_id))


@app.route("/attachments/<int:attachment_id>")
@login_required
def get_attachment(attachment_id: int):
    db = get_db()
    attachment = db.execute(
        "SELECT attachments.* FROM attachments "
        "JOIN notes ON notes.id = attachments.note_id "
        "WHERE attachments.id = ? AND attachments.user_id = ? "
        "AND notes.user_id = ? AND notes.deleted_at IS NULL",
        (attachment_id, session["user_id"], session["user_id"]),
    ).fetchone()
    if not attachment:
        abort(404)

    is_previewable = attachment["mime_type"].startswith(("image/", "audio/"))
    inline = request.args.get("preview") == "1" and is_previewable
    response = send_from_directory(
        os.path.join(UPLOAD_DIR, str(session["user_id"])),
        attachment["stored_name"],
        mimetype=attachment["mime_type"],
        as_attachment=not inline,
        download_name=attachment["original_name"],
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.route("/attachments/<int:attachment_id>/delete", methods=["POST"])
@login_required
def delete_attachment(attachment_id: int):
    db = get_db()
    attachment = db.execute(
        "SELECT * FROM attachments WHERE id = ? AND user_id = ?",
        (attachment_id, session["user_id"]),
    ).fetchone()
    if attachment:
        file_path = os.path.join(UPLOAD_DIR, str(session["user_id"]), attachment["stored_name"])
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
            except OSError:
                flash("附件目前正在使用中，請關閉預覽後再試。", "danger")
                return redirect(url_for("edit_note", note_id=attachment["note_id"]))
        db.execute(
            "DELETE FROM attachments WHERE id = ? AND user_id = ?",
            (attachment_id, session["user_id"]),
        )
        db.commit()
        flash("附件已移除。", "success")
        return redirect(url_for("edit_note", note_id=attachment["note_id"]))
    abort(404)


@app.route("/api/notes/<int:note_id>", methods=["PATCH"])
@login_required
def autosave_note(note_id: int):
    db = get_db()
    payload = request.get_json(silent=True) or {}
    note = db.execute(
        "SELECT id, revision, title, content FROM notes "
        "WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (note_id, session["user_id"]),
    ).fetchone()
    if not note:
        return jsonify(error="找不到筆記。"), 404
    if payload.get("revision") != note["revision"]:
        return jsonify(error="筆記已在其他位置更新，請重新載入以避免覆寫。"), 409

    title = str(payload.get("title", ""))[:200]
    content = str(payload.get("content", ""))[:50000]
    raw_tags = str(payload.get("tags", ""))[:1000]
    previous_tags = note_tags(db, note_id)
    now = utc_now()
    update = db.execute(
        "UPDATE notes SET title = ?, content = ?, updated_at = ?, revision = revision + 1 "
        "WHERE id = ? AND user_id = ? AND revision = ?",
        (title, content, now, note_id, session["user_id"], note["revision"]),
    )
    if update.rowcount != 1:
        db.rollback()
        return jsonify(error="筆記已在其他位置更新，請重新載入以避免覆寫。"), 409
    save_note_tags(db, session["user_id"], note_id, raw_tags)
    saved_tags = note_tags(db, note_id)
    if (
        note["title"] != title
        or note["content"] != content
        or previous_tags != saved_tags
    ):
        create_note_revision(db, note_id, session["user_id"], title, content, saved_tags, now)
    db.commit()
    return jsonify(status="saved", revision=note["revision"] + 1, updated_at=now)


@app.route("/notes/<int:note_id>/pin", methods=["POST"])
@login_required
def toggle_pin(note_id: int):
    db = get_db()
    db.execute(
        "UPDATE notes SET is_pinned = 1 - is_pinned, updated_at = ? "
        "WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (utc_now(), note_id, session["user_id"]),
    )
    db.commit()
    return redirect(url_for("notes"))


@app.route("/notes/<int:note_id>/archive", methods=["POST"])
@login_required
def toggle_archive(note_id: int):
    db = get_db()
    note = db.execute(
        "SELECT archived_at FROM notes WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (note_id, session["user_id"]),
    ).fetchone()
    if note:
        archived_at = None if note["archived_at"] else utc_now()
        db.execute(
            "UPDATE notes SET archived_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (archived_at, utc_now(), note_id, session["user_id"]),
        )
        db.commit()
        return redirect(url_for("notes", view="archive" if archived_at else "active"))
    return redirect(url_for("notes"))


@app.route("/notes/<int:note_id>/trash", methods=["POST"])
@login_required
def trash_note(note_id: int):
    db = get_db()
    db.execute(
        "UPDATE notes SET deleted_at = ?, updated_at = ? WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (utc_now(), utc_now(), note_id, session["user_id"]),
    )
    db.commit()
    flash("筆記已移至垃圾桶，可隨時還原。", "success")
    return redirect(url_for("notes", view="trash"))


@app.route("/notes/<int:note_id>/restore", methods=["POST"])
@login_required
def restore_note(note_id: int):
    db = get_db()
    note = db.execute(
        "SELECT archived_at FROM notes WHERE id = ? AND user_id = ? AND deleted_at IS NOT NULL",
        (note_id, session["user_id"]),
    ).fetchone()
    if not note:
        return redirect(url_for("notes", view="trash"))
    db.execute(
        "UPDATE notes SET deleted_at = NULL, updated_at = ? WHERE id = ? AND user_id = ?",
        (utc_now(), note_id, session["user_id"]),
    )
    db.commit()
    destination = "archive" if note["archived_at"] else "active"
    flash("筆記已還原。", "success")
    return redirect(url_for("notes", view=destination))


@app.route("/notes/<int:note_id>/purge", methods=["POST"])
@login_required
def purge_note(note_id: int):
    db = get_db()
    attachments = db.execute(
        "SELECT stored_name FROM attachments WHERE note_id = ? AND user_id = ?",
        (note_id, session["user_id"]),
    ).fetchall()
    db.execute(
        "DELETE FROM notes WHERE id = ? AND user_id = ? AND deleted_at IS NOT NULL",
        (note_id, session["user_id"]),
    )
    db.commit()
    user_directory = os.path.join(UPLOAD_DIR, str(session["user_id"]))
    for attachment in attachments:
        file_path = os.path.join(user_directory, attachment["stored_name"])
        try:
            os.remove(file_path)
        except FileNotFoundError:
            pass
    return redirect(url_for("notes", view="trash"))


if __name__ == "__main__":
    app.run(debug=not IS_PRODUCTION, host="127.0.0.1", port=5000)
