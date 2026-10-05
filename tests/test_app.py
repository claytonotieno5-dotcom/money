import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

from app import create_app


class MoneylineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.temp_dir.name, "test.sqlite3")
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "DATABASE": self.database,
            "DATABASE_URL": "",
        })
        self.admin = self.create_user(
            "admin", "clayton paul otieno", "admin@example.test", role="admin",
            approved=1, verified=1,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_user(self, name, username, email, role="user", approved=1, verified=1):
        connection = sqlite3.connect(self.database)
        cursor = connection.execute(
            "INSERT INTO users (name, username, email, password_hash, role, approved, verified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, username, email, generate_password_hash(
                "long-test-password"), role, approved, verified),
        )
        connection.commit()
        user_id = cursor.lastrowid
        connection.close()
        return user_id

    def sign_in_session(self, client, user_id, role="user", verified=True):
        with client.session_transaction() as user_session:
            user_session["user_id"] = user_id
            user_session["role"] = role
            user_session["verified"] = verified
            user_session["csrf_token"] = "test-csrf-token"

    def test_installable_app_assets_are_available(self):
        client = self.app.test_client()
        manifest_response = client.get("/static/manifest.json")
        self.assertEqual(manifest_response.status_code, 200)
        manifest = json.loads(manifest_response.data)
        manifest_response.close()
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        for icon in manifest["icons"]:
            icon_response = client.get(icon["src"])
            self.assertEqual(icon_response.status_code, 200)
            icon_response.close()

        worker_response = client.get("/service-worker.js")
        self.assertEqual(worker_response.status_code, 200)
        self.assertEqual(
            worker_response.headers["Service-Worker-Allowed"], "/")
        self.assertIn(b"/static/", worker_response.data)
        worker_response.close()

    def test_registration_creates_account_and_opens_dashboard(self):
        applicant = self.app.test_client()
        with applicant.session_transaction() as user_session:
            user_session["csrf_token"] = "test-csrf-token"

        response = applicant.post("/register", data={
            "csrf_token": "test-csrf-token",
            "name": "Applicant",
            "username": "applicant",
            "email": "applicant@example.test",
            "password": "a-secure-test-password",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/dashboard")
        self.assertIn(b"Applicant", applicant.get("/dashboard").data)
        connection = sqlite3.connect(self.database)
        verified = connection.execute(
            "SELECT verified FROM users WHERE username = 'applicant'"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(verified, 1)

    def test_signup_does_not_require_admin_approval_or_email_verification(self):
        applicant = self.app.test_client()
        with applicant.session_transaction() as user_session:
            user_session["csrf_token"] = "test-csrf-token"
        response = applicant.post("/register", data={
            "csrf_token": "test-csrf-token",
            "name": "Student One",
            "username": "student.one",
            "email": "student@example.test",
            "password": "a-secure-test-password",
            "role": "admin",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/dashboard")

        connection = sqlite3.connect(self.database)
        user_id, role, verified = connection.execute(
            "SELECT id, role, verified FROM users WHERE username = 'student.one'"
        ).fetchone()
        connection.close()
        self.assertEqual((role, verified), ("user", 1))
        self.assertIn(b"Student One", applicant.get("/dashboard").data)

    def test_login_accepts_email_as_well_as_username(self):
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash("long-test-password"), self.admin),
        )
        connection.commit()
        connection.close()
        client = self.app.test_client()
        with client.session_transaction() as user_session:
            user_session["csrf_token"] = "test-csrf-token"

        response = client.post("/login", data={
            "csrf_token": "test-csrf-token",
            "username": "admin@example.test",
            "password": "long-test-password",
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/")
        self.assertEqual(client.get("/").location, "/admin")

    def test_admin_bootstrap_promotes_account_matching_default_email(self):
        self.create_user(
            "Existing account",
            "old-admin-name",
            "claytonotieno5@gmail.com",
        )
        import init_admin

        with (
            patch.object(init_admin, "flask_app", self.app),
            patch("builtins.input", side_effect=["", "", ""]),
            patch.object(
                init_admin.getpass,
                "getpass",
                side_effect=["a-new-secure-password", "a-new-secure-password"],
            ),
        ):
            self.assertEqual(init_admin.main(), 0)

        connection = sqlite3.connect(self.database)
        name, username, role, blocked, password_hash = connection.execute(
            "SELECT name, username, role, blocked, password_hash FROM users "
            "WHERE email = 'claytonotieno5@gmail.com'"
        ).fetchone()
        connection.close()
        self.assertEqual((name, username, role, blocked), (
            "oclayton paul otieno", "oclayton paul otieno", "admin", 0,
        ))
        self.assertTrue(check_password_hash(
            password_hash, "a-new-secure-password"))

    def test_leaderboard_awards_points_per_entry_and_hides_blocked_users(self):
        first_id = self.create_user("First", "first", "first@example.test")
        second_id = self.create_user("Second", "second", "second@example.test")
        blocked_id = self.create_user(
            "Blocked", "blocked", "blocked@example.test")
        connection = sqlite3.connect(self.database)
        for _ in range(2):
            connection.execute(
                "INSERT INTO transactions (user_id, direction, amount_cents, purpose, created_at) "
                "VALUES (?, 'used', 100, 'Entry', '2026-01-01T12:00:00+00:00')",
                (first_id,),
            )
        connection.execute(
            "INSERT INTO transactions (user_id, direction, amount_cents, purpose, created_at) "
            "VALUES (?, 'received', 100, 'Entry', '2026-01-01T12:00:00+00:00')",
            (second_id,),
        )
        connection.execute(
            "UPDATE users SET blocked = 1 WHERE id = ?", (blocked_id,))
        connection.commit()
        connection.close()
        client = self.app.test_client()
        self.sign_in_session(client, first_id)

        response = client.get("/api/leaderboard")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["leaders"][0], {
            "rank": 1, "username": "first", "points": 20,
        })
        self.assertEqual(response.json["leaders"][1]["points"], 10)
        self.assertEqual(response.json["current_user"]["rank"], 1)
        self.assertNotIn("email", response.json["leaders"][0])
        self.assertNotIn("blocked", [item["username"]
                         for item in response.json["leaders"]])

    def test_admin_cannot_access_user_leaderboard(self):
        client = self.app.test_client()
        self.sign_in_session(client, self.admin, role="admin")

        self.assertEqual(client.get("/api/leaderboard").status_code, 403)

    def test_users_can_only_read_their_own_ledger(self):
        first_id = self.create_user("First", "first", "first@example.test")
        second_id = self.create_user("Second", "second", "second@example.test")
        first = self.app.test_client()
        second = self.app.test_client()
        self.sign_in_session(first, first_id)
        self.sign_in_session(second, second_id)
        self.assertIn(b"First", first.get("/dashboard").data)

        response = first.post("/api/transactions", json={
            "direction": "used", "amount": "125.50", "purpose": "Bus fare",
        }, headers={"X-CSRF-Token": "test-csrf-token"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(first.get("/api/summary").json["spent"], 12550)
        self.assertEqual(second.get("/api/summary").json["spent"], 0)
        self.assertNotIn(b"Bus fare", second.get("/api/export.csv").data)

    def test_existing_unverified_user_can_access_ledger_api(self):
        user_id = self.create_user(
            "Waiting", "waiting", "waiting@example.test", approved=0, verified=0)
        client = self.app.test_client()
        self.sign_in_session(client, user_id, verified=False)
        self.assertEqual(client.get("/api/summary").status_code, 200)

    def test_admin_can_count_and_block_users_but_user_cannot_open_admin_page(self):
        user_id = self.create_user(
            "Ledger Owner", "owner", "owner@example.test")
        connection = sqlite3.connect(self.database)
        connection.execute(
            "INSERT INTO transactions (user_id, direction, amount_cents, purpose, created_at) "
            "VALUES (?, 'used', 500, 'Lunch', '2026-01-01T12:00:00+00:00')",
            (user_id,),
        )
        connection.commit()
        connection.close()

        admin = self.app.test_client()
        self.sign_in_session(admin, self.admin, role="admin")
        response = admin.get("/admin")
        self.assertIn(b"registered accounts", response.data)
        self.assertIn(b"Block user", response.data)
        self.assertNotIn(b"Lunch", response.data)
        response = admin.post(
            f"/admin/users/{user_id}/block",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(response.status_code, 302)
        connection = sqlite3.connect(self.database)
        self.assertEqual(
            connection.execute(
                "SELECT blocked FROM users WHERE id = ?", (user_id,)
            ).fetchone()[0],
            1,
        )
        connection.close()
        self.assertIn(b"Unblock", admin.get("/admin").data)

        user = self.app.test_client()
        self.sign_in_session(user, user_id)
        self.assertEqual(user.get("/admin").status_code, 403)
        self.assertEqual(user.get("/dashboard").status_code, 302)

        response = admin.post(
            f"/admin/users/{user_id}/unblock",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(response.status_code, 302)
        self.sign_in_session(user, user_id)
        self.assertIn(b"Ledger Owner", user.get("/dashboard").data)

    def test_admin_page_shows_clear_status_badges_for_active_and_blocked_accounts(self):
        blocked_user_id = self.create_user(
            "Blocked", "blocked.user", "blocked@example.test"
        )
        active_user_id = self.create_user(
            "Active", "active.user", "active@example.test"
        )
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE users SET blocked = 1 WHERE id = ?",
            (blocked_user_id,),
        )
        connection.commit()
        connection.close()

        admin = self.app.test_client()
        self.sign_in_session(admin, self.admin, role="admin")
        response = admin.get("/admin")

        self.assertIn(b"Blocked", response.data)
        self.assertIn(b"Active", response.data)
        self.assertNotIn(
            b"Blocked</span><span class=\"status-badge status-active\">Active</span>",
            response.data,
        )


if __name__ == "__main__":
    unittest.main()
