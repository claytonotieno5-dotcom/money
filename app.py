import csv
import base64
import io
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from io import BytesIO

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
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.exceptions import RequestEntityTooLarge


DATABASE_INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 500
MAX_OCR_TEXT_LENGTH = 20_000


def read_profile_picture(upload):
    if upload is None or not upload.filename:
        return None, None

    content = upload.stream.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("Profile photos must be 2 MB or smaller.")

    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        mime_type = "image/jpeg"
    elif (
        len(content) >= 12
        and content.startswith(b"RIFF")
        and content[8:12] == b"WEBP"
    ):
        mime_type = "image/webp"
    else:
        raise ValueError("Choose a PNG, JPEG, or WebP image.")

    return base64.b64encode(content).decode("ascii"), mime_type


def parse_import_amount(value):
    cleaned = re.sub(r"(?i)\b(?:KES|KSH)\b", "", str(value)).replace(",", "").strip()
    try:
        amount = Decimal(cleaned).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0 or amount > Decimal("100000000"):
        return None
    return format(amount, ".2f")


def parse_import_date(value):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for date_format in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
            try:
                parsed = datetime.strptime(value, date_format)
                break
            except ValueError:
                continue
        if parsed is None:
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def parse_csv_transactions(content):
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("The CSV must use UTF-8 text encoding.") from error

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("The CSV file needs a header row.")

    columns = {
        re.sub(r"[_\s]+", " ", name.strip().casefold()): name
        for name in reader.fieldnames
        if name
    }

    def column(*names):
        return next((columns[name] for name in names if name in columns), None)

    direction_key = column("type", "direction", "transaction type", "flow")
    amount_key = column(
        "amount", "amount kes", "amount ksh", "amount (kes)",
        "amount (ksh)", "value", "value kes", "value (kes)",
    )
    purpose_key = column(
        "purpose", "description", "details", "narration", "note", "merchant"
    )
    date_key = column("date", "transaction date", "recorded at utc", "timestamp")
    if not direction_key or not amount_key or not purpose_key:
        raise ValueError(
            "Include columns for type (received/used), amount, and purpose or description."
        )

    received_terms = {
        "received", "money received", "income", "credit", "deposit",
        "deposited", "money in", "earned", "inflow",
    }
    used_terms = {
        "used", "money used", "expense", "debit", "withdrawal",
        "withdrawn", "money out", "spent", "payment", "outflow",
    }
    transactions = []
    skipped = 0
    row_count = 0
    for row in reader:
        if not any(str(value or "").strip() for value in row.values()):
            continue
        row_count += 1
        if row_count > MAX_IMPORT_ROWS:
            raise ValueError(f"Import no more than {MAX_IMPORT_ROWS} rows at a time.")
        direction_value = str(row.get(direction_key, "")).strip().casefold()
        if direction_value in received_terms:
            direction = "received"
        elif direction_value in used_terms:
            direction = "used"
        else:
            skipped += 1
            continue
        amount = parse_import_amount(row.get(amount_key, ""))
        purpose = str(row.get(purpose_key, "")).strip()[:120]
        if amount is None or not purpose:
            skipped += 1
            continue
        raw_date = row.get(date_key, "") if date_key else ""
        normalized_date = parse_import_date(raw_date)
        transactions.append({
            "direction": direction,
            "amount": amount,
            "purpose": purpose,
            "date": normalized_date,
            "date_warning": bool(str(raw_date or "").strip() and not normalized_date),
        })

    if not transactions:
        raise ValueError("No readable transactions were found in this CSV.")
    return transactions, skipped


def parse_ocr_transactions(text):
    if len(text) > MAX_OCR_TEXT_LENGTH:
        raise ValueError("The extracted text is too long to preview.")

    kind = (
        r"received|income|earned|deposit(?:ed)?|credit(?:ed)?|inflow|"
        r"spent|paid|used|bought|purchase|debit(?:ed)?|withdraw(?:al|n)?|"
        r"expense|payment|outflow"
    )
    amount = (
        r"(?:KES|KSH)?\s*(?P<amount>[0-9][0-9,]*(?:\.[0-9]{1,2})?)"
        r"\s*(?:KES|KSH)?"
    )
    transactions = []
    for line in text.splitlines():
        line = line.strip()
        match = re.search(
            rf"\b(?P<kind>{kind})\b\s+{amount}\s+"
            r"(?:(?:from|for|on|as|via)\s+)?(?P<purpose>[\w][\w .,&'-]{0,119})",
            line,
            re.IGNORECASE,
        )
        if not match:
            continue
        direction = (
            "received"
            if match.group("kind").casefold() in {
                "received", "income", "earned", "deposit", "deposited",
                "credit", "credited", "inflow",
            }
            else "used"
        )
        normalized_amount = parse_import_amount(match.group("amount"))
        purpose = match.group("purpose").strip(" .,:;-")[:120]
        if normalized_amount and purpose:
            transactions.append({
                "direction": direction,
                "amount": normalized_amount,
                "purpose": purpose,
                "date": "",
            })
        if len(transactions) >= MAX_IMPORT_ROWS:
            break
    if not transactions:
        raise ValueError(
            "No clear income or spending rows were detected. Use a CSV or enter the details manually."
        )
    return transactions


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
                profile_picture_data TEXT,
                profile_picture_mime TEXT,
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
                profile_picture_data TEXT,
                profile_picture_mime TEXT,
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
        db.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_picture_data TEXT"
        )
        db.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_picture_mime TEXT"
        )
    else:
        columns = db.execute("PRAGMA table_info(users)").fetchall()
        if not any(column["name"] == "blocked" for column in columns):
            db.execute(
                "ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0"
            )
        if not any(column["name"] == "profile_picture_data" for column in columns):
            db.execute("ALTER TABLE users ADD COLUMN profile_picture_data TEXT")
        if not any(column["name"] == "profile_picture_mime" for column in columns):
            db.execute("ALTER TABLE users ADD COLUMN profile_picture_mime TEXT")

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
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES + 64 * 1024,
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
        user = None
        if session.get("user_id") and session.get("role") == "user":
            user = get_db().execute(
                "SELECT name, profile_picture_mime FROM users WHERE id = ?",
                (session["user_id"],),
            ).fetchone()
        return {
            "csrf_token": session.get("csrf_token", ""),
            "profile_name": user["name"] if user else "",
            "profile_picture_available": bool(
                user and user["profile_picture_mime"]
            ),
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
            profile_picture = (None, None)

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
                    profile_picture = read_profile_picture(
                        request.files.get("profile_picture")
                    )
                except ValueError as error:
                    flash(str(error), "error")
                    return render_template("register.html"), 400

                try:
                    cursor = get_db().execute(
                        """
                        INSERT INTO users
                        (name, username, email, password_hash, verified,
                         profile_picture_data, profile_picture_mime)
                        VALUES (?, ?, ?, ?, 1, ?, ?)
                        RETURNING id
                        """,
                        (
                            name,
                            username,
                            email,
                            generate_password_hash(password),
                            *profile_picture,
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
            SELECT name, monthly_budget_cents, profile_picture_mime
            FROM users
            WHERE id = ?
            """,
            (session["user_id"],),
        ).fetchone()

        return render_template(
            "dashboard.html",
            user=user,
            active_page="dashboard",
        )

    @app.get("/transactions")
    @login_required
    def transactions_page():
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        return render_template(
            "transactions.html",
            user=get_db().execute(
                "SELECT name FROM users WHERE id = ?",
                (session["user_id"],),
            ).fetchone(),
            active_page="transactions",
        )

    @app.get("/import")
    @login_required
    def import_page():
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        return render_template(
            "import.html",
            active_page="import",
        )

    @app.get("/assistant")
    @login_required
    def assistant_page():
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        return render_template(
            "assistant.html",
            active_page="assistant",
        )

    @app.get("/profile-picture")
    @login_required
    def profile_picture():
        user = get_db().execute(
            "SELECT profile_picture_data, profile_picture_mime FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()
        if user is None or not user["profile_picture_data"]:
            abort(404)
        content = base64.b64decode(user["profile_picture_data"], validate=True)
        response = send_file(
            BytesIO(content),
            mimetype=user["profile_picture_mime"],
            max_age=0,
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    def update_profile():
        ensure_active_user()
        if session.get("role") != "user":
            abort(403)
        if request.method == "GET":
            return render_template(
                "profile.html",
                user=get_db().execute(
                    "SELECT name, username, email, profile_picture_mime "
                    "FROM users WHERE id = ?",
                    (session["user_id"],),
                ).fetchone(),
                active_page="profile",
            )
        try:
            picture_data, picture_mime = read_profile_picture(
                request.files.get("profile_picture")
            )
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("dashboard"))
        if picture_data is None:
            flash("Choose a profile photo before saving.", "error")
            return redirect(url_for("dashboard"))
        db = get_db()
        db.execute(
            "UPDATE users SET profile_picture_data = ?, profile_picture_mime = ? "
            "WHERE id = ?",
            (picture_data, picture_mime, session["user_id"]),
        )
        db.commit()
        flash("Profile photo updated.", "success")
        return redirect(url_for("dashboard"))

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
            "SELECT id, direction, amount_cents, purpose, created_at FROM transactions "
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

    @app.delete("/api/transactions/<int:transaction_id>")
    @login_required
    def api_delete_transaction(transaction_id):
        ensure_active_user()
        cursor = get_db().execute(
            "DELETE FROM transactions WHERE id = ? AND user_id = ?",
            (transaction_id, session["user_id"]),
        )
        if cursor.rowcount == 0:
            abort(404)
        get_db().commit()
        return jsonify(ok=True)

    @app.post("/api/import/preview")
    @login_required
    def api_import_preview():
        ensure_active_user()
        upload = request.files.get("file")
        if upload is not None:
            if not upload.filename or not upload.filename.casefold().endswith(".csv"):
                return jsonify(
                    error="Choose a CSV file. For photos, use the on-device photo scanner."
                ), 415
            content = upload.stream.read(MAX_UPLOAD_BYTES + 1)
            if len(content) > MAX_UPLOAD_BYTES:
                return jsonify(error="CSV files must be 2 MB or smaller."), 413
            try:
                transactions, skipped = parse_csv_transactions(content)
            except ValueError as error:
                return jsonify(error=str(error)), 400
        else:
            payload = request.get_json(silent=True) or {}
            extracted_text = payload.get("text", "")
            if not isinstance(extracted_text, str) or not extracted_text.strip():
                return jsonify(error="Choose a CSV file or scan a photo first."), 400
            try:
                transactions = parse_ocr_transactions(extracted_text)
            except ValueError as error:
                return jsonify(error=str(error)), 400
            skipped = 0

        return jsonify(transactions=transactions, skipped=skipped)

    @app.post("/api/transactions/import")
    @login_required
    def api_import_transactions():
        ensure_active_user()
        payload = request.get_json(silent=True) or {}
        transactions = payload.get("transactions")
        if (
            not isinstance(transactions, list)
            or not transactions
            or len(transactions) > MAX_IMPORT_ROWS
        ):
            return jsonify(
                error=f"Select between 1 and {MAX_IMPORT_ROWS} transactions to add."
            ), 400

        validated = []
        for transaction in transactions:
            if not isinstance(transaction, dict):
                return jsonify(error="The preview contains an invalid transaction."), 400
            direction = transaction.get("direction")
            purpose_value = transaction.get("purpose", "")
            if not isinstance(purpose_value, str):
                return jsonify(error="Each purpose must be text."), 400
            purpose = purpose_value.strip()
            amount = parse_import_amount(transaction.get("amount", ""))
            if direction not in ("received", "used"):
                return jsonify(error="Choose received or used for each row."), 400
            if amount is None:
                return jsonify(error="Each amount must be greater than zero."), 400
            if not purpose or len(purpose) > 120:
                return jsonify(error="Each purpose must contain 1 to 120 characters."), 400
            raw_date = str(transaction.get("date", "")).strip()
            parsed_date = parse_import_date(raw_date)
            if raw_date and not parsed_date:
                return jsonify(error="Use a valid date for each transaction."), 400
            validated.append((
                session["user_id"],
                direction,
                int(Decimal(amount) * 100),
                purpose,
                parsed_date or datetime.now(timezone.utc).isoformat(),
            ))

        db = get_db()
        for transaction in validated:
            db.execute(
                "INSERT INTO transactions "
                "(user_id, direction, amount_cents, purpose, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                transaction,
            )
        db.commit()
        return jsonify(ok=True, count=len(validated)), 201

    @app.post("/api/coach")
    @login_required
    def api_financial_coach():
        ensure_active_user()
        payload = request.get_json(silent=True) or {}
        question = payload.get("question", "")
        if not isinstance(question, str) or not question.strip() or len(question) > 300:
            return jsonify(error="Ask a question of 1 to 300 characters."), 400
        question = question.casefold()
        finance_terms = (
            "money", "finance", "financial", "budget", "spend", "spent",
            "spending", "income", "received", "balance", "save", "saving",
            "savings",
            "transaction", "afford", "expense", "expenses",
        )
        if not any(re.search(rf"\b{re.escape(term)}\b", question) for term in finance_terms):
            return jsonify(
                error="I can help with your Moneyline transactions, spending, balance, and monthly budget."
            ), 400
        if re.search(r"\b(invest(?:ment|ing)?|tax(?:es)?|debt|loan|mortgage|crypto)\b", question):
            return jsonify(
                answer=(
                    "I can summarize your Moneyline records and offer basic budgeting prompts, "
                    "but I cannot provide investment, tax, credit, or debt recommendations."
                )
            )

        db = get_db()
        user_id = session["user_id"]
        totals = db.execute(
            "SELECT direction, COALESCE(SUM(amount_cents), 0) AS total "
            "FROM transactions WHERE user_id = ? GROUP BY direction",
            (user_id,),
        ).fetchall()
        received = sum(row["total"] for row in totals if row["direction"] == "received")
        spent = sum(row["total"] for row in totals if row["direction"] == "used")
        monthly_budget = db.execute(
            "SELECT monthly_budget_cents FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()["monthly_budget_cents"] or 0
        monthly_spent = db.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM transactions "
            "WHERE user_id = ? AND direction = 'used' "
            "AND created_at >= ?",
            (
                user_id,
                datetime.now(timezone.utc).replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0
                ).isoformat(),
            ),
        ).fetchone()["total"]
        largest = db.execute(
            "SELECT purpose, SUM(amount_cents) AS total FROM transactions "
            "WHERE user_id = ? AND direction = 'used' "
            "GROUP BY purpose ORDER BY total DESC LIMIT 1",
            (user_id,),
        ).fetchone()

        if "budget" in question:
            if not monthly_budget:
                answer = (
                    "You have not set a monthly budget yet. Set one that fits your "
                    "income and essential costs, then compare it with your recorded spending."
                )
            else:
                remaining = monthly_budget - monthly_spent
                if remaining >= 0:
                    answer = (
                        f"You have used KES {monthly_spent / 100:,.2f} of your "
                        f"KES {monthly_budget / 100:,.2f} monthly budget, with "
                        f"KES {remaining / 100:,.2f} remaining."
                    )
                else:
                    answer = (
                        f"You are KES {abs(remaining) / 100:,.2f} over your "
                        f"KES {monthly_budget / 100:,.2f} monthly budget so far. "
                        "Review upcoming non-essential spending and adjust your plan if needed."
                    )
        elif any(term in question for term in ("spend", "expense")):
            answer = (
                f"Your recorded spending is KES {spent / 100:,.2f} in total. "
                if spent
                else "You have no spending recorded yet. "
            )
            if largest:
                answer += (
                    f"Your largest recorded category is {largest['purpose']} "
                    f"at KES {largest['total'] / 100:,.2f}. Check that category "
                    "for a realistic opportunity to save without cutting essentials."
                )
            else:
                answer += "Add a spending entry to get a category breakdown."
        elif any(term in question for term in ("save", "saving", "savings", "afford")):
            if largest:
                answer = (
                    f"Your largest recorded spending category is {largest['purpose']} "
                    f"at KES {largest['total'] / 100:,.2f}. Compare it with your needs "
                    "and budget to identify a realistic saving goal."
                )
            else:
                answer = (
                    "Start by recording income and spending, then set a realistic "
                    "monthly budget before choosing a savings target."
                )
        else:
            answer = (
                f"Your recorded income is KES {received / 100:,.2f}, spending is "
                f"KES {spent / 100:,.2f}, and ledger balance is "
                f"KES {(received - spent) / 100:,.2f}. These totals reflect only "
                "transactions you have entered."
            )
        return jsonify(answer=answer)

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

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify(
                error="The requested record was not found or is no longer available."
            ), 404
        return render_template(
            "error.html",
            title="Page not found",
            message="The page or record you requested could not be found.",
        ), 404

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_error):
        message = "Files and requests must be smaller than 2 MB."
        if request.path.startswith("/api/"):
            return jsonify(error=message), 413
        return render_template(
            "error.html",
            title="File too large",
            message=message,
        ), 413

    @app.route("/setup-admin", methods=["GET", "POST"])
    def setup_admin():
        setup_key = os.environ.get("ADMIN_SETUP_KEY", "").strip()

        if not setup_key:
            abort(404)

        if request.method == "GET":
            return """
            <h2>MoneyLine Administrator Setup</h2>
            <form method="post">
                <input name="key" placeholder="Setup key" required><br><br>
                <input name="name" placeholder="Administrator name" required><br><br>
                <input name="username" placeholder="Username" required><br><br>
                <input name="email" type="email" placeholder="Email" required><br><br>
                <input name="password" type="password" placeholder="Password" required><br><br>
                <button type="submit">Create Administrator</button>
            </form>
            """

        if request.form.get("key") != setup_key:
            abort(403)

        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip().casefold()
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")

        if not name or not username or not email or len(password) < 10:
            return "Invalid administrator details.", 400

        db = get_db()
        password_hash = generate_password_hash(password)

        existing = db.execute(
            "SELECT id FROM users WHERE username = ? OR email = ?",
            (username, email),
        ).fetchone()

        if existing:
            db.execute(
                "UPDATE users SET name = ?, username = ?, email = ?, "
                "password_hash = ?, role = 'admin', approved = 1, "
                "verified = 1, blocked = 0 WHERE id = ?",
                (name, username, email, password_hash, existing["id"]),
            )
        else:
            db.execute(
                "INSERT INTO users "
                "(name, username, email, password_hash, role, approved, verified, blocked) "
                "VALUES (?, ?, ?, ?, 'admin', 1, 1, 0)",
                (name, username, email, password_hash),
            )

        db.commit()
        return "Administrator created successfully. You can now log in."

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
