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
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from functools import wraps

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
import psycopg
from psycopg.rows import dict_row

DATABASE_INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)


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
            supplied = request.headers.get(
                "X-CSRF-Token") or request.form.get("csrf_token", "")
            if not expected or not secrets.compare_digest(expected, supplied):
                abort(400, "Your session expired. Refresh the page and try again.")
        session.setdefault("csrf_token", secrets.token_urlsafe(32))

    @app.context_processor
    def template_security_context():
        return {"csrf_token": session.get("csrf_token", "")}

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
                "Development verification code for %s: %s", recipient, code)
            return
        if app.config["MAIL_MODE"] == "apps-script":
            web_app_url = app.config["MAIL_WEB_APP_URL"]
            secret = app.config["MAILER_SECRET"]
            if not web_app_url or not secret:
                raise RuntimeError(
                    "Apps Script mail delivery is not configured.")
            timestamp = str(int(time.time()))
            payload = json.dumps(
                {"to": recipient, "code": code, "timestamp": timestamp},
                separators=(",", ":"),
            )
            signature = hmac.new(
                secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            body = json.dumps(
                {"payload": payload, "signature": signature}).encode("utf-8")
            mail_request = urllib.request.Request(
                web_app_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(mail_request, timeout=15) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    "Apps Script mail delivery request failed.") from error
            if not result.get("ok"):
                raise RuntimeError("Apps Script did not accept the email.")
            return
        required = ("MAIL_HOST", "MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM")
        if any(not app.config[key] for key in required):
            raise RuntimeError(
                "Email delivery is not configured. Set the MAIL_* environment variables.")
        message = EmailMessage()
        message["Subject"] = "Your Moneyline verification code"
        message["From"] = app.config["MAIL_FROM"]
        message["To"] = recipient
        message.set_content(
            f"Your account verification code is {code}. It expires in 15 minutes. "
            "If you did not request an account, you can ignore this email."
        )
        with smtplib.SMTP(app.config["MAIL_HOST"], app.config["MAIL_PORT"]) as server:
            server.starttls()
            server.login(app.config["MAIL_USERNAME"],
                         app.config["MAIL_PASSWORD"])
            server.send_message(message)

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
            username = request.form.get("username", "").strip().casefold()
            email = request.form.get("email", "").strip().casefold()
            password = request.form.get("password", "")
            if not name or len(name) > 100 or not username or len(username) > 80:
                flash("Enter your name and a username under 80 characters.", "error")
            elif "@" not in email or len(email) > 254:
                flash("Enter a valid email address for your verification code.", "error")
            elif len(password) < 10:
                flash("Use a password with at least 10 characters.", "error")
            else:
                try:
                    cursor = get_db().execute(
                        "INSERT INTO users (name, username, email, password_hash) VALUES (?, ?, ?, ?) RETURNING id",
                        (name, username, email, generate_password_hash(password)),
                    )
                    new_user_id = cursor.fetchone()["id"]
                    get_db().commit()
                except DATABASE_INTEGRITY_ERRORS:
                    flash("That username or email is already registered.", "error")
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
            username = request.form.get("username", "").strip().casefold()
            user = get_db().execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            if user is None or not check_password_hash(user["password_hash"], request.form.get("password", "")):
                flash("Username or password is incorrect.", "error")
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
            "SELECT approved, verified FROM users WHERE id = ?", (
                session["user_id"],)
        ).fetchone()
        if user is None:
            session.clear()
            return redirect(url_for("login"))
        if user["verified"]:
            session["verified"] = True
            return redirect(url_for("dashboard"))
        return render_template("pending.html", approved=bool(user["approved"]))

    @app.route("/verify", methods=["GET", "POST"])
    @login_required
    def verify():
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        user = get_db().execute("SELECT * FROM users WHERE id = ?",
                                (session["user_id"],)).fetchone()
        if user is None:
            session.clear()
            return redirect(url_for("login"))
        if user["verified"]:
            return redirect(url_for("dashboard"))
        if not user["approved"]:
            return redirect(url_for("pending"))
        if request.method == "POST":
            code = request.form.get("code", "").strip()
            now = datetime.now(timezone.utc)
            expiry = datetime.fromisoformat(
                user["verification_expires_at"]) if user["verification_expires_at"] else now
            if user["verification_attempts"] >= 5 or expiry <= now or not user["verification_hash"]:
                flash(
                    "This code has expired or too many attempts were made. Ask an administrator to resend it.", "error")
            elif check_password_hash(user["verification_hash"], code):
                get_db().execute(
                    "UPDATE users SET verified = 1, verification_hash = NULL, verification_expires_at = NULL WHERE id = ?",
                    (user["id"],),
                )
                get_db().commit()
                session["verified"] = True
                return redirect(url_for("dashboard"))
            else:
                get_db().execute(
                    "UPDATE users SET verification_attempts = verification_attempts + 1 WHERE id = ?",
                    (user["id"],),
                )
                get_db().commit()
                flash("That code is not correct.", "error")
        return render_template("verify.html", email=user["email"])

    @app.get("/dashboard")
    @login_required
    def dashboard():
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        if not session.get("verified"):
            return redirect(url_for("pending"))
        user = get_db().execute("SELECT name, monthly_budget_cents FROM users WHERE id = ?",
                                (session["user_id"],)).fetchone()
        return render_template("dashboard.html", user=user)

    @app.get("/api/summary")
    @login_required
    def api_summary():
        ensure_verified_user()
        db = get_db()
        user_id = session["user_id"]
        totals = db.execute(
            "SELECT direction, COALESCE(SUM(amount_cents), 0) AS total FROM transactions WHERE user_id = ? GROUP BY direction",
            (user_id,),
        ).fetchall()
        received = sum(row["total"]
                       for row in totals if row["direction"] == "received")
        spent = sum(row["total"]
                    for row in totals if row["direction"] == "used")
        purposes = db.execute(
            "SELECT purpose, SUM(amount_cents) AS total FROM transactions WHERE user_id = ? AND direction = 'used' GROUP BY purpose ORDER BY total DESC",
            (user_id,),
        ).fetchall()
        months = db.execute(
            "SELECT substr(created_at, 1, 7) AS month, direction, SUM(amount_cents) AS total "
            "FROM transactions WHERE user_id = ? AND created_at >= ? GROUP BY month, direction ORDER BY month",
            (user_id, (datetime.now(timezone.utc) - timedelta(days=183)).isoformat()),
        ).fetchall()
        history = db.execute(
            "SELECT id, direction, amount_cents, purpose, created_at FROM transactions WHERE user_id = ? ORDER BY created_at DESC LIMIT 100",
            (user_id,),
        ).fetchall()
        user = db.execute(
            "SELECT monthly_budget_cents FROM users WHERE id = ?", (user_id,)).fetchone()
        current_month = datetime.now(timezone.utc).strftime("%Y-%m")
        monthly_spend = db.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM transactions WHERE user_id = ? AND direction = 'used' AND substr(created_at, 1, 7) = ?",
            (user_id, current_month),
        ).fetchone()["total"]
        return jsonify(
            received=received,
            spent=spent,
            balance=received - spent,
            monthly_budget=user["monthly_budget_cents"],
            monthly_spent=monthly_spend,
            purposes=[dict(row) for row in purposes],
            months=[dict(row) for row in months],
            transactions=[dict(row) for row in history],
        )

    @app.post("/api/transactions")
    @login_required
    def api_add_transaction():
        ensure_verified_user()
        payload = request.get_json(silent=True) or {}
        direction = payload.get("direction")
        purpose = str(payload.get("purpose", "")).strip()
        try:
            amount = Decimal(str(payload.get("amount", ""))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
            cents = int(amount * 100)
        except (InvalidOperation, ValueError):
            return jsonify(error="Enter a valid amount."), 400
        if direction not in ("received", "used"):
            return jsonify(error="Choose money received or money used."), 400
        if cents <= 0 or cents > 100_000_000_00:
            return jsonify(error="Enter an amount greater than zero and below 100,000,000."), 400
        if not purpose or len(purpose) > 120:
            return jsonify(error="Add a purpose of 1 to 120 characters."), 400
        get_db().execute(
            "INSERT INTO transactions (user_id, direction, amount_cents, purpose, created_at) VALUES (?, ?, ?, ?, ?)",
            (session["user_id"], direction, cents, purpose,
             datetime.now(timezone.utc).isoformat()),
        )
        get_db().commit()
        return jsonify(ok=True), 201

    @app.post("/api/budget")
    @login_required
    def api_set_budget():
        ensure_verified_user()
        payload = request.get_json(silent=True) or {}
        try:
            amount = Decimal(str(payload.get("amount", ""))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
            cents = int(amount * 100)
        except (InvalidOperation, ValueError):
            return jsonify(error="Enter a valid budget amount."), 400
        if cents <= 0 or cents > 100_000_000_00:
            return jsonify(error="Enter a monthly budget greater than zero."), 400
        get_db().execute("UPDATE users SET monthly_budget_cents = ? WHERE id = ?",
                         (cents, session["user_id"]))
        get_db().commit()
        return jsonify(ok=True), 200

    @app.get("/api/export.csv")
    @login_required
    def export_csv():
        ensure_verified_user()
        rows = get_db().execute(
            "SELECT direction, amount_cents, purpose, created_at FROM transactions WHERE user_id = ? ORDER BY created_at DESC",
            (session["user_id"],),
        ).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(("type", "amount", "purpose", "recorded_at_utc"))
        for row in rows:
            writer.writerow(
                (row["direction"], f"{row['amount_cents'] / 100:.2f}", row["purpose"], row["created_at"]))
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=moneyline-transactions.csv"},
        )

    @app.get("/admin")
    @admin_required
    def admin_dashboard():
        users = get_db().execute(
            "SELECT u.id, u.name, u.username, u.email, u.approved, u.verified, u.created_at, "
            "COALESCE(SUM(CASE WHEN t.direction = 'received' THEN t.amount_cents ELSE 0 END), 0) AS received, "
            "COALESCE(SUM(CASE WHEN t.direction = 'used' THEN t.amount_cents ELSE 0 END), 0) AS spent "
            "FROM users u LEFT JOIN transactions t ON t.user_id = u.id "
            "WHERE u.role = 'user' GROUP BY u.id ORDER BY u.created_at DESC"
        ).fetchall()
        accounts = []
        for user in users:
            account = dict(user)
            account["transactions"] = get_db().execute(
                "SELECT direction, amount_cents, purpose, created_at FROM transactions WHERE user_id = ? ORDER BY created_at DESC",
                (user["id"],),
            ).fetchall()
            accounts.append(account)
        return render_template("admin.html", users=accounts)

    @app.post("/admin/users/<int:user_id>/approve")
    @admin_required
    def approve_user(user_id):
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE id = ? AND role = 'user'", (user_id,)).fetchone()
        if user is None:
            abort(404)
        if user["verified"]:
            flash("This account is already verified.", "error")
            return redirect(url_for("admin_dashboard"))
        code = str(secrets.randbelow(100_000) + 1)
        expires = datetime.now(timezone.utc) + timedelta(minutes=15)
        try:
            send_verification_code(user["email"], code)
        except (OSError, smtplib.SMTPException, RuntimeError) as error:
            app.logger.error("Could not send verification email: %s", error)
            flash(
                "Email could not be sent. Check mail configuration and try again.", "error")
            return redirect(url_for("admin_dashboard"))
        db.execute(
            "UPDATE users SET approved = 1, verification_hash = ?, verification_expires_at = ?, verification_attempts = 0 WHERE id = ?",
            (generate_password_hash(code), expires.isoformat(), user_id),
        )
        db.commit()
        if app.config["MAIL_MODE"] == "console":
            flash(
                f"Approved {user['name']}. Local mode did not send email; use the code printed in the app server terminal.",
                "success",
            )
        else:
            flash(
                f"Approved {user['name']}; the mail server accepted the verification email. Check spam or resend if it does not arrive.",
                "success",
            )
        return redirect(url_for("admin_dashboard"))

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("error.html", title="Access denied", message="This area is for administrators."), 403

    def ensure_verified_user():
        if session.get("role") != "user" or not session.get("verified"):
            abort(403)

    return app


def get_db():
    if "db" not in g:
        from flask import current_app

        database_url = current_app.config.get("DATABASE_URL", "")
        if database_url:
            connection = psycopg.connect(
                database_url, row_factory=dict_row, connect_timeout=10)
            g.db = DatabaseConnection(connection, postgres=True)
        else:
            connection = sqlite3.connect(current_app.config["DATABASE"])
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            g.db = DatabaseConnection(connection, postgres=False)
    return g.db


class DatabaseConnection:
    def __init__(self, connection, postgres):
        self.connection = connection
        self.postgres = postgres

    def execute(self, statement, parameters=()):
        if self.postgres:
            statement = statement.replace("?", "%s")
        return self.connection.execute(statement, parameters)

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.close()


def init_db():
    from flask import current_app

    database_url = current_app.config.get("DATABASE_URL", "")
    if database_url:
        connection = psycopg.connect(
            database_url, row_factory=dict_row, connect_timeout=10)
        db = DatabaseConnection(connection, postgres=True)
        schema = (
            """CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
                approved INTEGER NOT NULL DEFAULT 0,
                verified INTEGER NOT NULL DEFAULT 0,
                verification_hash TEXT,
                verification_expires_at TEXT,
                verification_attempts INTEGER NOT NULL DEFAULT 0,
                monthly_budget_cents BIGINT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS transactions (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                direction TEXT NOT NULL CHECK (direction IN ('received', 'used')),
                amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
                purpose TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS transactions_user_date ON transactions(user_id, created_at)",
        )
    else:
        connection = sqlite3.connect(current_app.config["DATABASE"])
        connection.execute("PRAGMA foreign_keys = ON")
        db = DatabaseConnection(connection, postgres=False)
        schema = (
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
                approved INTEGER NOT NULL DEFAULT 0,
                verified INTEGER NOT NULL DEFAULT 0,
                verification_hash TEXT,
                verification_expires_at TEXT,
                verification_attempts INTEGER NOT NULL DEFAULT 0,
                monthly_budget_cents INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                direction TEXT NOT NULL CHECK (direction IN ('received', 'used')),
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                purpose TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS transactions_user_date ON transactions(user_id, created_at)",
        )
    try:
        for statement in schema:
            db.execute(statement)
        db.commit()
    finally:
        db.close()


app = create_app()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
