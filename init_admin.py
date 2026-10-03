import getpass
import os
import sqlite3
import sys

from werkzeug.security import generate_password_hash

from app import app as flask_app


def main():
    username = "clayton paul otieno"
    with flask_app.app_context():
        pass
    print("Create the first Moneyline administrator.")
    print(f"Admin username: {username}")
    name = input("Administrator name [Clayton Paul Otieno]: ").strip(
    ) or "Clayton Paul Otieno"
    email = input("Administrator email: ").strip().casefold()
    if "@" not in email:
        print("Enter a valid email address.", file=sys.stderr)
        return 1

    password = getpass.getpass("Choose the administrator password: ")
    if len(password) < 10:
        print("Use at least 10 characters.", file=sys.stderr)
        return 1
    if password != getpass.getpass("Confirm the password: "):
        print("Passwords did not match.", file=sys.stderr)
        return 1

    database = os.environ.get("DATABASE_PATH", "money_tracker.sqlite3")
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO users (name, username, email, password_hash, role, approved, verified) "
            "VALUES (?, ?, ?, ?, 'admin', 1, 1)",
            (name, username, email, generate_password_hash(password)),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        print("An account with that administrator username or email already exists.", file=sys.stderr)
        return 1
    finally:
        connection.close()
    print("Administrator account created. Sign in with the username above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
