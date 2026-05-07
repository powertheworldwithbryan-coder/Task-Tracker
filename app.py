"""
Task Tracker — Flask backend with multi-user auth and workspaces.

Phase 1 features:
  - Email/password auth (open signup)
  - Personal + shared workspaces
  - Invite-link collaboration (token URLs)
  - Workspace owner can toggle "members can edit"
  - Password reset via email (Resend)
  - Per-user Daily To-Do
"""

from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
from flask import (
    Flask, request, jsonify, send_from_directory, render_template,
    redirect, url_for, flash, session,
)
from flask_cors import CORS
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, login_required, current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash

from email_helper import send_email
from db import get_db, column_exists, auto_id, USE_PG

load_dotenv()

IS_PROD = os.environ.get("FLASK_ENV", "").lower() == "production" or not os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "")
# Treat the app as production whenever DATABASE_URL is set (Render/Neon path).
IS_PROD = IS_PROD or bool(os.environ.get("DATABASE_URL"))

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"]              = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config["BASE_URL"]                = os.environ.get("APP_BASE_URL", "http://localhost:5050")
app.config["SESSION_COOKIE_SECURE"]   = IS_PROD
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
# Behind Render's proxy: trust X-Forwarded-Proto so url_for builds https URLs
if IS_PROD:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
CORS(app, supports_credentials=True)

login_manager = LoginManager(app)
login_manager.login_view = "login_page"


# ── Rate limiting (basic abuse protection on auth endpoints) ──────────
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],          # opt-in per route
    storage_uri="memory://",    # fine for single-instance free tier
)


@login_manager.unauthorized_handler
def _unauthorized():
    """Return JSON 401 for /api/* routes; redirect HTML pages to /login."""
    if request.path.startswith("/api/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect(url_for("login_page", next=request.path))


# ── DB helpers ────────────────────────────────────────────────────────
def now_str():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str():
    return date.today().isoformat()


def row_to_dict(row):
    return dict(row) if row else None


# ── Schema initialization ─────────────────────────────────────────────
def init_db():
    with get_db() as conn:
        # Legacy tables (preserved)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS tasks (
                id           {auto_id()},
                title        TEXT    NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'Not Started',
                pending_on   TEXT,
                deadline     TEXT,
                priority     TEXT    NOT NULL DEFAULT 'Medium',
                remarks      TEXT,
                category     TEXT    DEFAULT '',
                requester    TEXT    DEFAULT '',
                links        TEXT    DEFAULT '',
                completed_at TEXT,
                created_at   TEXT    NOT NULL,
                updated_at   TEXT    NOT NULL
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS daily_todos (
                id          {auto_id()},
                daily_date  TEXT    NOT NULL,
                task_id     INTEGER,
                title       TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'Not Started',
                pending_on  TEXT,
                deadline    TEXT,
                priority    TEXT    NOT NULL DEFAULT 'Medium',
                remarks     TEXT,
                created_at  TEXT    NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS subtasks (
                id         {auto_id()},
                task_id    INTEGER NOT NULL,
                title      TEXT    NOT NULL,
                done       INTEGER NOT NULL DEFAULT 0,
                position   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
        """)

        # Legacy column additions
        for col, ddl in [
            ("category",            "ALTER TABLE tasks ADD COLUMN category TEXT DEFAULT ''"),
            ("requester",           "ALTER TABLE tasks ADD COLUMN requester TEXT DEFAULT ''"),
            ("links",               "ALTER TABLE tasks ADD COLUMN links TEXT DEFAULT ''"),
            ("completed_at",        "ALTER TABLE tasks ADD COLUMN completed_at TEXT"),
            ("reminder_frequency",  "ALTER TABLE tasks ADD COLUMN reminder_frequency TEXT DEFAULT ''"),
            ("reminder_last_sent",  "ALTER TABLE tasks ADD COLUMN reminder_last_sent TEXT"),
        ]:
            if not column_exists(conn, "tasks", col):
                conn.execute(ddl)

        conn.execute("UPDATE tasks SET status = 'Dependent on Others' WHERE status = 'Blocked'")
        conn.execute("UPDATE daily_todos SET status = 'Dependent on Others' WHERE status = 'Blocked'")

        # ─── New auth + workspace tables ─────────────────────────────
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                id            {auto_id()},
                email         TEXT    NOT NULL UNIQUE,
                name          TEXT    NOT NULL DEFAULT '',
                password_hash TEXT    NOT NULL,
                created_at    TEXT    NOT NULL
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS workspaces (
                id               {auto_id()},
                name             TEXT    NOT NULL,
                owner_id         INTEGER NOT NULL,
                is_personal      INTEGER NOT NULL DEFAULT 0,
                members_can_edit INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT    NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_members (
                workspace_id INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                role         TEXT    NOT NULL DEFAULT 'member',
                joined_at    TEXT    NOT NULL,
                PRIMARY KEY (workspace_id, user_id),
                FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id)      REFERENCES users(id)      ON DELETE CASCADE
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS invites (
                id           {auto_id()},
                workspace_id INTEGER NOT NULL,
                token        TEXT    NOT NULL UNIQUE,
                created_by   INTEGER NOT NULL,
                created_at   TEXT    NOT NULL,
                expires_at   TEXT,
                used_by      INTEGER,
                used_at      TEXT,
                FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id         {auto_id()},
                user_id    INTEGER NOT NULL,
                token      TEXT    NOT NULL UNIQUE,
                created_at TEXT    NOT NULL,
                expires_at TEXT    NOT NULL,
                used_at    TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Scope columns on existing data tables
        if not column_exists(conn, "tasks", "workspace_id"):
            conn.execute("ALTER TABLE tasks ADD COLUMN workspace_id INTEGER")
        if not column_exists(conn, "tasks", "created_by"):
            conn.execute("ALTER TABLE tasks ADD COLUMN created_by INTEGER")
        if not column_exists(conn, "daily_todos", "user_id"):
            conn.execute("ALTER TABLE daily_todos ADD COLUMN user_id INTEGER")

        conn.commit()

        # First-run migration: create default user + personal workspace, attach legacy data
        existing_users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if existing_users == 0:
            _bootstrap_default_user(conn)


def _bootstrap_default_user(conn):
    """Create the seed user, attach all legacy tasks/daily_todos, and email a setup link."""
    seed_email = (os.environ.get("EMAIL_TEST_TO") or "").strip()
    if not seed_email:
        seed_email = "owner@local"
    seed_name = "Bryan"
    rand_pw   = secrets.token_urlsafe(24)

    now = now_str()
    cur = conn.execute(
        "INSERT INTO users (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (seed_email, seed_name, generate_password_hash(rand_pw), now),
    )
    user_id = cur.lastrowid

    cur = conn.execute(
        "INSERT INTO workspaces (name, owner_id, is_personal, members_can_edit, created_at) VALUES (?, ?, 1, 1, ?)",
        ("Personal", user_id, now),
    )
    ws_id = cur.lastrowid

    conn.execute(
        "INSERT INTO workspace_members (workspace_id, user_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
        (ws_id, user_id, now),
    )

    conn.execute("UPDATE tasks       SET workspace_id = ?, created_by = ? WHERE workspace_id IS NULL", (ws_id, user_id))
    conn.execute("UPDATE daily_todos SET user_id      = ?                  WHERE user_id      IS NULL", (user_id,))
    conn.commit()

    # Generate a password reset token and email a setup link
    token  = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO password_reset_tokens (user_id, token, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (user_id, token, now, expires),
    )
    conn.commit()

    base = os.environ.get("APP_BASE_URL", "http://localhost:5050").rstrip("/")
    link = f"{base}/reset-password/{token}"
    try:
        send_email(
            to=seed_email,
            subject="Task Tracker — set your password",
            html=(
                f"<h2>Welcome, {seed_name}!</h2>"
                f"<p>Your Task Tracker account is ready. Click the link below to set your password:</p>"
                f"<p><a href='{link}'>{link}</a></p>"
                f"<p>This link expires in 24 hours.</p>"
            ),
            text=f"Set your Task Tracker password: {link}",
        )
        print(f"[setup] Password setup email sent to {seed_email}")
    except Exception as e:
        print(f"[setup] WARN: could not send setup email ({e}). Use this link manually:\n  {link}")
    print(f"[setup] Created user '{seed_email}' (id={user_id}) and workspace 'Personal' (id={ws_id})")


# ── flask-login glue ──────────────────────────────────────────────────
class User(UserMixin):
    def __init__(self, row):
        self.id    = row["id"]
        self.email = row["email"]
        self.name  = row["name"]


@login_manager.user_loader
def load_user(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT id, email, name FROM users WHERE id = ?", (user_id,)).fetchone()
        return User(row) if row else None


# ── Workspace helpers ────────────────────────────────────────────────
def accessible_workspaces(conn, user_id):
    return conn.execute(
        """SELECT w.* FROM workspaces w
           JOIN workspace_members m ON m.workspace_id = w.id
           WHERE m.user_id = ?
           ORDER BY w.is_personal DESC, w.created_at""",
        (user_id,),
    ).fetchall()


def accessible_workspace_ids(conn, user_id):
    return [r["id"] for r in accessible_workspaces(conn, user_id)]


def get_workspace(conn, ws_id):
    return conn.execute("SELECT * FROM workspaces WHERE id = ?", (ws_id,)).fetchone()


def is_member(conn, ws_id, user_id):
    return conn.execute(
        "SELECT 1 FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
        (ws_id, user_id),
    ).fetchone() is not None


def can_view(conn, ws, user_id):
    return ws and is_member(conn, ws["id"], user_id)


def can_edit(conn, ws, user_id):
    if not ws or not is_member(conn, ws["id"], user_id):
        return False
    if ws["owner_id"] == user_id:
        return True
    return bool(ws["members_can_edit"])


def resolve_current_workspace(conn, user_id, requested_id=None):
    """Return workspace row the request is operating in.

    Priority: explicit ?ws=ID query param → user's personal workspace → first
    workspace they belong to. Returns None if the user doesn't have access.
    """
    if requested_id:
        try:
            wid = int(requested_id)
        except (TypeError, ValueError):
            return None
        ws = get_workspace(conn, wid)
        return ws if ws and is_member(conn, wid, user_id) else None
    rows = accessible_workspaces(conn, user_id)
    return rows[0] if rows else None


def task_workspace(conn, task_id):
    row = conn.execute("SELECT workspace_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return get_workspace(conn, row["workspace_id"]) if row else None


def attach_subtasks(conn, task_dict):
    rows = conn.execute(
        "SELECT id, title, done, position FROM subtasks WHERE task_id = ? ORDER BY position, id",
        (task_dict["id"],),
    ).fetchall()
    task_dict["subtasks"] = [dict(r) for r in rows]
    return task_dict


# ── Auth pages ────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 30 per hour", methods=["POST"])
def login_page():
    if current_user.is_authenticated:
        return redirect("/")
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw    = request.form.get("password", "")
        with get_db() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not row or not check_password_hash(row["password_hash"], pw):
            flash("Invalid email or password.", "error")
            return render_template("login.html", email=email), 401
        login_user(User(row), remember=True)
        next_url = request.args.get("next") or "/"
        return redirect(next_url)
    return render_template("login.html", email="")


@app.route("/signup", methods=["GET", "POST"])
@limiter.limit("5 per minute; 20 per hour", methods=["POST"])
def signup_page():
    if current_user.is_authenticated:
        return redirect("/")
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name  = request.form.get("name", "").strip() or email.split("@")[0]
        pw    = request.form.get("password", "")
        if not email or "@" not in email:
            flash("Please enter a valid email.", "error")
            return render_template("signup.html", email=email, name=name), 400
        if len(pw) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("signup.html", email=email, name=name), 400

        now = now_str()
        with get_db() as conn:
            existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                flash("That email is already registered. Try logging in.", "error")
                return render_template("signup.html", email=email, name=name), 400
            cur = conn.execute(
                "INSERT INTO users (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (email, name, generate_password_hash(pw), now),
            )
            uid = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO workspaces (name, owner_id, is_personal, members_can_edit, created_at) VALUES (?, ?, 1, 1, ?)",
                ("Personal", uid, now),
            )
            ws_id = cur.lastrowid
            conn.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
                (ws_id, uid, now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

        login_user(User(row), remember=True)

        # If signup came via an invite link, auto-join that workspace
        invite_token = session.pop("pending_invite", None)
        if invite_token:
            return redirect(url_for("join_invite", token=invite_token))
        return redirect("/")
    return render_template("signup.html", email="", name="")


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login_page"))


@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("3 per minute; 10 per hour", methods=["POST"])
def forgot_password_page():
    sent = False
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        with get_db() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if row:
                token = secrets.token_urlsafe(32)
                expires = (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    "INSERT INTO password_reset_tokens (user_id, token, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (row["id"], token, now_str(), expires),
                )
                conn.commit()
                base = app.config["BASE_URL"].rstrip("/")
                link = f"{base}/reset-password/{token}"
                send_email(
                    to=email,
                    subject="Task Tracker — reset your password",
                    html=(
                        f"<p>You requested a password reset for Task Tracker.</p>"
                        f"<p><a href='{link}'>Click here to set a new password</a> (link expires in 1 hour).</p>"
                        f"<p>If you didn't request this, ignore this email.</p>"
                    ),
                    text=f"Reset your Task Tracker password: {link}",
                )
        # Always show "sent" to avoid leaking which emails exist
        sent = True
    return render_template("forgot_password.html", sent=sent)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password_page(token):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM password_reset_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if not row or row["used_at"] or row["expires_at"] < now_str():
            return render_template("reset_password.html", token=None, error="This link is invalid or has expired."), 400

        if request.method == "POST":
            pw1 = request.form.get("password", "")
            pw2 = request.form.get("password_confirm", "")
            if pw1 != pw2:
                return render_template("reset_password.html", token=token, error="Passwords don't match."), 400
            if len(pw1) < 8:
                return render_template("reset_password.html", token=token, error="Password must be at least 8 characters."), 400
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(pw1), row["user_id"]),
            )
            conn.execute(
                "UPDATE password_reset_tokens SET used_at = ? WHERE id = ?",
                (now_str(), row["id"]),
            )
            conn.commit()
            flash("Password updated. Please log in.", "info")
            return redirect(url_for("login_page"))
    return render_template("reset_password.html", token=token, error=None)


# ── Account API ──────────────────────────────────────────────────────
@app.route("/api/me", methods=["GET"])
@login_required
def get_me():
    return jsonify({
        "id": current_user.id, "email": current_user.email, "name": current_user.name,
    })


@app.route("/api/me", methods=["PATCH"])
@login_required
def update_me():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    with get_db() as conn:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, current_user.id))
        conn.commit()
    return jsonify({"id": current_user.id, "name": name})


@app.route("/api/me/password", methods=["POST"])
@login_required
def change_password():
    data = request.get_json(force=True) or {}
    current_pw = data.get("current_password", "")
    new_pw     = data.get("new_password", "")
    if len(new_pw) < 8:
        return jsonify({"error": "new password must be at least 8 characters"}), 400
    with get_db() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (current_user.id,)).fetchone()
        if not check_password_hash(row["password_hash"], current_pw):
            return jsonify({"error": "current password incorrect"}), 401
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (generate_password_hash(new_pw), current_user.id))
        conn.commit()
    return jsonify({"ok": True})


# ── Workspace API ────────────────────────────────────────────────────
def _ws_to_dict(conn, ws, user_id):
    member_count = conn.execute(
        "SELECT COUNT(*) AS n FROM workspace_members WHERE workspace_id = ?", (ws["id"],),
    ).fetchone()["n"]
    return {
        "id":               ws["id"],
        "name":             ws["name"],
        "owner_id":         ws["owner_id"],
        "is_personal":      bool(ws["is_personal"]),
        "members_can_edit": bool(ws["members_can_edit"]),
        "is_owner":         ws["owner_id"] == user_id,
        "member_count":     member_count,
    }


@app.route("/api/workspaces", methods=["GET"])
@login_required
def list_workspaces():
    with get_db() as conn:
        rows = accessible_workspaces(conn, current_user.id)
        return jsonify([_ws_to_dict(conn, r, current_user.id) for r in rows])


@app.route("/api/workspaces", methods=["POST"])
@login_required
def create_workspace():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    now = now_str()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO workspaces (name, owner_id, is_personal, members_can_edit, created_at) VALUES (?, ?, 0, 1, ?)",
            (name, current_user.id, now),
        )
        ws_id = cur.lastrowid
        conn.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
            (ws_id, current_user.id, now),
        )
        conn.commit()
        ws = get_workspace(conn, ws_id)
        return jsonify(_ws_to_dict(conn, ws, current_user.id)), 201


@app.route("/api/workspaces/<int:ws_id>", methods=["PATCH"])
@login_required
def update_workspace(ws_id):
    data = request.get_json(force=True) or {}
    with get_db() as conn:
        ws = get_workspace(conn, ws_id)
        if not ws or ws["owner_id"] != current_user.id:
            return jsonify({"error": "owner only"}), 403
        if ws["is_personal"]:
            return jsonify({"error": "cannot modify personal workspace"}), 400
        fields, params = [], []
        if "name" in data:
            n = (data.get("name") or "").strip()
            if n:
                fields.append("name = ?"); params.append(n)
        if "members_can_edit" in data:
            fields.append("members_can_edit = ?"); params.append(1 if data["members_can_edit"] else 0)
        if fields:
            params.append(ws_id)
            conn.execute(f"UPDATE workspaces SET {', '.join(fields)} WHERE id = ?", params)
            conn.commit()
        ws = get_workspace(conn, ws_id)
        return jsonify(_ws_to_dict(conn, ws, current_user.id))


@app.route("/api/workspaces/<int:ws_id>", methods=["DELETE"])
@login_required
def delete_workspace(ws_id):
    with get_db() as conn:
        ws = get_workspace(conn, ws_id)
        if not ws or ws["owner_id"] != current_user.id:
            return jsonify({"error": "owner only"}), 403
        if ws["is_personal"]:
            return jsonify({"error": "cannot delete personal workspace"}), 400
        conn.execute("DELETE FROM workspaces WHERE id = ?", (ws_id,))
        conn.commit()
    return jsonify({"deleted": ws_id})


@app.route("/api/workspaces/<int:ws_id>/members", methods=["GET"])
@login_required
def list_members(ws_id):
    with get_db() as conn:
        ws = get_workspace(conn, ws_id)
        if not ws or not is_member(conn, ws_id, current_user.id):
            return jsonify({"error": "not found"}), 404
        rows = conn.execute(
            """SELECT u.id, u.email, u.name, m.role, m.joined_at
               FROM workspace_members m JOIN users u ON u.id = m.user_id
               WHERE m.workspace_id = ? ORDER BY m.joined_at""",
            (ws_id,),
        ).fetchall()
        return jsonify([dict(r) for r in rows])


@app.route("/api/workspaces/<int:ws_id>/members/<int:user_id>", methods=["DELETE"])
@login_required
def remove_member(ws_id, user_id):
    with get_db() as conn:
        ws = get_workspace(conn, ws_id)
        if not ws or ws["owner_id"] != current_user.id:
            return jsonify({"error": "owner only"}), 403
        if user_id == ws["owner_id"]:
            return jsonify({"error": "cannot remove the owner"}), 400
        conn.execute("DELETE FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
                     (ws_id, user_id))
        conn.commit()
    return jsonify({"removed": user_id})


@app.route("/api/workspaces/<int:ws_id>/invites", methods=["POST"])
@login_required
def create_invite(ws_id):
    with get_db() as conn:
        ws = get_workspace(conn, ws_id)
        if not ws or ws["owner_id"] != current_user.id:
            return jsonify({"error": "owner only"}), 403
        if ws["is_personal"]:
            return jsonify({"error": "cannot share a personal workspace"}), 400
        token = secrets.token_urlsafe(20)
        expires = (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO invites (workspace_id, token, created_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (ws_id, token, current_user.id, now_str(), expires),
        )
        conn.commit()
    base = app.config["BASE_URL"].rstrip("/")
    return jsonify({"token": token, "url": f"{base}/join/{token}", "expires_at": expires}), 201


@app.route("/join/<token>", methods=["GET", "POST"])
def join_invite(token):
    """Invite landing page. Logged-out users are redirected to login/signup
    with the token preserved in session, then auto-joined post-login."""
    with get_db() as conn:
        inv = conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
        if not inv or (inv["expires_at"] and inv["expires_at"] < now_str()):
            return render_template("join.html", error="This invite link is invalid or has expired.", workspace=None), 400
        ws = get_workspace(conn, inv["workspace_id"])
        ws_dict = dict(ws) if ws else None

        if not current_user.is_authenticated:
            session["pending_invite"] = token
            return render_template("join.html", error=None, workspace=ws_dict, token=token, needs_login=True)

        if request.method == "POST":
            already = is_member(conn, ws["id"], current_user.id)
            if not already:
                conn.execute(
                    "INSERT INTO workspace_members (workspace_id, user_id, role, joined_at) VALUES (?, ?, 'member', ?)",
                    (ws["id"], current_user.id, now_str()),
                )
                if not inv["used_at"]:
                    conn.execute(
                        "UPDATE invites SET used_at = ?, used_by = ? WHERE id = ?",
                        (now_str(), current_user.id, inv["id"]),
                    )
                conn.commit()
            session.pop("pending_invite", None)
            return redirect(f"/?ws={ws['id']}")

        already = is_member(conn, ws["id"], current_user.id)
        return render_template("join.html", error=None, workspace=ws_dict, token=token, needs_login=False, already=already)


# ── Tasks API (workspace-scoped) ─────────────────────────────────────
def _request_workspace(conn):
    """Find the workspace this request operates on. Returns row or None."""
    return resolve_current_workspace(conn, current_user.id, request.args.get("ws") or request.values.get("ws"))


@app.route("/api/tasks", methods=["GET"])
@login_required
def list_tasks():
    with get_db() as conn:
        ws = _request_workspace(conn)
        if not ws:
            return jsonify([])

        params = [ws["id"]]
        query  = "SELECT * FROM tasks WHERE workspace_id = ?"

        for arg, col, like in [
            ("status",     "status",     False),
            ("priority",   "priority",   False),
            ("pending_on", "pending_on", True),
            ("category",   "category",   False),
        ]:
            v = request.args.get(arg)
            if v:
                if like:
                    query  += f" AND {col} LIKE ?"; params.append(f"%{v}%")
                else:
                    query  += f" AND {col} = ?";    params.append(v)

        query += (" ORDER BY CASE status "
                  "WHEN 'In Progress' THEN 1 "
                  "WHEN 'Blocked - Technical' THEN 2 "
                  "WHEN 'Dependent on Others' THEN 3 "
                  "WHEN 'Not Started' THEN 4 "
                  "WHEN 'Done' THEN 5 "
                  "WHEN 'Stale' THEN 6 "
                  "WHEN 'Cancelled' THEN 7 ELSE 8 END, "
                  "CASE priority WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 "
                  "WHEN 'Medium' THEN 3 WHEN 'Low' THEN 4 ELSE 5 END, deadline ASC")

        rows = conn.execute(query, params).fetchall()
        out = [attach_subtasks(conn, dict(r)) for r in rows]
    return jsonify(out)


@app.route("/api/tasks", methods=["POST"])
@login_required
def create_task():
    data  = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400

    with get_db() as conn:
        ws = _request_workspace(conn)
        if not ws:
            return jsonify({"error": "workspace not accessible"}), 403
        if not can_edit(conn, ws, current_user.id):
            return jsonify({"error": "view-only access"}), 403

        now    = now_str()
        status = data.get("status", "Not Started")
        completed_at = now if status == "Done" else None

        cur = conn.execute(
            """INSERT INTO tasks
               (workspace_id, created_by, title, status, pending_on, deadline, priority, remarks,
                category, requester, links, completed_at, reminder_frequency,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ws["id"], current_user.id,
                title, status,
                data.get("pending_on", ""), data.get("deadline", ""),
                data.get("priority", "Medium"), data.get("remarks", ""),
                data.get("category", ""), data.get("requester", ""), data.get("links", ""),
                completed_at, data.get("reminder_frequency", ""),
                now, now,
            ),
        )
        task_id = cur.lastrowid

        for i, st in enumerate(data.get("subtasks") or []):
            t = (st.get("title") or "").strip()
            if not t:
                continue
            conn.execute(
                "INSERT INTO subtasks (task_id, title, done, position, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, t, 1 if st.get("done") else 0, i, now),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return jsonify(attach_subtasks(conn, dict(row))), 201


@app.route("/api/tasks/<int:task_id>", methods=["GET"])
@login_required
def get_task(task_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        ws = get_workspace(conn, row["workspace_id"])
        if not can_view(conn, ws, current_user.id):
            return jsonify({"error": "not found"}), 404
        return jsonify(attach_subtasks(conn, dict(row)))


@app.route("/api/tasks/<int:task_id>", methods=["PUT"])
@login_required
def update_task(task_id):
    data = request.get_json(force=True) or {}
    now  = now_str()
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not existing:
            return jsonify({"error": "not found"}), 404
        ws = get_workspace(conn, existing["workspace_id"])
        if not can_edit(conn, ws, current_user.id):
            return jsonify({"error": "no edit access"}), 403

        title      = (data.get("title") or existing["title"]).strip()
        status     = data.get("status",     existing["status"])
        pending_on = data.get("pending_on", existing["pending_on"])
        deadline   = data.get("deadline",   existing["deadline"])
        priority   = data.get("priority",   existing["priority"])
        remarks    = data.get("remarks",    existing["remarks"])
        category   = data.get("category",   existing["category"]   or "")
        requester  = data.get("requester",  existing["requester"]  or "")
        links      = data.get("links",      existing["links"]      or "")
        reminder_frequency = data.get("reminder_frequency", existing["reminder_frequency"] or "")

        if status not in ("Blocked - Technical", "Dependent on Others"):
            reminder_frequency = ""

        prev_status    = existing["status"]
        prev_completed = existing["completed_at"]
        if status == "Done" and prev_status != "Done":
            completed_at = now
        elif status != "Done":
            completed_at = None
        else:
            completed_at = prev_completed

        conn.execute(
            """UPDATE tasks SET
                  title=?, status=?, pending_on=?, deadline=?, priority=?, remarks=?,
                  category=?, requester=?, links=?, completed_at=?,
                  reminder_frequency=?, updated_at=?
               WHERE id=?""",
            (title, status, pending_on, deadline, priority, remarks,
             category, requester, links, completed_at, reminder_frequency, now, task_id),
        )

        if "subtasks" in data:
            conn.execute("DELETE FROM subtasks WHERE task_id = ?", (task_id,))
            for i, st in enumerate(data.get("subtasks") or []):
                t = (st.get("title") or "").strip()
                if not t:
                    continue
                conn.execute(
                    "INSERT INTO subtasks (task_id, title, done, position, created_at) VALUES (?, ?, ?, ?, ?)",
                    (task_id, t, 1 if st.get("done") else 0, i, now),
                )

        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return jsonify(attach_subtasks(conn, dict(row)))


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
@login_required
def delete_task(task_id):
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not existing:
            return jsonify({"error": "not found"}), 404
        ws = get_workspace(conn, existing["workspace_id"])
        if not can_edit(conn, ws, current_user.id):
            return jsonify({"error": "no edit access"}), 403
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
    return jsonify({"deleted": task_id})


@app.route("/api/subtasks/<int:sub_id>/toggle", methods=["POST"])
@login_required
def toggle_subtask(sub_id):
    with get_db() as conn:
        row = conn.execute("SELECT s.*, t.workspace_id FROM subtasks s JOIN tasks t ON t.id = s.task_id WHERE s.id = ?",
                           (sub_id,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        ws = get_workspace(conn, row["workspace_id"])
        # Subtask toggling is allowed for any member (light interaction).
        if not can_view(conn, ws, current_user.id):
            return jsonify({"error": "not found"}), 404
        new_val = 0 if row["done"] else 1
        conn.execute("UPDATE subtasks SET done = ? WHERE id = ?", (new_val, sub_id))
        conn.commit()
        return jsonify({"id": sub_id, "done": new_val})


# ── Reminders / stats / daily (all workspace-scoped) ─────────────────
@app.route("/api/reminders", methods=["GET"])
@login_required
def get_reminders():
    today = today_str()
    with get_db() as conn:
        ws = _request_workspace(conn)
        if not ws:
            return jsonify([])
        urgent = conn.execute(
            """SELECT * FROM tasks
               WHERE workspace_id = ?
                 AND status NOT IN ('Done', 'Cancelled')
                 AND deadline = ?
               ORDER BY CASE priority WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3 ELSE 4 END""",
            (ws["id"], today),
        ).fetchall()

        blocked = conn.execute(
            """SELECT * FROM tasks
               WHERE workspace_id = ?
                 AND status IN ('Blocked - Technical', 'Dependent on Others')
                 AND COALESCE(reminder_frequency,'') NOT IN ('', 'None')""",
            (ws["id"],),
        ).fetchall()

        due_blocked = []
        for t in blocked:
            freq = t["reminder_frequency"]
            days = {"Daily": 1, "Every 3 Days": 3, "Weekly": 7, "Bi-weekly": 14}.get(freq)
            if not days:
                continue
            last  = t["reminder_last_sent"] or t["updated_at"]
            try:
                last_dt = datetime.strptime(last[:10], "%Y-%m-%d").date()
            except Exception:
                last_dt = date.today()
            if (date.today() - last_dt).days >= days:
                due_blocked.append(dict(t))

    return jsonify([dict(r) for r in urgent] + due_blocked)


@app.route("/api/reminders/<int:task_id>/snooze", methods=["POST"])
@login_required
def snooze_reminder(task_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        ws = get_workspace(conn, row["workspace_id"])
        if not can_edit(conn, ws, current_user.id):
            return jsonify({"error": "no edit access"}), 403
        conn.execute("UPDATE tasks SET reminder_last_sent = ? WHERE id = ?", (now_str(), task_id))
        conn.commit()
    return jsonify({"snoozed": task_id})


@app.route("/api/stats", methods=["GET"])
@login_required
def get_stats():
    today = today_str()
    with get_db() as conn:
        ws = _request_workspace(conn)
        if not ws:
            return jsonify({"by_status": {}, "by_priority": {}, "by_category": [], "totals": {"open":0,"done":0,"overdue":0,"today":0,"urgent":0}, "recent_done": []})

        wid = ws["id"]
        status_rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks WHERE workspace_id = ? GROUP BY status", (wid,)).fetchall()
        by_status = {r["status"]: r["n"] for r in status_rows}

        prio_rows = conn.execute(
            """SELECT priority, COUNT(*) AS n FROM tasks
               WHERE workspace_id = ? AND status NOT IN ('Done','Cancelled')
               GROUP BY priority""", (wid,),
        ).fetchall()
        by_priority = {r["priority"]: r["n"] for r in prio_rows}

        cat_rows = conn.execute(
            """SELECT COALESCE(NULLIF(category,''),'(uncategorized)') AS category, COUNT(*) AS n
               FROM tasks WHERE workspace_id = ? AND status NOT IN ('Done','Cancelled')
               GROUP BY category ORDER BY n DESC LIMIT 8""", (wid,),
        ).fetchall()
        by_category = [{"category": r["category"], "n": r["n"]} for r in cat_rows]

        total_open    = sum(v for k, v in by_status.items() if k not in ("Done", "Cancelled"))
        total_done    = by_status.get("Done", 0)
        total_overdue = conn.execute(
            """SELECT COUNT(*) AS n FROM tasks
               WHERE workspace_id = ? AND status NOT IN ('Done','Cancelled')
                 AND deadline != '' AND deadline IS NOT NULL AND deadline < ?""",
            (wid, today),
        ).fetchone()["n"]
        total_today = conn.execute(
            """SELECT COUNT(*) AS n FROM tasks
               WHERE workspace_id = ? AND status NOT IN ('Done','Cancelled') AND deadline = ?""",
            (wid, today),
        ).fetchone()["n"]
        total_urgent = (by_priority.get("Critical", 0) + by_priority.get("High", 0))

        recent_done = conn.execute(
            """SELECT id, title, completed_at, category, priority FROM tasks
               WHERE workspace_id = ? AND status = 'Done' AND completed_at IS NOT NULL
               ORDER BY completed_at DESC LIMIT 5""", (wid,),
        ).fetchall()

    return jsonify({
        "by_status":   by_status,
        "by_priority": by_priority,
        "by_category": by_category,
        "totals": {
            "open":    total_open,
            "done":    total_done,
            "overdue": total_overdue,
            "today":   total_today,
            "urgent":  total_urgent,
        },
        "recent_done": [dict(r) for r in recent_done],
    })


@app.route("/api/daily", methods=["GET"])
@login_required
def get_daily():
    target_date = request.args.get("date", today_str())
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM daily_todos
               WHERE daily_date = ? AND user_id = ?
               ORDER BY CASE priority WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3 WHEN 'Low' THEN 4 ELSE 5 END""",
            (target_date, current_user.id),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/daily/generate", methods=["POST"])
@login_required
def generate_daily():
    target_date = (request.get_json(force=True) or {}).get("date", today_str())
    with get_db() as conn:
        ws_ids = accessible_workspace_ids(conn, current_user.id)
        if not ws_ids:
            return jsonify({"date": target_date, "added_count": 0, "task_ids": []})
        placeholders = ",".join(["?"] * len(ws_ids))
        rows = conn.execute(
            f"""SELECT * FROM tasks
                WHERE workspace_id IN ({placeholders})
                  AND status NOT IN ('Done', 'Cancelled')
                  AND (priority IN ('Critical', 'High') OR (deadline != '' AND deadline <= ?))""",
            (*ws_ids, target_date),
        ).fetchall()

        existing_ids = {
            r["task_id"] for r in conn.execute(
                "SELECT task_id FROM daily_todos WHERE daily_date = ? AND user_id = ?",
                (target_date, current_user.id),
            ).fetchall()
        }

        added, now = [], now_str()
        for task in rows:
            if task["id"] in existing_ids:
                continue
            conn.execute(
                """INSERT INTO daily_todos (daily_date, task_id, title, status, pending_on, deadline, priority, remarks, created_at, user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (target_date, task["id"], task["title"], task["status"],
                 task["pending_on"], task["deadline"], task["priority"], task["remarks"], now, current_user.id),
            )
            added.append(task["id"])
        conn.commit()
    return jsonify({"date": target_date, "added_count": len(added), "task_ids": added})


@app.route("/api/daily/<int:entry_id>", methods=["PUT"])
@login_required
def update_daily_entry(entry_id):
    data = request.get_json(force=True) or {}
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM daily_todos WHERE id = ? AND user_id = ?",
                                (entry_id, current_user.id)).fetchone()
        if not existing:
            return jsonify({"error": "not found"}), 404
        conn.execute(
            "UPDATE daily_todos SET status=?, remarks=? WHERE id=?",
            (data.get("status", existing["status"]), data.get("remarks", existing["remarks"]), entry_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM daily_todos WHERE id = ?", (entry_id,)).fetchone()
    return jsonify(dict(row))


@app.route("/api/daily/<int:entry_id>", methods=["DELETE"])
@login_required
def delete_daily_entry(entry_id):
    with get_db() as conn:
        cur = conn.execute("DELETE FROM daily_todos WHERE id = ? AND user_id = ?",
                           (entry_id, current_user.id))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "not found"}), 404
    return jsonify({"deleted": entry_id})





# ── Static / SPA ──────────────────────────────────────────────────────
@app.route("/sw.js")
def service_worker():
    """Serve the SW from root scope so it can intercept the whole app."""
    resp = send_from_directory(app.static_folder, "sw.js")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    # Auth gate the SPA at the root
    if path == "" and not current_user.is_authenticated:
        return redirect(url_for("login_page"))
    if path and os.path.exists(os.path.join(app.template_folder, path)):
        resp = send_from_directory(app.template_folder, path)
    else:
        if not current_user.is_authenticated:
            return redirect(url_for("login_page"))
        resp = send_from_directory(app.template_folder, "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]  = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# Run init_db at import time so gunicorn workers have the schema ready.
init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    debug = not IS_PROD
    print(f"Task Tracker running at http://localhost:{port} (debug={debug})")
    app.run(host="0.0.0.0", port=port, debug=debug)
