import json
import io
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

    def test_dashboard_renders_navigation_to_personal_pages(self):
        user_id = self.create_user(
            "Owner", "owner", "owner@example.test")
        client = self.app.test_client()
        self.sign_in_session(client, user_id)

        response = client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        for control in (
            b'href="/transactions"',
            b'href="/import"',
            b'href="/assistant"',
            b'href="/profile"',
        ):
            self.assertIn(control, response.data)

    def test_personal_tools_have_separate_navigation_pages(self):
        user_id = self.create_user("Owner", "owner", "owner@example.test")
        client = self.app.test_client()
        self.sign_in_session(client, user_id)

        pages = {
            "/transactions": (b"Transaction history", b'id="page-transaction-form"'),
            "/import": (b"Scan &amp; import", b'id="camera-start"', b'id="import-file"'),
            "/assistant": (
                b"Voice &amp; coach",
                b'id="voice-start"',
                b'id="voice-ask-coach"',
                b'id="coach-form"',
            ),
            "/profile": (b"Profile", b'id="profile-picture"'),
        }
        for path, markers in pages.items():
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                for marker in markers:
                    self.assertIn(marker, response.data)
                self.assertIn(b'aria-current="page"', response.data)

    def test_admin_is_redirected_from_personal_feature_pages(self):
        client = self.app.test_client()
        self.sign_in_session(client, self.admin, role="admin")

        for path in ("/transactions", "/import", "/assistant"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, "/admin")

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

    def test_csv_import_only_previews_until_user_approves_selected_rows(self):
        first_id = self.create_user("First", "first", "first@example.test")
        second_id = self.create_user("Second", "second", "second@example.test")
        first = self.app.test_client()
        second = self.app.test_client()
        self.sign_in_session(first, first_id)
        self.sign_in_session(second, second_id)
        csv_data = (
            b"type,amount,purpose,date\n"
            b"received,1000.50,Salary,2025-12-01\n"
            b"used,200,Transport,2025-12-03\n"
            b"unknown,5,Unreadable,2025-12-04\n"
        )

        preview = first.post(
            "/api/import/preview",
            data={"file": (io.BytesIO(csv_data), "budget.csv")},
            headers={"X-CSRF-Token": "test-csrf-token"},
        )

        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json["skipped"], 1)
        self.assertEqual(len(preview.json["transactions"]), 2)
        self.assertEqual(first.get("/api/summary").json["received"], 0)
        self.assertEqual(second.get("/api/summary").json["received"], 0)

        approval = first.post(
            "/api/transactions/import",
            json={"transactions": preview.json["transactions"]},
            headers={"X-CSRF-Token": "test-csrf-token"},
        )

        self.assertEqual(approval.status_code, 201)
        self.assertEqual(approval.json["count"], 2)
        self.assertEqual(first.get("/api/summary").json["received"], 100050)
        self.assertEqual(first.get("/api/summary").json["spent"], 20000)
        self.assertEqual(second.get("/api/summary").json["received"], 0)

    def test_ocr_preview_does_not_save_until_approved(self):
        user_id = self.create_user("Owner", "owner", "owner@example.test")
        client = self.app.test_client()
        self.sign_in_session(client, user_id)

        response = client.post(
            "/api/import/preview",
            json={"text": "Received 1,500 from salary\nSpent 350 on transport"},
            headers={"X-CSRF-Token": "test-csrf-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["transactions"], [
            {"direction": "received", "amount": "1500.00", "purpose": "salary", "date": ""},
            {"direction": "used", "amount": "350.00", "purpose": "transport", "date": ""},
        ])
        self.assertEqual(client.get("/api/summary").json["received"], 0)

    def test_import_rejects_invalid_batch_without_partial_writes(self):
        user_id = self.create_user("Owner", "owner", "owner@example.test")
        client = self.app.test_client()
        self.sign_in_session(client, user_id)

        response = client.post(
            "/api/transactions/import",
            json={"transactions": [
                {"direction": "received", "amount": "100", "purpose": "Pay"},
                {"direction": "unknown", "amount": "1", "purpose": "Invalid"},
            ]},
            headers={"X-CSRF-Token": "test-csrf-token"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(client.get("/api/summary").json["received"], 0)

    def test_user_can_delete_only_their_own_transactions(self):
        owner_id = self.create_user("Owner", "owner", "owner@example.test")
        other_id = self.create_user("Other", "other", "other@example.test")
        owner = self.app.test_client()
        other = self.app.test_client()
        self.sign_in_session(owner, owner_id)
        self.sign_in_session(other, other_id)
        added = owner.post(
            "/api/transactions",
            json={"direction": "used", "amount": "12.50", "purpose": "Lunch"},
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        transaction_id = owner.get("/api/summary").json["transactions"][0]["id"]

        self.assertEqual(added.status_code, 201)
        self.assertEqual(other.delete(
            f"/api/transactions/{transaction_id}",
            headers={"X-CSRF-Token": "test-csrf-token"},
        ).status_code, 404)
        self.assertEqual(owner.get("/api/summary").json["spent"], 1250)
        self.assertEqual(owner.delete(
            f"/api/transactions/{transaction_id}",
            headers={"X-CSRF-Token": "test-csrf-token"},
        ).status_code, 200)
        self.assertEqual(owner.get("/api/summary").json["spent"], 0)

    def test_profile_photo_is_stored_and_served_only_to_its_owner(self):
        owner_id = self.create_user("Owner", "owner", "owner@example.test")
        other_id = self.create_user("Other", "other", "other@example.test")
        owner = self.app.test_client()
        other = self.app.test_client()
        self.sign_in_session(owner, owner_id)
        self.sign_in_session(other, other_id)
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
            b"\x1f\x15\xc4\x89\x00\x00\x00\x0bIDAT\x08\xd7c\xf8\x0f"
            b"\x00\x01\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND"
            b"\xaeB`\x82"
        )

        updated = owner.post(
            "/profile",
            data={
                "csrf_token": "test-csrf-token",
                "profile_picture": (io.BytesIO(png), "profile.png"),
            },
        )

        self.assertEqual(updated.status_code, 302)
        photo = owner.get("/profile-picture")
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.mimetype, "image/png")
        self.assertEqual(photo.data, png)
        self.assertEqual(other.get("/profile-picture").status_code, 404)

    def test_financial_coach_uses_users_records_and_rejects_unrelated_questions(self):
        user_id = self.create_user("Owner", "owner", "owner@example.test")
        client = self.app.test_client()
        self.sign_in_session(client, user_id)
        client.post(
            "/api/transactions",
            json={"direction": "received", "amount": "500", "purpose": "Pay"},
            headers={"X-CSRF-Token": "test-csrf-token"},
        )

        answer = client.post(
            "/api/coach",
            json={"question": "What is my balance?"},
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        unrelated = client.post(
            "/api/coach",
            json={"question": "Tell me a joke"},
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        unsupported = client.post(
            "/api/coach",
            json={"question": "What investment should I make with my money?"},
            headers={"X-CSRF-Token": "test-csrf-token"},
        )

        self.assertEqual(answer.status_code, 200)
        self.assertIn("KES 500.00", answer.json["answer"])
        self.assertEqual(unrelated.status_code, 400)
        self.assertIn("cannot provide investment", unsupported.json["answer"])

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
