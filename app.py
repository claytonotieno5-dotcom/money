import csv
import io
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from flask import (
    Flask,
    Response,
    abort,
    current_app,
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
        database_url = current_app.config.get("DATABASE_URL", "").strip()

        if database_url:
            connection = psycopg.connect(
                database_url,
                row_factory=dict_row,
                connect_timeout=10,
            )
            g.db = DatabaseConnection(connection, postgres=True)
        else:
            connection = sqlite3.connect(
                current_app.config["DATABASE"]
            )
            connection.execute("PRAGMA foreign_keys = ON")
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
                verified INTEGER NOT NULL DEFAULT 1,
                blocked INTEGER NOT NULL DEFAULT 0,
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
                verified INTEGER NOT NULL DEFAULT 1,
                blocked INTEGER NOT NULL DEFAULT 0,
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

    if db.postgres:
        db.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked INTEGER NOT NULL DEFAULT 0"
        )
    else:
        columns = db.execute("PRAGMA table_info(users)").fetchall()
        if not any(column["name"] == "blocked" for column in columns):
            db.execute(
                "ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0"
            )

    db.execute(
        "UPDATE users SET verified = 1, verification_hash = NULL, "
        "verification_expires_at = NULL, verification_attempts = 0 "
        "WHERE role = 'user' AND verified = 0"
    )

    db.commit()
    db.close()
    g.pop("db", None)


def create_app(test_config=None):
    app = Flask(__name__)

    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get("DATABASE_PATH", "money_tracker.sqlite3"),
        DATABASE_URL=os.environ.get("DATABASE_URL", ""),
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

            user = get_db().execute(
                "SELECT blocked FROM users WHERE id = ?",
                (session["user_id"],),
            ).fetchone()
            if user is None or user["blocked"]:
                session.clear()
                flash("This account is unavailable. Contact an administrator.", "error")
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

            user = get_db().execute(
                "SELECT blocked FROM users WHERE id = ?",
                (session["user_id"],),
            ).fetchone()
            if user is None or user["blocked"]:
                session.clear()
                return redirect(url_for("login"))

            return view(*args, **kwargs)

        return wrapped

    def ensure_active_user():
        if session.get("role") == "admin":
            return

        user = get_db().execute(
            "SELECT blocked FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()

        if user is None:
            session.clear()
            abort(403, "Account not found.")

        if user["blocked"]:
            session.clear()
            abort(403, "This account is unavailable.")

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
                    "Enter a valid email address.",
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
                        (name, username, email, password_hash, verified)
                        VALUES (?, ?, ?, ?, 1)
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
                    session["csrf_token"] = secrets.token_urlsafe(32)
                    return redirect(url_for("dashboard"))

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
                "SELECT * FROM users WHERE username = ? OR email = ?",
                (username, username),
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

            elif user["blocked"]:
                flash("This account is unavailable. Contact an administrator.", "error")

            else:
                session.clear()
                session["user_id"] = user["id"]
                session["role"] = user["role"]
                session["csrf_token"] = secrets.token_urlsafe(32)

                return redirect(url_for("index"))

        return render_template("login.html")

    @app.post("/logout")
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/dashboard")
    @login_required
    def dashboard():
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))

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
        ensure_active_user()

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

        monthly = db.execute(
            "SELECT monthly_budget_cents FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        now = datetime.now(timezone.utc)
        month_start = now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
        six_months_ago = (month_start.year * 12 + month_start.month - 6)
        start_year, start_month_index = divmod(six_months_ago - 1, 12)
        start_month = f"{start_year:04d}-{start_month_index + 1:02d}-01T00:00:00+00:00"
        months = db.execute(
            "SELECT substr(created_at, 1, 7) AS month, direction, SUM(amount_cents) AS total "
            "FROM transactions WHERE user_id = ? AND created_at >= ? "
            "GROUP BY substr(created_at, 1, 7), direction ORDER BY month",
            (user_id, start_month),
        ).fetchall()
        monthly_spent = db.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM transactions "
            "WHERE user_id = ? AND direction = 'used' AND created_at >= ?",
            (user_id, month_start.isoformat()),
        ).fetchone()["total"]
        transactions = db.execute(
            "SELECT direction, amount_cents, purpose, created_at FROM transactions "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT 100",
            (user_id,),
        ).fetchall()

        return jsonify(
            received=received,
            spent=spent,
            balance=received - spent,
            monthly_budget=monthly["monthly_budget_cents"] or 0,
            monthly_spent=monthly_spent,
            purposes=[dict(row) for row in purposes],
            months=[dict(row) for row in months],
            transactions=[dict(row) for row in transactions],
        )

    @app.get("/api/leaderboard")
    @login_required
    def api_leaderboard():
        if session.get("role") != "user":
            abort(403)

        rows = get_db().execute(
            """
            SELECT users.id, users.username, COUNT(transactions.id) * 10 AS points
            FROM users
            LEFT JOIN transactions ON transactions.user_id = users.id
            WHERE users.role = 'user' AND users.blocked = 0
            GROUP BY users.id, users.username, users.created_at
            ORDER BY points DESC, users.created_at ASC, users.id ASC
            """
        ).fetchall()
        ranked = [
            {"rank": index + 1,
                "username": row["username"], "points": row["points"]}
            for index, row in enumerate(rows)
        ]
        current_user = next(
            (item for item, row in zip(ranked, rows)
             if row["id"] == session["user_id"]),
            None,
        )
        return jsonify(leaders=ranked[:10], current_user=current_user)

    @app.post("/api/transactions")
    @login_required
    def api_add_transaction():
        ensure_active_user()
        payload = request.get_json(silent=True) or {}
        direction = payload.get("direction")
        purpose = str(payload.get("purpose", "")).strip()
        try:
            amount = Decimal(str(payload.get("amount", ""))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            cents = int(amount * 100)
        except (InvalidOperation, ValueError):
            return jsonify(error="Enter a valid amount."), 400
        if direction not in ("received", "used"):
            return jsonify(error="Choose money received or money used."), 400
        if cents <= 0 or cents > 10_000_000_000:
            return jsonify(error="Enter an amount greater than zero and below 100,000,000."), 400
        if not purpose or len(purpose) > 120:
            return jsonify(error="Add a purpose of 1 to 120 characters."), 400
        db = get_db()
        db.execute(
            "INSERT INTO transactions (user_id, direction, amount_cents, purpose, created_at) VALUES (?, ?, ?, ?, ?)",
            (session["user_id"], direction, cents, purpose,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
        return jsonify(ok=True), 201

    @app.post("/api/budget")
    @login_required
    def api_set_budget():
        ensure_active_user()
        payload = request.get_json(silent=True) or {}
        try:
            amount = Decimal(str(payload.get("amount", ""))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            cents = int(amount * 100)
        except (InvalidOperation, ValueError):
            return jsonify(error="Enter a valid budget amount."), 400
        if cents <= 0 or cents > 10_000_000_000:
            return jsonify(error="Enter a monthly budget greater than zero."), 400
        db = get_db()
        db.execute(
            "UPDATE users SET monthly_budget_cents = ? WHERE id = ?",
            (cents, session["user_id"]),
        )
        db.commit()
        return jsonify(ok=True), 200

    @app.get("/api/export.csv")
    @login_required
    def export_csv():
        ensure_active_user()
        rows = get_db().execute(
            "SELECT direction, amount_cents, purpose, created_at FROM transactions "
            "WHERE user_id = ? ORDER BY created_at DESC",
            (session["user_id"],),
        ).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(("type", "amount", "purpose", "recorded_at_utc"))
        for row in rows:
            writer.writerow((
                row["direction"], f"{row['amount_cents'] / 100:.2f}",
                row["purpose"], row["created_at"],
            ))
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
            "SELECT id, name, username, email, verified, blocked, created_at "
            "FROM users WHERE role = 'user' ORDER BY created_at DESC"
        ).fetchall()
        return render_template("admin.html", users=users)

    @app.post("/admin/users/<int:user_id>/block")
    @admin_required
    def block_user(user_id):
        db = get_db()
        cursor = db.execute(
            "UPDATE users SET blocked = 1 WHERE id = ? AND role = 'user'",
            (user_id,),
        )
        if cursor.rowcount == 0:
            abort(404)
        db.commit()
        flash("Account blocked.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/users/<int:user_id>/unblock")
    @admin_required
    def unblock_user(user_id):
        db = get_db()
        cursor = db.execute(
            "UPDATE users SET blocked = 0 WHERE id = ? AND role = 'user'",
            (user_id,),
        )
        if cursor.rowcount == 0:
            abort(404)
        db.commit()
        flash("Account unblocked.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template(
            "error.html",
            title="Access denied",
            message="This area is for administrators or signed-in account holders.",
        ), 403

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
