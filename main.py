#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FloraMind 题库服务器 - 增强版（带网页管理后台）

依赖:
    pip install flask flask-cors

启动:
    python main.py

访问:
    App 接口:   http://host/api/...
    管理后台:   http://host/admin/login

环境变量:
    FM_ADMIN_TOKEN   管理员登录令牌（默认 change-me-admin-token）
    FM_SECRET_KEY    Flask session 加密密钥（默认 floramind-secret-change-me，生产必须改）
"""
import os
import json
import sqlite3
import time
import uuid
import zipfile
import html as h
from functools import wraps

from flask import (
    Flask, request, jsonify, send_file, abort, g,
    session, redirect, url_for, flash, get_flashed_messages,
)
from flask_cors import CORS

# ---------------- 路径与配置 ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "server.db")

ADMIN_TOKEN = os.environ.get("FM_ADMIN_TOKEN", "change-me-admin-token")
SECRET_KEY = os.environ.get("FM_SECRET_KEY", "floramind-secret-change-me")

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB
app.secret_key = SECRET_KEY
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


# ---------------- 装饰器 ----------------
def require_admin(f):
    """API 用：检查 X-Admin-Token 请求头"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
            return jsonify({"success": False, "message": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_session_required(f):
    """管理后台页面用：检查 session"""
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


def do_upload(file_storage, name, description, uploader):
    """
    统一的上传逻辑。返回 (pkg_id, q_count, error)。
    成功时 error=None。
    """
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
#                          管理后台样式
# ============================================================
CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
       margin: 0; background: #f4f6f8; color: #2c3e50; font-size: 14px; line-height: 1.6; }
a { color: #3498db; text-decoration: none; }
a:hover { text-decoration: underline; }

.topbar { background: #2c3e50; color: #fff; padding: 0 24px; display: flex;
          justify-content: space-between; align-items: center; height: 56px; }
.topbar .nav { display: flex; align-items: center; gap: 4px; }
.topbar .brand { font-weight: 600; margin-right: 24px; font-size: 15px; }
.topbar a.nav-link { color: rgba(255,255,255,0.85); padding: 6px 12px; border-radius: 4px; }
.topbar a.nav-link:hover { background: rgba(255,255,255,0.1); text-decoration: none; }
.topbar a.nav-link.active { background: rgba(255,255,255,0.18); color: #fff; }

.container { max-width: 1200px; margin: 24px auto; padding: 0 16px; }
.card { background: #fff; border-radius: 8px; padding: 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.06); margin-bottom: 16px; }
h1, h2 { margin-top: 0; font-weight: 600; }
h2 { font-size: 18px; }

table { width: 100%; border-collapse: collapse; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #eef1f5;
         vertical-align: top; font-size: 13px; }
th { background: #fafbfc; font-weight: 600; color: #6a7581; font-size: 12px;
     text-transform: uppercase; letter-spacing: 0.3px; }
tr:hover td { background: #fafbfc; }
td.desc { color: #6a7581; max-width: 300px; }
td.content-cell { white-space: pre-wrap; max-width: 400px; word-break: break-word; }
td.actions { white-space: nowrap; }

.btn { display: inline-block; padding: 7px 14px; border-radius: 4px; border: none;
       cursor: pointer; font-size: 13px; text-decoration: none; color: #fff;
       background: #3498db; transition: opacity .15s; font-family: inherit; }
.btn:hover { opacity: .88; text-decoration: none; }
.btn-primary { background: #3498db; }
.btn-danger { background: #e74c3c; }
.btn-secondary { background: #95a5a6; }
.btn-sm { padding: 4px 10px; font-size: 12px; }

input[type=text], input[type=password], input[type=file], textarea {
    width: 100%; padding: 8px 11px; border: 1px solid #d8dde3; border-radius: 4px;
    font-size: 14px; font-family: inherit; transition: border-color .15s; }
input:focus, textarea:focus { outline: none; border-color: #3498db; }
textarea { resize: vertical; min-height: 70px; }
.form-group { margin-bottom: 14px; }
.form-group label { display: block; margin-bottom: 5px; font-size: 13px;
                    color: #4a5568; font-weight: 500; }
.hint { color: #8895a4; font-size: 12px; }

.toolbar { display: flex; justify-content: space-between; align-items: center;
           gap: 12px; flex-wrap: wrap; }
.search-form { display: flex; gap: 8px; flex: 1; max-width: 480px; }
.search-form input { flex: 1; }

.flash { padding: 11px 16px; border-radius: 4px; margin-bottom: 16px; font-size: 13px; }
.flash-success { background: #d4edda; color: #155724; border-left: 4px solid #28a745; }
.flash-error { background: #f8d7da; color: #721c24; border-left: 4px solid #dc3545; }

.empty { text-align: center; color: #8895a4; padding: 50px 0; }
.inline-form { display: inline; }

/* 登录页 */
.login-body { display: flex; align-items: center; justify-content: center;
              min-height: 100vh; background: linear-gradient(135deg, #667eea, #764ba2); }
.login-card { background: #fff; border-radius: 10px; padding: 40px; width: 360px;
              box-shadow: 0 10px 40px rgba(0,0,0,0.2); }
.login-card h1 { font-size: 20px; text-align: center; margin-bottom: 24px; }
"""


# ============================================================
#                       HTML 渲染辅助
# ============================================================
def render_layout(title, body_html, active=""):
    topbar = f"""
    <header class="topbar">
      <div class="nav">
        <span class="brand">🌿 FloraMind</span>
        <a href="{url_for('admin_packages')}" class="nav-link {'active' if active == 'packages' else ''}">题库管理</a>
        <a href="{url_for('admin_feedback')}" class="nav-link {'active' if active == 'feedback' else ''}">反馈管理</a>
      </div>
      <a href="{url_for('admin_logout')}" class="nav-link">退出登录</a>
    </header>
    """
    flashes = ""
    for cat, msg in get_flashed_messages(with_categories=True):
        flashes += f'<div class="flash flash-{cat}">{h.escape(str(msg))}</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h.escape(title)} - FloraMind 管理后台</title>
<style>{CSS}</style>
</head>
<body>
{topbar}
<main class="container">
{flashes}
{body_html}
</main>
</body>
</html>"""


def render_login(error_msg=None):
    err = f'<div class="flash flash-error">{h.escape(error_msg)}</div>' if error_msg else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 - FloraMind 管理后台</title>
<style>{CSS}</style>
</head>
<body class="login-body">
<div class="login-card">
  <h1>🌿 FloraMind 管理后台</h1>
  {err}
  <form method="post">
    <div class="form-group">
      <label>管理员令牌</label>
      <input type="password" name="token" autofocus required>
    </div>
    <button type="submit" class="btn btn-primary" style="width:100%">登 录</button>
  </form>
</div>
</body>
</html>"""


def render_packages_page(rows, keyword=""):
    if not rows:
        body = '<div class="empty">暂无题库。点击右上角"+ 上传题库"添加。</div>'
    else:
        trs = ""
        for r in rows:
            trs += f"""
            <tr>
              <td><strong>{h.escape(r['name'])}</strong></td>
              <td class="desc">{h.escape(r['description'] or '')}</td>
              <td>{r['question_count']}</td>
              <td>{fmt_size(r['file_size'])}</td>
              <td>{h.escape(r['uploader'] or '')}</td>
              <td>{r['download_count']}</td>
              <td>{fmt_time(r['upload_time'])}</td>
              <td class="actions">
                <a class="btn btn-sm btn-secondary" href="{url_for('admin_package_download', pkg_id=r['id'])}">下载</a>
                <a class="btn btn-sm" href="{url_for('admin_package_edit', pkg_id=r['id'])}">编辑</a>
                <form method="post" action="{url_for('admin_package_delete', pkg_id=r['id'])}"
                      class="inline-form"
                      onsubmit="return confirm('确认删除《' + this.dataset.name + '》？此操作不可恢复。');"
                      data-name="{h.escape(r['name'])}">
                  <button type="submit" class="btn btn-sm btn-danger">删除</button>
                </form>
              </td>
            </tr>
            """
        body = f"""
        <table>
          <thead>
            <tr>
              <th>名称</th><th>描述</th><th>题数</th><th>大小</th>
              <th>上传者</th><th>下载</th><th>上传时间</th><th>操作</th>
            </tr>
          </thead>
          <tbody>{trs}</tbody>
        </table>
        """

    content = f"""
    <div class="card">
      <div class="toolbar">
        <form method="get" class="search-form">
          <input type="text" name="q" value="{h.escape(keyword)}" placeholder="搜索题库名称或描述...">
          <button type="submit" class="btn">搜索</button>
          <a href="{url_for('admin_packages')}" class="btn btn-secondary">重置</a>
        </form>
        <a href="{url_for('admin_package_upload_form')}" class="btn btn-primary">+ 上传题库</a>
      </div>
    </div>
    <div class="card">{body}</div>
    """
    return render_layout("题库管理", content, "packages")


def render_upload_form():
    content = f"""
    <div class="card">
      <h2>上传新题库</h2>
      <p class="hint" style="margin-bottom:16px">
        支持包含多级目录的 ZIP，每层有 <code>questions.json</code> 即视为一个题库包。
      </p>
      <form method="post" enctype="multipart/form-data">
        <div class="form-group">
          <label>ZIP 文件 *</label>
          <input type="file" name="file" accept=".zip" required>
        </div>
        <div class="form-group">
          <label>题库名称（可选，默认使用文件名）</label>
          <input type="text" name="name" placeholder="例如：植物学基础">
        </div>
        <div class="form-group">
          <label>描述（可选）</label>
          <textarea name="description" placeholder="简单描述这个题库..."></textarea>
        </div>
        <div class="form-group">
          <label>上传者（可选）</label>
          <input type="text" name="uploader" placeholder="你的名字或 ID">
        </div>
        <div style="display:flex;gap:8px">
          <button type="submit" class="btn btn-primary">上传</button>
          <a href="{url_for('admin_packages')}" class="btn btn-secondary">取消</a>
        </div>
      </form>
    </div>
    """
    return render_layout("上传题库", content, "packages")


def render_package_edit_form(row):
    content = f"""
    <div class="card">
      <h2>编辑题库</h2>
      <form method="post">
        <div class="form-group">
          <label>名称</label>
          <input type="text" name="name" value="{h.escape(row['name'])}" required>
        </div>
        <div class="form-group">
          <label>描述</label>
          <textarea name="description">{h.escape(row['description'] or '')}</textarea>
        </div>
        <div class="form-group">
          <label>上传者</label>
          <input type="text" name="uploader" value="{h.escape(row['uploader'] or '')}">
        </div>
        <p class="hint" style="margin-bottom:16px">
          题目数：{row['question_count']} · 文件大小：{fmt_size(row['file_size'])}
          · 上传时间：{fmt_time(row['upload_time'])} · 下载次数：{row['download_count']}
        </p>
        <div style="display:flex;gap:8px">
          <button type="submit" class="btn btn-primary">保存</button>
          <a href="{url_for('admin_packages')}" class="btn btn-secondary">取消</a>
        </div>
      </form>
    </div>
    """
    return render_layout("编辑题库", content, "packages")


def render_feedback_page(rows):
    if not rows:
        body = '<div class="empty">暂无反馈。</div>'
    else:
        trs = ""
        for r in rows:
            trs += f"""
            <tr>
              <td>{fmt_time(r['created_time'])}</td>
              <td>{h.escape(r['user_name'] or '匿名')}</td>
              <td class="content-cell">{h.escape(r['content'])}</td>
              <td>{h.escape(r['contact'] or '')}</td>
              <td>{h.escape(r['app_version'] or '')}</td>
              <td class="actions">
                <form method="post" action="{url_for('admin_feedback_delete', fb_id=r['id'])}"
                      class="inline-form"
                      onsubmit="return confirm('确认删除这条反馈？');">
                  <button type="submit" class="btn btn-sm btn-danger">删除</button>
                </form>
              </td>
            </tr>
            """
        body = f"""
        <table>
          <thead>
            <tr>
              <th>时间</th><th>用户</th><th>内容</th>
              <th>联系方式</th><th>版本</th><th>操作</th>
            </tr>
          </thead>
          <tbody>{trs}</tbody>
        </table>
        """

    content = f"""
    <div class="card">
      <div class="toolbar">
        <span class="hint">共 {len(rows)} 条反馈</span>
        <form method="post" action="{url_for('admin_feedback_clear')}" class="inline-form"
              onsubmit="return confirm('确认清空所有反馈？此操作不可恢复。');">
          <button type="submit" class="btn btn-danger">清空全部反馈</button>
        </form>
      </div>
    </div>
    <div class="card">{body}</div>
    """
    return render_layout("反馈管理", content, "feedback")


# ============================================================
#                      App 使用 API 接口（不变）
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


# ============================================================
#                     管理后台页面路由
# ============================================================
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        token = request.form.get("token", "")
        if token == ADMIN_TOKEN:
            session["admin"] = True
            flash("登录成功", "success")
            return redirect(url_for("admin_packages"))
        return render_login("令牌错误，请重试")
    if session.get("admin"):
        return redirect(url_for("admin_packages"))
    return render_login()


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


# ---------- 题库管理 ----------
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
    return render_packages_page(rows, keyword)


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
    return render_upload_form()


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
    return render_package_edit_form(row)


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


# ---------- 反馈管理 ----------
@app.route("/admin/feedback")
@admin_session_required
def admin_feedback():
    rows = get_db().execute(
        "SELECT * FROM feedback ORDER BY created_time DESC"
    ).fetchall()
    return render_feedback_page(rows)


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