"""Create the database and a first administrator.

    python setup_db.py          -> create tables, then ask for an administrator account
    python setup_db.py --demo   -> also create sample accounts and events for testing
"""
import argparse
import getpass
from datetime import timedelta

import database as db
from exceptions import CampusBadgeError
from models import Administrator, Event, Faculty, Student, User

DEMO_USERS = [
    (Administrator, "System Administrator", "admin@campusbadge.test", "Admin@12345", "active", "", "ICT Office"),
    (Faculty, "Prof. Maria Reyes", "faculty@campusbadge.test", "Faculty@123", "active", "", "College of Computing"),
    (Faculty, "Prof. Pending Approval", "newfaculty@campusbadge.test", "Faculty@123", "pending", "", "College of Education"),
    (Student, "Juan Dela Cruz", "student@campusbadge.test", "Student@123", "active", "2024-0001", "BS Information Technology"),
    (Student, "Ana Santos", "student2@campusbadge.test", "Student@123", "active", "2024-0002", "BS Information Technology"),
]


def create_admin(name, email, password):
    admin = Administrator(None, name, email, User.hash_password(password), "active", "", "Administration")
    with db.db_session() as conn:
        if db.get_user_by_email(conn, admin.email):
            print(f"  (skipped, already exists) {admin.email}")
            return
        db.create_user(conn, admin.to_record())
    print(f"  created administrator {admin.email}")


def create_demo_data():
    ids = {}
    with db.db_session() as conn:
        for cls, name, email, password, status, id_number, program in DEMO_USERS:
            existing = db.get_user_by_email(conn, email)
            if existing:
                ids[email] = existing["id"]
                continue
            person = cls(None, name, email, User.hash_password(password), status, id_number, program)
            ids[email] = db.create_user(conn, person.to_record())
            print(f"  {person.role:8} {email}  password: {password}")
        if db.scalar(conn, "SELECT COUNT(*) FROM events"):
            print("  (events already exist, skipping sample events)")
            return
        now, owner = db.current_time(), ids["faculty@campusbadge.test"]
        samples = [
            ("Campus Orientation Day (happening now)", "Welcome program for students.", "Academic", "University Gym",
             now - timedelta(hours=1), now + timedelta(hours=3), 200),
            ("Leadership Training Workshop", "Build your leadership skills.", "Leadership", "Room 201",
             now + timedelta(days=1), now + timedelta(days=1, hours=4), 50),
            ("Tree Planting Drive", "Community service activity.", "Community Service", "Campus Grounds",
             now + timedelta(days=3), now + timedelta(days=3, hours=3), 100),
            ("Intro to Python Seminar (already ended)", "Use this one to test an expired QR code.", "Seminar", "Lab 3",
             now - timedelta(days=2, hours=3), now - timedelta(days=2), 40),
        ]
        for title, desc, cat, place, start, end, cap in samples:
            event = Event(None, title, desc, cat, place, start.replace(second=0, microsecond=0),
                          end.replace(second=0, microsecond=0), cap, None, owner)
            event_id = db.create_event(conn, event.to_record())
            if "now" in title or "ended" in title:   # pre-register the demo student for testing scans
                conn.execute("INSERT INTO registrations (event_id, student_id, registered_at) VALUES (?,?,?)",
                             (event_id, ids["student@campusbadge.test"], now.strftime("%Y-%m-%d %H:%M:%S")))
    print("  sample events created")


def main():
    parser = argparse.ArgumentParser(description="Set up the CampusBadge database.")
    parser.add_argument("--demo", action="store_true", help="create sample accounts and events (for testing only)")
    args = parser.parse_args()
    try:
        db.init_db()
        print("Database ready:", db.DB_PATH)
        if args.demo:
            print("Creating demo data (change these passwords before real use!):")
            create_demo_data()
            return
        with db.db_session() as conn:
            has_admin = db.count_active_admins(conn) > 0
        if has_admin:
            print("An administrator already exists. Nothing more to do.")
            return
        print("Create the first administrator account:")
        name = input("  Full name: ").strip()
        email = input("  Email: ").strip()
        password = getpass.getpass("  Password (8+ chars, letter + number): ")
        if password != getpass.getpass("  Confirm password: "):
            print("Passwords did not match. Run the command again.")
            return
        create_admin(name, email, password)
    except CampusBadgeError as exc:
        print("Setup problem:", exc.message)


if __name__ == "__main__":
    main()
