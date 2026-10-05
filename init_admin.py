import getpass
import sys

from werkzeug.security import generate_password_hash

from app import DATABASE_INTEGRITY_ERRORS, app as flask_app, get_db


def main():
    default_username = "oclayton paul otieno"
    default_name = "oclayton paul otieno"
    default_email = "claytonotieno5@gmail.com"
    with flask_app.app_context():
        pass
    print("Create or reset the Moneyline administrator account.")
    username = input(
        f"Admin username [{default_username}]: ").strip().casefold()
    username = username or default_username
    name = input(
        f"Administrator name [{default_name}]: ").strip() or default_name
    email = input(
        f"Administrator email [{default_email}]: ").strip().casefold()
    email = email or default_email
    if not name or len(name) > 100 or not username or len(username) > 80:
        print("Enter a name and a username under 80 characters.", file=sys.stderr)
        return 1
    if "@" not in email or len(email) > 254:
        print("Enter a valid email address.", file=sys.stderr)
        return 1

    password = getpass.getpass("Choose the administrator password: ")
    if len(password) < 10:
        print("Use at least 10 characters.", file=sys.stderr)
        return 1
    if password != getpass.getpass("Confirm the password: "):
        print("Passwords did not match.", file=sys.stderr)
        return 1

    with flask_app.app_context():
        try:
            db = get_db()
            matches = db.execute(
                "SELECT id FROM users WHERE username = ? OR email = ?",
                (username, email),
            ).fetchall()
            if len(matches) > 1:
                print(
                    "The username and email belong to different accounts. Resolve that conflict first.",
                    file=sys.stderr,
                )
                return 1

            password_hash = generate_password_hash(password)
            if matches:
                db.execute(
                    "UPDATE users SET name = ?, username = ?, email = ?, password_hash = ?, "
                    "role = 'admin', approved = 1, verified = 1, blocked = 0, "
                    "verification_hash = NULL, verification_expires_at = NULL, "
                    "verification_attempts = 0 WHERE id = ?",
                    (name, username, email, password_hash, matches[0]["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO users "
                    "(name, username, email, password_hash, role, approved, verified, blocked) "
                    "VALUES (?, ?, ?, ?, 'admin', 1, 1, 0)",
                    (name, username, email, password_hash),
                )
            db.commit()
        except DATABASE_INTEGRITY_ERRORS:
            get_db().rollback()
            print(
                "An account with that username or email already exists.", file=sys.stderr)
            return 1
    print(f"Administrator is ready. Sign in with {username} or {email}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
