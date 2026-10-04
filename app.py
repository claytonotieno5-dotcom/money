import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import smtplib
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


DATABASE_INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)


class DatabaseConnection:
    def __init__(self, connection, postgres=False):
        self.connection = connection
        self.postgres = postgres

    def execute(self, statement, parameters=()):
        if self.postgres:
            statement = statement.replace("?", "%s")
        return self.connection.execute(statement, parameters)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


def get_db():
    if "db" not in g:
        database_url = os.environ.get("DATABASE_URL", "").strip()

        if database_url:
            connection = psycopg.connect(
                database_url,
                row_factory=dict_row,
                connect_timeout=10,
            )
            g.db = DatabaseConnection(connection, postgres=True)
        else:
            connection = sqlite3.connect(
                os.environ.get("DATABASE_PATH", "money_tracker.sqlite3")
            )
            connection.row_factory = sqlite3.Row
            g.db = DatabaseConnection(connection, postgres=False)

    return g.db


def init_db():
    db = get_db()

    if db.postgres:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                approved INTEGER NOT NULL DEFAULT 0,
                verified INTEGER NOT NULL DEFAULT 0,
                verification_hash TEXT,
                verification_expires_at TEXT,
                verification_attempts INTEGER NOT NULL DEFAULT 0,
                monthly_budget_cents INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                direction TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                purpose TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
    else:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                approved INTEGER NOT NULL DEFAULT 0,
                verified INTEGER NOT NULL DEFAULT 0,
                verification_hash TEXT,
                verification_expires_at TEXT,
                verification_attempts INTEGER NOT NULL DEFAULT 0,
                monthly_budget_cents INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                direction TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                purpose TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

    db.commit()


def create_app(test_config=None):
    app = Flask(__name__)

    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get("DATABASE_PATH", "money_tracker.sqlite3"),
        DATABASE_URL=os.environ.get("DATABASE_URL", ""),
        MAIL_MODE=os.environ.get("MAIL_MODE", "console"),
        MAIL_HOST=os.environ.get("MAIL_HOST", ""),
        MAIL_PORT=int(os.environ.get("MAIL_PORT", "587")),
        MAIL_USERNAME=os.environ.get("MAIL_USERNAME", ""),
        MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", ""),
        MAIL_FROM=os.environ.get("MAIL_FROM", ""),
        MAIL_WEB_APP_URL=os.environ.get("MAIL_WEB_APP_URL", ""),
        MAILER_SECRET=os.environ.get("MAILER_SECRET", ""),
        ADMIN_SETUP_KEY=os.environ.get("ADMIN_SETUP_KEY", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    )

    if test_config:
        app.config.update(test_config)

    with app.app_context():
        init_db()

    @app.before_request
    def protect_post_requests():
        if request.method == "POST":
            expected = session.get("csrf_token", "")
            supplied = (
                request.headers.get("X-CSRF-Token")
                or request.form.get("csrf_token", "")
            )

            if not expected or not secrets.compare_digest(expected, supplied):
                abort(
                    400,
                    "Your session expired. Refresh the page and try again.",
                )

        session.setdefault(
            "csrf_token",
            secrets.token_urlsafe(32),
        )

    @app.context_processor
    def template_security_context():
        return {
            "csrf_token": session.get("csrf_token", "")
        }

    @app.teardown_appcontext
    def close_db(_error=None):
        connection = g.pop("db", None)

        if connection is not None:
            connection.close()

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))

            return view(*args, **kwargs)

        return wrapped

    def admin_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))

            if session.get("role") != "admin":
                abort(403)

            return view(*args, **kwargs)

        return wrapped

    def send_verification_code(recipient, code):
        if app.config["MAIL_MODE"] == "console":
            app.logger.warning(
                "Development verification code for %s: %s",
                recipient,
                code,
            )
            return

        if app.config["MAIL_MODE"] == "apps-script":
            web_app_url = app.config["MAIL_WEB_APP_URL"]
            secret = app.config["MAILER_SECRET"]

            if not web_app_url or not secret:
                raise RuntimeError(
                    "Apps Script mail delivery is not configured."
                )

            timestamp = str(int(time.time()))

            payload = json.dumps(
                {
                    "to": recipient,
                    "code": code,
                    "timestamp": timestamp,
                },
                separators=(",", ":"),
            )

            signature = hmac.new(
                secret.encode("utf-8"),
                payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()

            body = json.dumps(
                {
                    "payload": payload,
                    "signature": signature,
                }
            ).encode("utf-8")

            mail_request = urllib.request.Request(
                web_app_url,
                data=body,
                headers={
                    "Content-Type": "application/json"
                },
                method="POST",
            )

            try:
                with urllib.request.urlopen(
                    mail_request,
                    timeout=15,
                ) as response:
                    result = json.loads(
                        response.read().decode("utf-8")
                    )

            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    "Apps Script mail delivery request failed."
                ) from error

            if not result.get("ok"):
                raise RuntimeError(
                    "Apps Script did not accept the email."
                )

            return

        required = (
            "MAIL_HOST",
            "MAIL_USERNAME",
            "MAIL_PASSWORD",
            "MAIL_FROM",
        )

        if any(not app.config[key] for key in required):
            raise RuntimeError(
                "Email delivery is not configured. "
                "Set the MAIL_* environment variables."
            )

        message = EmailMessage()
        message["Subject"] = "Your Moneyline verification code"
        message["From"] = app.config["MAIL_FROM"]
        message["To"] = recipient

        message.set_content(
            f"Your account verification code is {code}. "
            "It expires in 15 minutes. "
            "If you did not request an account, you can ignore this email."
        )

        with smtplib.SMTP(
            app.config["MAIL_HOST"],
            app.config["MAIL_PORT"],
        ) as server:
            server.starttls()
            server.login(
                app.config["MAIL_USERNAME"],
                app.config["MAIL_PASSWORD"],
            )
            server.send_message(message)

    def ensure_verified_user():
        if session.get("role") == "admin":
            return

        if not session.get("verified"):
            abort(403, "Your account must be verified.")

        user = get_db().execute(
            "SELECT verified FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()

        if user is None:
            session.clear()
            abort(403, "Account not found.")

        if not user["verified"]:
            session["verified"] = False
            abort(403, "Your account must be verified.")

    @app.get("/service-worker.js")
    def service_worker():
        response = app.send_static_file("service-worker.js")
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def index():
        if "user_id" not in session:
            return redirect(url_for("login"))

        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))

        if not session.get("verified"):
            return redirect(url_for("pending"))

        return redirect(url_for("dashboard"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            username = request.form.get(
                "username",
                "",
            ).strip().casefold()
            email = request.form.get(
                "email",
                "",
            ).strip().casefold()
            password = request.form.get("password", "")

            if (
                not name
                or len(name) > 100
                or not username
                or len(username) > 80
            ):
                flash(
                    "Enter your name and a username under 80 characters.",
                    "error",
                )

            elif "@" not in email or len(email) > 254:
                flash(
                    "Enter a valid email address for your verification code.",
                    "error",
                )

            elif len(password) < 10:
                flash(
                    "Use a password with at least 10 characters.",
                    "error",
                )

            else:
                try:
                    cursor = get_db().execute(
                        """
                        INSERT INTO users
                        (name, username, email, password_hash)
                        VALUES (?, ?, ?, ?)
                        RETURNING id
                        """,
                        (
                            name,
                            username,
                            email,
                            generate_password_hash(password),
                        ),
                    )

                    new_user_id = cursor.fetchone()["id"]
                    get_db().commit()

                except DATABASE_INTEGRITY_ERRORS:
                    get_db().rollback()

                    flash(
                        "That username or email is already registered.",
                        "error",
                    )

                else:
                    session.clear()
                    session["user_id"] = new_user_id
                    session["role"] = "user"
                    session["verified"] = False
                    session["csrf_token"] = secrets.token_urlsafe(32)

                    return redirect(url_for("pending"))

        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get(
                "username",
                "",
            ).strip().casefold()

            password = request.form.get(
                "password",
                "",
            )

            user = get_db().execute(
                "SELECT * FROM users WHERE username = ?",
                (username,),
            ).fetchone()

            if (
                user is None
                or not check_password_hash(
                    user["password_hash"],
                    password,
                )
            ):
                flash(
                    "Username or password is incorrect.",
                    "error",
                )

            else:
                session.clear()
                session["user_id"] = user["id"]
                session["role"] = user["role"]
                session["verified"] = bool(user["verified"])
                session["csrf_token"] = secrets.token_urlsafe(32)

                return redirect(url_for("index"))

        return render_template("login.html")

    @app.post("/logout")
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/pending")
    @login_required
    def pending():
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))

        user = get_db().execute(
            "SELECT approved, verified FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()

        if user is None:
            session.clear()
            return redirect(url_for("login"))

        if user["verified"]:
            session["verified"] = True
            return redirect(url_for("dashboard"))

        return render_template(
            "pending.html",
            approved=bool(user["approved"]),
        )

    @app.route("/verify", methods=["GET", "POST"])
    @login_required
    def verify():
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))

        user = get_db().execute(
            "SELECT * FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()

        if user is None:
            session.clear()
            return redirect(url_for("login"))

        if user["verified"]:
            return redirect(url_for("dashboard"))

        if not user["approved"]:
            return redirect(url_for("pending"))

        if request.method == "POST":
            code = request.form.get(
                "code",
                "",
            ).strip()

            now = datetime.now(timezone.utc)

            expiry = (
                datetime.fromisoformat(
                    user["verification_expires_at"]
                )
                if user["verification_expires_at"]
                else now
            )

            if (
                user["verification_attempts"] >= 5
                or expiry <= now
                or not user["verification_hash"]
            ):
                flash(
                    "This code has expired or too many attempts "
                    "were made. Ask an administrator to resend it.",
                    "error",
                )

            elif check_password_hash(
                user["verification_hash"],
                code,
            ):
                get_db().execute(
                    """
                    UPDATE users
                    SET verified = 1,
                        verification_hash = NULL,
                        verification_expires_at = NULL
                    WHERE id = ?
                    """,
                    (user["id"],),
                )

                get_db().commit()

                session["verified"] = True

                return redirect(url_for("dashboard"))

            else:
                get_db().execute(
                    """
                    UPDATE users
                    SET verification_attempts =
                        verification_attempts + 1
                    WHERE id = ?
                    """,
                    (user["id"],),
                )

                get_db().commit()

                flash(
                    "That code is not correct.",
                    "error",
                )

        return render_template(
            "verify.html",
            email=user["email"],
        )

    @app.get("/dashboard")
    @login_required
    def dashboard():
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))

        if not session.get("verified"):
            return redirect(url_for("pending"))

        user = get_db().execute(
            """
            SELECT name, monthly_budget_cents
            FROM users
            WHERE id = ?
            """,
            (session["user_id"],),
        ).fetchone()

        return render_template(
            "dashboard.html",
            user=user,
        )

    @app.get("/api/summary")
    @login_required
    def api_summary():
        ensure_verified_user()

        db = get_db()
        user_id = session["user_id"]

        totals = db.execute(
            """
            SELECT direction,
                   COALESCE(SUM(amount_cents), 0) AS total
            FROM transactions
            WHERE user_id = ?
            GROUP BY direction
            """,
            (user_id,),
        ).fetchall()

        received = sum(
            row["total"]
            for row in totals
            if row["direction"] == "received"
        )

        spent = sum(
            row["total"]
            for row in totals
            if row["direction"] == "used"
        )

        purposes = db.execute(
            """
            SELECT purpose,
                   SUM(amount_cents) AS total
            FROM transactions
            WHERE user_id = ?
              AND direction = 'used'
            GROUP BY purpose
            ORDER BY total DESC
            """,
            (user_id,),
        ).fetchall()

        months = db.execute()
