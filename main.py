#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FloraMind 题库服务器 - 增强版
含：主入口展示页、题库管理、反馈管理、公告管理（网页后台）

依赖:
    pip install flask flask-cors

启动:
    python main.py

环境变量:
    FM_ADMIN_TOKEN   管理员令牌（默认 动植智记）
    FM_SECRET_KEY    session 加密密钥（生产必须改）
    FM_HTTPS         设为 1 时启用 Secure Cookie（部署 HTTPS 后建议开启）

模板文件位于 templates/ 目录，样式位于 static/style.css 与 static/home.css。
"""
import os
import json
import sqlite3
import time
import uuid
import zipfile
from functools import wraps

from flask import (
    Flask, request, jsonify, send_file, abort, g,
    session, redirect, url_for, flash, get_flashed_messages,
    render_template,
)
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "server.db")

ADMIN_TOKEN = os.environ.get("FM_ADMIN_TOKEN", "动植智记")
SECRET_KEY = os.environ.get("FM_SECRET_KEY", "floramind-secret-change-me")

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
app.secret_key = SECRET_KEY

# 反向代理（Nginx）修正：让 Flask 正确识别 https / host / 客户端 IP
app.wsgi_app = ProxyFix(
    app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1
)

# 部署 HTTPS 后，通过环境变量 FM_HTTPS=1 启用安全 Cookie
if os.environ.get("FM_HTTPS", "0") == "1":
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

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
            CREATE TABLE IF NOT EXISTS announcements (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                created_time INTEGER,
                updated_time INTEGER
            );
            """
        )
        conn.commit()


init_db()


# ---------------- 装饰器 ----------------
def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
            return jsonify({"success": False, "message": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_session_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


# ---------------- 工具函数 ----------------
def count_questions_in_zip(zip_path):
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


def fmt_time(ts):
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def fmt_size(b):
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / 1024 / 1024:.1f} MB"


@app.context_processor
def inject_helpers():
    return {"fmt_time": fmt_time, "fmt_size": fmt_size}


def do_upload(file_storage, name, description, uploader):
    if not file_storage or not file_storage.filename:
        return None, 0, "文件不能为空"
    if not file_storage.filename.lower().endswith(".zip"):
        return None, 0, "只支持 ZIP 文件"

    name = (name or "").strip() or os.path.splitext(file_storage.filename)[0]
    description = (description or "").strip()
    uploader = (uploader or "").strip()

    pkg_id = uuid.uuid4().hex
    save_path = os.path.join(UPLOAD_DIR, f"{pkg_id}.zip")
    file_storage.save(save_path)

    q_count = count_questions_in_zip(save_path)
    if q_count == 0:
        try:
            os.remove(save_path)
        except Exception:
            pass
        return None, 0, "ZIP 内未找到有效的 questions.json"

    file_size = os.path.getsize(save_path)
    db = get_db()
    db.execute(
        "INSERT INTO packages(id, name, description, question_count, file_size, "
        "uploader, upload_time, download_count) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (pkg_id, name, description, q_count, file_size, uploader, int(time.time())),
    )
    db.commit()
    return pkg_id, q_count, None


# ============================================================
#                    主入口展示页
# ============================================================
@app.route("/", methods=["GET"])
def home():
    keyword = (request.args.get("q") or "").strip()
    db = get_db()
    if keyword:
        packages = db.execute(
            "SELECT * FROM packages WHERE name LIKE ? OR description LIKE ? "
            "ORDER BY upload_time DESC",
            (f"%{keyword}%", f"%{keyword}%"),
        ).fetchall()
    else:
        packages = db.execute(
            "SELECT * FROM packages ORDER BY upload_time DESC"
        ).fetchall()

    announcement = db.execute(
        "SELECT * FROM announcements WHERE enabled = 1 "
        "ORDER BY updated_time DESC LIMIT 1"
    ).fetchone()

    return render_template(
        "index.html",
        title="首页",
        packages=packages,
        keyword=keyword,
        announcement=announcement,
    )


# ============================================================
#                     App 使用的 API
# ============================================================
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
    pkg_id, q_count, err = do_upload(
        request.files.get("file"),
        request.form.get("name"),
        request.form.get("description"),
        request.form.get("uploader"),
    )
    if err:
        return jsonify({"success": False, "message": err}), 400
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
def api_delete_package(pkg_id):
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
def api_list_feedback():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM feedback ORDER BY created_time DESC"
    ).fetchall()
    return jsonify({"feedback": [dict(r) for r in rows]})


@app.route("/api/announcement", methods=["GET"])
def get_announcement():
    db = get_db()
    row = db.execute(
        "SELECT * FROM announcements WHERE enabled = 1 "
        "ORDER BY updated_time DESC LIMIT 1"
    ).fetchone()
    if not row:
        return jsonify({"announcement": None})
    return jsonify({"announcement": {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "updated_time": row["updated_time"],
    }})


# ============================================================
#                    管理后台路由
# ============================================================
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        token = request.form.get("token", "")
        if token == ADMIN_TOKEN:
            session["admin"] = True
            flash("登录成功", "success")
            return redirect(url_for("admin_packages"))
        return render_template("login.html", error_msg="令牌错误，请重试")
    if session.get("admin"):
        return redirect(url_for("admin_packages"))
    return render_template("login.html", error_msg=None)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    flash("已退出登录", "success")
    return redirect(url_for("admin_login"))


@app.route("/admin/")
def admin_index():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    return redirect(url_for("admin_packages"))


# ---------- 题库 ----------
@app.route("/admin/packages")
@admin_session_required
def admin_packages():
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
    return render_template("packages.html", title="题库管理",
                           active="packages", rows=rows, keyword=keyword)


@app.route("/admin/packages/upload", methods=["GET", "POST"])
@admin_session_required
def admin_package_upload_form():
    if request.method == "POST":
        _, q_count, err = do_upload(
            request.files.get("file"),
            request.form.get("name"),
            request.form.get("description"),
            request.form.get("uploader"),
        )
        if err:
            flash(err, "error")
            return redirect(url_for("admin_package_upload_form"))
        flash(f"上传成功，共识别到 {q_count} 道题目", "success")
        return redirect(url_for("admin_packages"))
    return render_template("upload.html", title="上传题库", active="packages")


@app.route("/admin/packages/<pkg_id>/edit", methods=["GET", "POST"])
@admin_session_required
def admin_package_edit(pkg_id):
    db = get_db()
    row = db.execute("SELECT * FROM packages WHERE id = ?", (pkg_id,)).fetchone()
    if not row:
        flash("题库不存在", "error")
        return redirect(url_for("admin_packages"))

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        description = (request.form.get("description") or "").strip()
        uploader = (request.form.get("uploader") or "").strip()
        if not name:
            flash("名称不能为空", "error")
        else:
            db.execute(
                "UPDATE packages SET name = ?, description = ?, uploader = ? WHERE id = ?",
                (name, description, uploader, pkg_id),
            )
            db.commit()
            flash("已保存", "success")
            return redirect(url_for("admin_packages"))
    return render_template("package_edit.html", title="编辑题库",
                           active="packages", row=row)


@app.route("/admin/packages/<pkg_id>/delete", methods=["POST"])
@admin_session_required
def admin_package_delete(pkg_id):
    db = get_db()
    row = db.execute("SELECT * FROM packages WHERE id = ?", (pkg_id,)).fetchone()
    if row:
        db.execute("DELETE FROM packages WHERE id = ?", (pkg_id,))
        db.commit()
        path = os.path.join(UPLOAD_DIR, f"{pkg_id}.zip")
        if os.path.exists(path):
            os.remove(path)
        flash(f"已删除《{row['name']}》", "success")
    else:
        flash("题库不存在", "error")
    return redirect(url_for("admin_packages"))


@app.route("/admin/packages/<pkg_id>/download")
@admin_session_required
def admin_package_download(pkg_id):
    db = get_db()
    row = db.execute("SELECT * FROM packages WHERE id = ?", (pkg_id,)).fetchone()
    if not row:
        abort(404)
    path = os.path.join(UPLOAD_DIR, f"{pkg_id}.zip")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=f"{row['name']}.zip")


# ---------- 公告 ----------
@app.route("/admin/announcements")
@admin_session_required
def admin_announcements():
    rows = get_db().execute(
        "SELECT * FROM announcements ORDER BY updated_time DESC"
    ).fetchall()
    return render_template("announcements.html", title="公告管理",
                           active="announcements", rows=rows)


@app.route("/admin/announcements/new", methods=["GET", "POST"])
@admin_session_required
def admin_announcement_new():
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        content = (request.form.get("content") or "").strip()
        enabled = 1 if request.form.get("enabled") else 0
        if not title or not content:
            flash("标题和内容不能为空", "error")
        else:
            now = int(time.time())
            ann_id = uuid.uuid4().hex
            db = get_db()
            db.execute(
                "INSERT INTO announcements(id, title, content, enabled, "
                "created_time, updated_time) VALUES (?, ?, ?, ?, ?, ?)",
                (ann_id, title, content, enabled, now, now),
            )
            db.commit()
            flash("公告已创建", "success")
            return redirect(url_for("admin_announcements"))
    return render_template("announcement_form.html", title="新建公告",
                           active="announcements", row=None, is_new=True,
                           action_url=url_for("admin_announcement_new"))


@app.route("/admin/announcements/<ann_id>/edit", methods=["GET", "POST"])
@admin_session_required
def admin_announcement_edit(ann_id):
    db = get_db()
    row = db.execute("SELECT * FROM announcements WHERE id = ?", (ann_id,)).fetchone()
    if not row:
        flash("公告不存在", "error")
        return redirect(url_for("admin_announcements"))

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        content = (request.form.get("content") or "").strip()
        enabled = 1 if request.form.get("enabled") else 0
        if not title or not content:
            flash("标题和内容不能为空", "error")
        else:
            now = int(time.time())
            db.execute(
                "UPDATE announcements SET title = ?, content = ?, enabled = ?, "
                "updated_time = ? WHERE id = ?",
                (title, content, enabled, now, ann_id),
            )
            db.commit()
            flash("已保存", "success")
            return redirect(url_for("admin_announcements"))
    return render_template("announcement_form.html", title="编辑公告",
                           active="announcements", row=row, is_new=False,
                           action_url=url_for("admin_announcement_edit", ann_id=ann_id))


@app.route("/admin/announcements/<ann_id>/toggle", methods=["POST"])
@admin_session_required
def admin_announcement_toggle(ann_id):
    db = get_db()
    row = db.execute("SELECT * FROM announcements WHERE id = ?", (ann_id,)).fetchone()
    if row:
        new_val = 0 if row["enabled"] == 1 else 1
        db.execute(
            "UPDATE announcements SET enabled = ? WHERE id = ?",
            (new_val, ann_id),
        )
        db.commit()
        flash("已" + ("启用" if new_val == 1 else "停用"), "success")
    return redirect(url_for("admin_announcements"))


@app.route("/admin/announcements/<ann_id>/delete", methods=["POST"])
@admin_session_required
def admin_announcement_delete(ann_id):
    db = get_db()
    db.execute("DELETE FROM announcements WHERE id = ?", (ann_id,))
    db.commit()
    flash("已删除公告", "success")
    return redirect(url_for("admin_announcements"))


# ---------- 反馈 ----------
@app.route("/admin/feedback")
@admin_session_required
def admin_feedback():
    rows = get_db().execute(
        "SELECT * FROM feedback ORDER BY created_time DESC"
    ).fetchall()
    return render_template("feedback.html", title="反馈管理",
                           active="feedback", rows=rows)


@app.route("/admin/feedback/<fb_id>/delete", methods=["POST"])
@admin_session_required
def admin_feedback_delete(fb_id):
    db = get_db()
    db.execute("DELETE FROM feedback WHERE id = ?", (fb_id,))
    db.commit()
    flash("已删除反馈", "success")
    return redirect(url_for("admin_feedback"))


@app.route("/admin/feedback/clear", methods=["POST"])
@admin_session_required
def admin_feedback_clear():
    db = get_db()
    n = db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    db.execute("DELETE FROM feedback")
    db.commit()
    flash(f"已清空 {n} 条反馈", "success")
    return redirect(url_for("admin_feedback"))


# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)