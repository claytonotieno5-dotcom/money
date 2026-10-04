import getpass
import sys

from werkzeug.security import generate_password_hash

from app import DATABASE_INTEGRITY_ERRORS, app as flask_app, get_db


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

    with flask_app.app_context():
        try:
            get_db().execute(
                "INSERT INTO users (name, username, email, password_hash, role, approved, verified) "
                "VALUES (?, ?, ?, ?, 'admin', 1, 1)",
                (name, username, email, generate_password_hash(password)),
            )
            get_db().commit()
        except DATABASE_INTEGRITY_ERRORS:
            print(
                "An account with that administrator username or email already exists.", file=sys.stderr)
            return 1
    print("Administrator account created. Sign in with the username above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
