import re
import sqlite3
from io import BytesIO

import pytest

import app as app_module


@pytest.fixture

def client(tmp_path, monkeypatch):
    database_path = tmp_path / "test-notes.db"
    monkeypatch.setattr(app_module, "DB_PATH", str(database_path))
    previous_config = app_module.app.config.copy()
    app_module.app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret-key",
        WTF_CSRF_ENABLED=True,
        RATELIMIT_ENABLED=False,
    )
    test_client = app_module.app.test_client()
    test_client.environ_base["REMOTE_ADDR"] = f"127.0.0.{abs(hash(str(tmp_path))) % 250 + 1}"
    yield test_client
    app_module.app.config.update(previous_config)


def token_from(response):
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    if not match:
        match = re.search(rb'<meta name="csrf-token" content="([^"]+)"', response.data)
    assert match, "CSRF token missing from page"
    return match.group(1).decode()


def register_and_login(client, username="tester", email=None):
    email = email or f"{username}@example.com"
    response = client.get("/register")
    token = token_from(response)
    response = client.post(
        "/register",
        data={"csrf_token": token, "username": username, "email": email, "password": "password123"},
    )
    assert response.status_code == 302
    response = client.get("/login")
    token = token_from(response)
    response = client.post(
        "/login",
        data={"csrf_token": token, "email": email, "password": "password123"},
    )
    assert response.status_code == 302


def create_note(client, title="測試筆記"):
    response = client.get("/notes")
    token = token_from(response)
    response = client.post("/notes/new", data={"csrf_token": token})
    assert response.status_code == 302
    edit_url = response.headers["Location"]
    edit_page = client.get(edit_url)
    return edit_url, token_from(edit_page)


def test_existing_sqlite_database_is_migrated_without_losing_notes(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )
    connection.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
        ("legacy", app_module.generate_password_hash("password123")),
    )
    connection.execute(
        "INSERT INTO notes (user_id, title, content) VALUES (1, '保留筆記', '既有內容')"
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(app_module, "DB_PATH", str(database_path))

    test_client = app_module.app.test_client()
    response = test_client.get("/login")
    assert response.status_code == 200
    token = token_from(response)
    login_response = test_client.post(
        "/login",
        data={"csrf_token": token, "email": "legacy", "password": "password123"},
    )
    assert login_response.status_code == 302

    connection = sqlite3.connect(database_path)
    user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
    note_columns = {row[1] for row in connection.execute("PRAGMA table_info(notes)")}
    saved_note = connection.execute("SELECT title, content FROM notes").fetchone()
    connection.close()
    assert {"email", "theme"} <= user_columns
    assert {"is_pinned", "archived_at", "deleted_at", "revision"} <= note_columns
    assert saved_note == ("保留筆記", "既有內容")
    connection = sqlite3.connect(database_path)
    assert connection.execute("SELECT COUNT(*) FROM note_revisions").fetchone()[0] == 1
    connection.close()


def test_autosave_tags_and_stale_revision_conflict(client):
    register_and_login(client)
    _, token = create_note(client)

    response = client.patch(
        "/api/notes/1",
        json={"title": "工作會議", "content": "討論版本", "tags": "工作, 會議", "revision": 1},
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 200
    assert response.json["revision"] == 2

    stale = client.patch(
        "/api/notes/1",
        json={"title": "舊版本", "content": "不可覆寫", "tags": "", "revision": 1},
        headers={"X-CSRFToken": token},
    )
    assert stale.status_code == 409

    listing = client.get("/notes?tag=%E5%B7%A5%E4%BD%9C&q=%E6%9C%83%E8%AD%B0")
    assert listing.status_code == 200
    assert "工作會議".encode() in listing.data
    assert "討論版本".encode() in listing.data


def test_pin_archive_trash_restore_and_permanent_delete(client):
    register_and_login(client)
    _, token = create_note(client)
    saved = client.patch(
        "/api/notes/1",
        json={"title": "測試筆記", "content": "草稿內容", "tags": "", "revision": 1},
        headers={"X-CSRFToken": token},
    )
    assert saved.status_code == 200

    assert client.post("/notes/1/pin", data={"csrf_token": token}).status_code == 302
    assert "測試筆記".encode() in client.get("/notes").data

    assert client.post("/notes/1/archive", data={"csrf_token": token}).status_code == 302
    assert "測試筆記".encode() in client.get("/notes?view=archive").data
    assert "測試筆記".encode() not in client.get("/notes").data

    client.post("/notes/1/trash", data={"csrf_token": token})
    assert "測試筆記".encode() in client.get("/notes?view=trash").data
    client.post("/notes/1/restore", data={"csrf_token": token})
    assert "測試筆記".encode() in client.get("/notes?view=archive").data

    client.post("/notes/1/trash", data={"csrf_token": token})
    client.post("/notes/1/purge", data={"csrf_token": token})
    assert "測試筆記".encode() not in client.get("/notes?view=trash").data
    connection = sqlite3.connect(app_module.DB_PATH)
    revision_count = connection.execute(
        "SELECT COUNT(*) FROM note_revisions WHERE note_id = 1"
    ).fetchone()[0]
    connection.close()
    assert revision_count == 0


def test_note_api_rejects_missing_csrf_and_other_users(client):
    register_and_login(client, "owner")
    _, token = create_note(client)
    missing_token = client.patch("/api/notes/1", json={"title": "越權", "revision": 1})
    assert missing_token.status_code == 400

    other_client = app_module.app.test_client()
    register_and_login(other_client, "another-user")
    other_token = token_from(other_client.get("/notes"))
    response = other_client.patch(
        "/api/notes/1",
        json={"title": "越權", "revision": 1},
        headers={"X-CSRFToken": other_token},
    )
    assert response.status_code == 404
    assert token


def test_email_is_normalized_and_duplicate_email_is_rejected(client):
    register_and_login(client, "email-user", "Person@Example.com")

    listing = client.get("/notes")
    logout_response = client.post(
        "/logout", data={"csrf_token": token_from(listing)}
    )
    assert logout_response.status_code == 302

    login_page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": token_from(login_page),
            "email": "person@example.com",
            "password": "password123",
        },
    )
    assert response.status_code == 302

    register_page = client.get("/register")
    duplicate = client.post(
        "/register",
        data={
            "csrf_token": token_from(register_page),
            "username": "second-user",
            "email": "PERSON@example.com",
            "password": "password123",
        },
    )
    assert duplicate.status_code == 200
    assert "電子郵件已被使用".encode() in duplicate.data


def test_attachment_upload_preview_download_and_owner_isolation(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path / "uploads"))
    register_and_login(client, "file-owner")
    _, token = create_note(client)

    upload = client.post(
        "/notes/1/attachments",
        data={
            "csrf_token": token,
            "file": (BytesIO(b"fake png bytes"), "vacation.png"),
        },
        content_type="multipart/form-data",
    )
    assert upload.status_code == 302
    editor = client.get("/notes/1/edit")
    assert "vacation.png".encode() in editor.data

    connection = sqlite3.connect(app_module.DB_PATH)
    connection.row_factory = sqlite3.Row
    attachment = connection.execute("SELECT * FROM attachments").fetchone()
    connection.close()
    assert attachment is not None

    preview = client.get(f"/attachments/{attachment['id']}?preview=1")
    assert preview.status_code == 200
    assert preview.mimetype == "image/png"
    assert preview.headers["X-Content-Type-Options"] == "nosniff"
    preview.close()

    other_client = app_module.app.test_client()
    register_and_login(other_client, "file-other")
    assert other_client.get(f"/attachments/{attachment['id']}").status_code == 404

    removed = client.post(
        f"/attachments/{attachment['id']}/delete",
        data={"csrf_token": token},
    )
    assert removed.status_code == 302
    assert client.get(f"/attachments/{attachment['id']}").status_code == 404


def test_attachment_rejects_unsupported_file_type(client):
    register_and_login(client, "file-type-user")
    _, token = create_note(client)
    response = client.post(
        "/notes/1/attachments",
        data={
            "csrf_token": token,
            "file": (BytesIO(b"not allowed"), "script.exe"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    editor = client.get("/notes/1/edit")
    assert "不支援此檔案格式".encode() in editor.data


def test_audio_attachment_is_playable_inline(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path / "uploads"))
    register_and_login(client, "audio-user")
    _, token = create_note(client)

    upload = client.post(
        "/notes/1/attachments",
        data={
            "csrf_token": token,
            "file": (BytesIO(b"sample recorded audio"), "voice-note.webm"),
        },
        content_type="multipart/form-data",
    )
    assert upload.status_code == 302

    connection = sqlite3.connect(app_module.DB_PATH)
    connection.row_factory = sqlite3.Row
    attachment = connection.execute("SELECT * FROM attachments").fetchone()
    connection.close()
    assert attachment["mime_type"] == "audio/webm"

    playback = client.get(f"/attachments/{attachment['id']}?preview=1")
    assert playback.status_code == 200
    assert playback.mimetype == "audio/webm"
    assert playback.headers["Content-Disposition"].startswith("inline;")
    assert playback.data == b"sample recorded audio"
    playback.close()

    editor = client.get("/notes/1/edit")
    assert "錄製語音".encode() in editor.data
    assert b"audio-player" in editor.data


def test_blank_draft_persists_after_logout_and_login(client):
    register_and_login(client, "draft-user")
    edit_url, _ = create_note(client)
    note_id = int(re.search(r"/notes/(\d+)/edit", edit_url).group(1))

    listing = client.get("/notes")
    assert "未命名筆記".encode() in listing.data
    client.post("/logout", data={"csrf_token": token_from(listing)})

    login_page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": token_from(login_page),
            "email": "draft-user@example.com",
            "password": "password123",
        },
    )
    assert response.status_code == 302
    assert "未命名筆記".encode() in client.get("/notes").data

    connection = sqlite3.connect(app_module.DB_PATH)
    saved = connection.execute("SELECT title, content FROM notes WHERE id = ?", (note_id,)).fetchone()
    connection.close()
    assert saved == ("", "")


def test_revision_history_restore_adds_version_without_overwriting_history(client):
    register_and_login(client, "history-user")
    edit_url, token = create_note(client)
    note_id = int(re.search(r"/notes/(\d+)/edit", edit_url).group(1))

    first_save = client.patch(
        f"/api/notes/{note_id}",
        json={"title": "第一版", "content": "原始內容", "tags": "原始", "revision": 1},
        headers={"X-CSRFToken": token},
    )
    assert first_save.status_code == 200

    identical_save = client.patch(
        f"/api/notes/{note_id}",
        json={"title": "第一版", "content": "原始內容", "tags": "原始", "revision": 2},
        headers={"X-CSRFToken": token},
    )
    assert identical_save.status_code == 200

    second_save = client.patch(
        f"/api/notes/{note_id}",
        json={"title": "第二版", "content": "更新內容", "tags": "新版", "revision": 3},
        headers={"X-CSRFToken": token},
    )
    assert second_save.status_code == 200

    connection = sqlite3.connect(app_module.DB_PATH)
    connection.row_factory = sqlite3.Row
    versions = connection.execute(
        "SELECT * FROM note_revisions WHERE note_id = ? ORDER BY version_number",
        (note_id,),
    ).fetchall()
    connection.close()
    assert [row["version_number"] for row in versions] == [1, 2, 3]
    assert versions[1]["content"] == "原始內容"

    history = client.get(f"/notes/{note_id}/history")
    assert history.status_code == 200
    assert "原始內容".encode() in history.data
    assert "更新內容".encode() in history.data

    restored = client.post(
        f"/notes/{note_id}/history/{versions[1]['id']}/restore",
        data={"csrf_token": token},
    )
    assert restored.status_code == 302

    connection = sqlite3.connect(app_module.DB_PATH)
    current = connection.execute(
        "SELECT title, content FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    history_rows = connection.execute(
        "SELECT version_number, content FROM note_revisions "
        "WHERE note_id = ? ORDER BY version_number",
        (note_id,),
    ).fetchall()
    connection.close()
    assert current == ("第一版", "原始內容")
    assert [row[0] for row in history_rows] == [1, 2, 3, 4]
    assert history_rows[2][1] == "更新內容"
    assert history_rows[3][1] == "原始內容"


def test_theme_is_saved_per_user_and_restored_after_login(client):
    register_and_login(client, "theme-user")
    page = client.get("/notes")
    assert b'data-theme="white"' in page.data
    for label in ("紅色", "橙色", "黃色", "綠色", "藍色", "靛色", "紫色", "黑色", "白色"):
        assert label.encode() in page.data

    changed = client.post(
        "/settings/theme",
        data={"csrf_token": token_from(page), "theme": "violet", "return_to": "/notes"},
    )
    assert changed.status_code == 302
    themed_page = client.get("/notes")
    assert b'data-theme="violet"' in themed_page.data

    client.post("/logout", data={"csrf_token": token_from(themed_page)})
    login_page = client.get("/login")
    client.post(
        "/login",
        data={
            "csrf_token": token_from(login_page),
            "email": "theme-user@example.com",
            "password": "password123",
        },
    )
    assert b'data-theme="violet"' in client.get("/notes").data


def test_invalid_theme_is_rejected_without_open_redirect(client):
    register_and_login(client, "theme-validation")
    page = client.get("/notes")
    response = client.post(
        "/settings/theme",
        data={
            "csrf_token": token_from(page),
            "theme": "not-a-theme",
            "return_to": "//example.com",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/notes")
    assert b'data-theme="white"' in client.get("/notes").data
