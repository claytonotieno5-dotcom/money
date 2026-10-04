import logging
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

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
            "MAIL_MODE": "console",
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

    def test_apps_script_delivery_uses_signed_https_request(self):
        self.app.config.update(
            MAIL_MODE="apps-script",
            MAIL_WEB_APP_URL="https://script.example.test/exec",
            MAILER_SECRET="test-mailer-secret",
        )
        user_id = self.create_user(
            "Applicant", "applicant", "applicant@example.test",
            approved=0, verified=0,
        )
        admin = self.app.test_client()
        self.sign_in_session(admin, self.admin, role="admin")

        with patch("app.urllib.request.urlopen") as send_request:
            send_request.return_value.__enter__.return_value.read.return_value = (
                b'{"ok":true}'
            )
            response = admin.post(
                f"/admin/users/{user_id}/approve",
                data={"csrf_token": "test-csrf-token"},
            )

        self.assertEqual(response.status_code, 302)
        request = send_request.call_args.args[0]
        self.assertEqual(request.full_url, "https://script.example.test/exec")
        envelope = json.loads(request.data)
        payload = json.loads(envelope["payload"])
        expected_signature = hmac.new(
            b"test-mailer-secret",
            envelope["payload"].encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(envelope["signature"], expected_signature)
        self.assertEqual(payload["to"], "applicant@example.test")
        self.assertRegex(payload["code"], r"^\d{1,6}$")
        send_request.assert_called_once()

    def test_signup_waits_for_admin_approval_and_email_code(self):
        applicant = self.app.test_client()
        self.sign_in_session(applicant, 0)
        response = applicant.post("/register", data={
            "csrf_token": "test-csrf-token",
            "name": "Student One",
            "username": "student.one",
            "email": "student@example.test",
            "password": "a-secure-test-password",
            "role": "admin",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/pending")

        connection = sqlite3.connect(self.database)
        user_id, role, approved, verified = connection.execute(
            "SELECT id, role, approved, verified FROM users WHERE username = 'student.one'"
        ).fetchone()
        connection.close()
        self.assertEqual((role, approved, verified), ("user", 0, 0))

        admin = self.app.test_client()
        self.sign_in_session(admin, self.admin, role="admin")
        with self.assertLogs("app", level=logging.WARNING) as captured:
            response = admin.post(
                f"/admin/users/{user_id}/approve",
                data={"csrf_token": "test-csrf-token"},
            )
        self.assertEqual(response.status_code, 302)
        approval_page = admin.get(response.location)
        self.assertIn(b"Local mode did not send email", approval_page.data)
        self.assertIn(b"app server terminal", approval_page.data)
        match = re.search(
            r"code for .*: (\d{1,6})", "\n".join(captured.output))
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 1)
        self.assertLessEqual(int(match.group(1)), 100_000)

        with applicant.session_transaction() as user_session:
            verification_csrf = user_session["csrf_token"]
        response = applicant.post(
            "/verify", data={"csrf_token": verification_csrf, "code": match.group(1)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/dashboard")

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

    def test_unverified_user_cannot_access_ledger_api(self):
        user_id = self.create_user(
            "Waiting", "waiting", "waiting@example.test", approved=0, verified=0)
        client = self.app.test_client()
        self.sign_in_session(client, user_id, verified=False)
        self.assertEqual(client.get("/api/summary").status_code, 403)

    def test_admin_can_review_transactions_but_user_cannot_open_admin_page(self):
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
        self.assertIn(b"Lunch", admin.get("/admin").data)
        user = self.app.test_client()
        self.sign_in_session(user, user_id)
        self.assertEqual(user.get("/admin").status_code, 403)


if __name__ == "__main__":
    unittest.main()
