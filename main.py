#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FloraMind 题库服务器
依赖: pip install flask flask-cors
启动: python server.py
"""
import os
import json
import sqlite3
import time
import uuid
import zipfile
from functools import wraps

from flask import Flask, request, jsonify, send_file, abort, g
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "server.db")

# 管理员令牌（用于删除题库/查看反馈），可通过环境变量覆盖
ADMIN_TOKEN = os.environ.get("FM_ADMIN_TOKEN", "change-me-admin-token")

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB
CORS(app)


# ---------------- 数据库 ----------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS packages (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                question_count INTEGER DEFAULT 0,
                file_size INTEGER DEFAULT 0,
                uploader TEXT,
                upload_time INTEGER,
                download_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id TEXT PRIMARY KEY,
                user_name TEXT,
                content TEXT NOT NULL,
                contact TEXT,
                app_version TEXT,
                created_time INTEGER
            );
            """
        )
        conn.commit()


init_db()


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
            return jsonify({"success": False, "message": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# ---------------- 工具函数 ----------------
def count_questions_in_zip(zip_path):
    """统计 ZIP 内所有 questions.json 的题目总数"""
    total = 0
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.lower().endswith("questions.json"):
                    try:
                        data = json.loads(zf.read(name).decode("utf-8"))
                        if isinstance(data, list):
                            total += len(data)
                    except Exception:
                        pass
    except Exception:
        pass
    return total


def package_row_to_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"] or "",
        "question_count": row["question_count"],
        "file_size": row["file_size"],
        "uploader": row["uploader"] or "",
        "upload_time": row["upload_time"],
        "download_count": row["download_count"],
    }


# ---------------- 接口 ----------------
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": int(time.time())})


@app.route("/api/packages", methods=["GET"])
def list_packages():
    keyword = (request.args.get("q") or "").strip()
    db = get_db()
    if keyword:
        rows = db.execute(
            "SELECT * FROM packages WHERE name LIKE ? OR description LIKE ? "
            "ORDER BY upload_time DESC",
            (f"%{keyword}%", f"%{keyword}%"),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM packages ORDER BY upload_time DESC"
        ).fetchall()
    return jsonify({"packages": [package_row_to_dict(r) for r in rows]})


@app.route("/api/packages/upload", methods=["POST"])
def upload_package():
    if "file" not in request.files:
        return jsonify({"success": False, "message": "缺少文件"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"success": False, "message": "文件名为空"}), 400
    if not f.filename.lower().endswith(".zip"):
        return jsonify({"success": False, "message": "只支持 ZIP 文件"}), 400

    name = (request.form.get("name") or "").strip() or os.path.splitext(f.filename)[0]
    description = (request.form.get("description") or "").strip()
    uploader = (request.form.get("uploader") or "").strip()

    pkg_id = uuid.uuid4().hex
    save_path = os.path.join(UPLOAD_DIR, f"{pkg_id}.zip")
    f.save(save_path)

    q_count = count_questions_in_zip(save_path)
    if q_count == 0:
        os.remove(save_path)
        return jsonify({"success": False, "message": "ZIP 内未找到有效的 questions.json"}), 400

    file_size = os.path.getsize(save_path)
    db = get_db()
    db.execute(
        "INSERT INTO packages(id, name, description, question_count, file_size, "
        "uploader, upload_time, download_count) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (pkg_id, name, description, q_count, file_size, uploader, int(time.time())),
    )
    db.commit()
    return jsonify({"success": True, "id": pkg_id, "questionCount": q_count})


@app.route("/api/packages/<pkg_id>/download", methods=["GET"])
def download_package(pkg_id):
    db = get_db()
    row = db.execute("SELECT * FROM packages WHERE id = ?", (pkg_id,)).fetchone()
    if not row:
        abort(404)
    path = os.path.join(UPLOAD_DIR, f"{pkg_id}.zip")
    if not os.path.exists(path):
        abort(404)
    db.execute(
        "UPDATE packages SET download_count = download_count + 1 WHERE id = ?",
        (pkg_id,),
    )
    db.commit()
    return send_file(path, as_attachment=True, download_name=f"{row['name']}.zip")


@app.route("/api/packages/<pkg_id>", methods=["DELETE"])
@require_admin
def delete_package(pkg_id):
    db = get_db()
    row = db.execute("SELECT * FROM packages WHERE id = ?", (pkg_id,)).fetchone()
    if not row:
        abort(404)
    db.execute("DELETE FROM packages WHERE id = ?", (pkg_id,))
    db.commit()
    path = os.path.join(UPLOAD_DIR, f"{pkg_id}.zip")
    if os.path.exists(path):
        os.remove(path)
    return jsonify({"success": True})


@app.route("/api/feedback", methods=["POST"])
def submit_feedback():
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"success": False, "message": "反馈内容不能为空"}), 400
    fb_id = uuid.uuid4().hex
    db = get_db()
    db.execute(
        "INSERT INTO feedback(id, user_name, content, contact, app_version, "
        "created_time) VALUES (?, ?, ?, ?, ?, ?)",
        (
            fb_id,
            data.get("userName"),
            content,
            data.get("contact"),
            data.get("appVersion"),
            int(time.time()),
        ),
    )
    db.commit()
    return jsonify({"success": True, "id": fb_id})


@app.route("/api/feedback", methods=["GET"])
@require_admin
def list_feedback():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM feedback ORDER BY created_time DESC"
    ).fetchall()
    return jsonify({"feedback": [dict(r) for r in rows]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)