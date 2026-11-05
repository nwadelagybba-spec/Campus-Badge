"""Workflow tests. Run with:  python -m unittest test_campusbadge -v
Uses a temporary data folder, so your real database is never touched."""
import os
import re
import tempfile
import unittest

os.environ["CAMPUSBADGE_DATA"] = tempfile.mkdtemp(prefix="campusbadge_test_")
os.environ.pop("CAMPUSBADGE_DB", None)
os.environ["CAMPUSBADGE_TESTING_MODE"] = "1"

import app as app_module            # noqa: E402  (must come after the env vars)
import database as db               # noqa: E402
import setup_db                     # noqa: E402
from attendance import process_scan  # noqa: E402
from exceptions import (DuplicateTimeInError, DuplicateTimeOutError, InvalidQRCodeError,  # noqa: E402
                        MissingTimeInError, UnauthorizedAccessError)
from models import Event, user_from_row  # noqa: E402

PASSWORDS = {"admin@campusbadge.test": "Admin@12345", "faculty@campusbadge.test": "Faculty@123",
             "student@campusbadge.test": "Student@123", "student2@campusbadge.test": "Student@123"}


def token(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)


def login(client, email, password=None):
    password = password or PASSWORDS[email]
    return client.post("/login", data={"_csrf": token(client), "email": email, "password": password}, follow_redirects=True)


def post(client, url, page, **data):
    return client.post(url, data={"_csrf": token(client, page), **data}, follow_redirects=True)


def student_obj(email):
    with db.db_session() as conn:
        return user_from_row(db.get_user_by_email(conn, email))


class CampusBadgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        setup_db.create_demo_data()
        app_module.app.config["TESTING"] = True
        with db.db_session() as conn:
            rows = db.list_events(conn)
            cls.live = next(r for r in rows if "now" in r["title"])
            cls.ended = next(r for r in rows if "ended" in r["title"])
            cls.future = next(r for r in rows if "Tree" in r["title"])
        cls.live_code = Event.from_row(cls.live).qr_payload

    def client(self):
        return app_module.app.test_client()

    # ---- accounts & security
    def test_01_login_redirects_by_role(self):
        for email, marker in [("admin@campusbadge.test", "Administrator Dashboard"),
                              ("faculty@campusbadge.test", "Faculty Dashboard"),
                              ("student@campusbadge.test", "Welcome, Juan")]:
            self.assertIn(marker, login(self.client(), email).get_data(as_text=True))

    def test_02_bad_login_and_pending_faculty(self):
        self.assertIn("Invalid email or password", login(self.client(), "student@campusbadge.test", "wrong").get_data(as_text=True))
        self.assertIn("waiting for administrator approval",
                      login(self.client(), "newfaculty@campusbadge.test", "Faculty@123").get_data(as_text=True))

    def test_03_register_blocks_admin_and_duplicates(self):
        c = self.client()
        form = dict(full_name="Test Student", email="new.student@test.dev", password="Passw0rdX", confirm_password="Passw0rdX", role="admin")
        self.assertIn("choose Student or Faculty", post(c, "/register", "/register", **form).get_data(as_text=True))
        form["role"] = "student"
        self.assertIn("Account created", post(c, "/register", "/register", **form).get_data(as_text=True))
        self.assertIn("already exists", post(c, "/register", "/register", **form).get_data(as_text=True))
        with db.db_session() as conn:
            self.assertEqual(db.get_user_by_email(conn, "new.student@test.dev")["role"], "student")

    def test_04_role_protection(self):
        c = self.client()
        login(c, "student@campusbadge.test")
        self.assertEqual(c.get("/admin/accounts").status_code, 403)
        self.assertEqual(c.get("/reports/export/activity").status_code, 403)
        self.assertEqual(self.client().get("/student", follow_redirects=False).status_code, 302)
        self.assertEqual(c.post("/login", data={}).status_code, 400)   # missing CSRF token

    def test_05_admin_approves_faculty(self):
        c = self.client()
        login(c, "admin@campusbadge.test")
        with db.db_session() as conn:
            target = db.get_user_by_email(conn, "newfaculty@campusbadge.test")["id"]
        post(c, f"/admin/accounts/{target}", "/admin/accounts", action="approve")
        self.assertIn("Faculty Dashboard", login(self.client(), "newfaculty@campusbadge.test", "Faculty@123").get_data(as_text=True))

    # ---- QR attendance rules
    def test_10_attendance_rules(self):
        s = student_obj("student@campusbadge.test")
        with self.assertRaises(MissingTimeInError):
            process_scan(s, self.live_code, "out")
        self.assertIn("Time in recorded", process_scan(s, self.live_code, "in"))
        with self.assertRaises(DuplicateTimeInError):
            process_scan(s, self.live_code, "in")
        self.assertIn("Attendance completed", process_scan(s, self.live_code, "out"))
        with self.assertRaises(DuplicateTimeOutError):
            process_scan(s, self.live_code, "out")
        with db.db_session() as conn:
            self.assertEqual(db.scalar(conn, "SELECT COUNT(*) FROM attendance WHERE student_id=?", (s.id,)), 1)

    def test_11_invalid_expired_unauthorized(self):
        s = student_obj("student@campusbadge.test")
        for bad in ["garbage", "CAMPUSBADGE:1:short", f"CAMPUSBADGE:{self.live['id']}:{'0' * 24}", "CAMPUSBADGE:9999:" + "a" * 24]:
            with self.assertRaises(InvalidQRCodeError):
                process_scan(s, bad, "in")
        with self.assertRaises(InvalidQRCodeError):       # ended event: QR expired
            process_scan(s, Event.from_row(self.ended).qr_payload, "in")
        with self.assertRaises(InvalidQRCodeError):       # too early
            process_scan(s, Event.from_row(self.future).qr_payload, "in")
        other = student_obj("student2@campusbadge.test")  # not registered for the live event
        with self.assertRaises(UnauthorizedAccessError):
            process_scan(other, self.live_code, "in")
        with self.assertRaises(UnauthorizedAccessError):  # only students may scan
            process_scan(user_from_row(self._admin_row()), self.live_code, "in")

    def _admin_row(self):
        with db.db_session() as conn:
            return db.get_user_by_email(conn, "admin@campusbadge.test")

    def test_12_scan_endpoint_and_testing_mode(self):
        c = self.client()
        login(c, "student2@campusbadge.test")
        post(c, f"/events/{self.live['id']}/register", "/events")
        r = c.post("/attendance/scan", headers={"X-Requested-With": "fetch"},
                   data={"_csrf": token(c, "/attendance"), "action": "in", "simulate_event_id": self.live["id"]})
        self.assertTrue(r.get_json()["ok"])
        again = c.post("/attendance/scan", headers={"X-Requested-With": "fetch"},
                       data={"_csrf": token(c, "/attendance"), "action": "in", "simulate_event_id": self.live["id"]})
        self.assertFalse(again.get_json()["ok"])
        self.assertIn("already timed in", again.get_json()["message"])

    # ---- events CRUD, reports, badges
    def test_20_event_crud_and_permissions(self):
        c = self.client()
        login(c, "faculty@campusbadge.test")
        form = dict(title="CRUD Test Event", description="x", category="Sports", location="Oval",
                    start_time="2030-01-01T09:00", end_time="2030-01-01T11:00", capacity="20")
        self.assertIn("Event created", post(c, "/events/new", "/events/new", **form).get_data(as_text=True))
        with db.db_session() as conn:
            event_id = db.scalar(conn, "SELECT id FROM events WHERE title='CRUD Test Event'")
        form["title"] = "CRUD Test Event Renamed"
        self.assertIn("Event updated", post(c, f"/events/{event_id}/edit", f"/events/{event_id}/edit", **form).get_data(as_text=True))
        bad = dict(form, end_time="2029-01-01T09:00")
        self.assertIn("must end after it starts", post(c, f"/events/{event_id}/edit", f"/events/{event_id}/edit", **bad).get_data(as_text=True))
        s = self.client()
        login(s, "student@campusbadge.test")
        self.assertEqual(post(s, f"/events/{event_id}/delete", "/events").status_code, 403)
        self.assertIn("Event removed", post(c, f"/events/{event_id}/delete", "/events").get_data(as_text=True))
        with db.db_session() as conn:
            self.assertEqual(db.scalar(conn, "SELECT archived FROM events WHERE id=?", (event_id,)), 1)

    def test_21_csv_export(self):
        c = self.client()
        login(c, "faculty@campusbadge.test")
        data = c.get("/reports/export/attendance").get_data(as_text=True)
        self.assertTrue(data.startswith("Event,Student,ID Number,Time In,Time Out,Duration"))
        self.assertIn("Juan Dela Cruz", data)

    def test_22_badge_request_and_approval(self):
        s = self.client()
        login(s, "student@campusbadge.test")
        self.assertIn("Request sent", post(s, "/badges/request", "/badges", badge_key="leadership", note="Led the IT club").get_data(as_text=True))
        self.assertIn("already waiting", post(s, "/badges/request", "/badges", badge_key="leadership", note="Led the IT club").get_data(as_text=True))
        self.assertIn("cannot be requested", post(s, "/badges/request", "/badges", badge_key="attendance", note="please give me").get_data(as_text=True))
        f = self.client()
        login(f, "faculty@campusbadge.test")
        with db.db_session() as conn:
            item = db.list_achievements(conn, status="pending")[0]["id"]
        self.assertIn("Achievement approved", post(f, f"/achievements/{item}/review", "/achievements", decision="approve").get_data(as_text=True))
        self.assertIn("Leadership", s.get("/badges").get_data(as_text=True))

    def test_23_admin_backup(self):
        c = self.client()
        login(c, "admin@campusbadge.test")
        self.assertIn("Backup created", post(c, "/admin/backup", "/reports").get_data(as_text=True))

    def test_24_encapsulation(self):
        s = student_obj("student@campusbadge.test")
        with self.assertRaises(AttributeError):
            s.role = "admin"                # read-only property
        with self.assertRaises(Exception):
            s.email = "not-an-email"        # validated setter

    def test_25_logo_upload(self):
        import base64
        import io
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        c = self.client()
        login(c, "admin@campusbadge.test")
        tok = token(c, "/admin")
        # a non-image (even named .png) is refused
        bad = c.post("/admin/logo", data={"_csrf": tok, "logo": (io.BytesIO(b"<svg onload=alert(1)>"), "x.png")},
                     content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("Unsupported file", bad.get_data(as_text=True))
        self.assertEqual(c.get("/logo").status_code, 404)
        # a real image is accepted and shown in the sidebar
        ok = c.post("/admin/logo", data={"_csrf": tok, "logo": (io.BytesIO(png), "badge.png")},
                    content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("Logo updated", ok.get_data(as_text=True))
        self.assertIn('class="logo-img"', ok.get_data(as_text=True))
        self.assertEqual(c.get("/logo").mimetype, "image/png")
        # students cannot change it
        s = self.client()
        login(s, "student@campusbadge.test")
        self.assertEqual(s.post("/admin/logo", data={"_csrf": token(s, "/student")}).status_code, 403)
        # remove restores the default
        rem = post(c, "/admin/logo", "/admin", action="remove")
        self.assertIn("Logo removed", rem.get_data(as_text=True))
        self.assertEqual(c.get("/logo").status_code, 404)


if __name__ == "__main__":
    unittest.main()
