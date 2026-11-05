"""SQLite connection handling, schema, and all SQL queries (parameterized only)."""
import logging
import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from exceptions import CampusBadgeError, DatabaseError, EventFullError, ValidationError
from file_processing import DATA_DIR
from models import ACADEMIC_CATEGORIES, BADGE_REGISTRY, DT_FMT

logger = logging.getLogger("campusbadge")
DB_PATH = os.environ.get("CAMPUSBADGE_DB") or str(DATA_DIR / "campusbadge.db")
PH_TZ = timezone(timedelta(hours=8))   # Philippine Standard Time


def current_time():
    """Naive datetime in Philippine time (all stored times use this clock)."""
    return datetime.now(PH_TZ).replace(tzinfo=None)


def now_str():
    return current_time().strftime(DT_FMT)


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    id_number TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('student','faculty','admin')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','pending','rejected','disabled')),
    program TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL,
    location TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    qr_token TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    CHECK (end_time > start_time)
);
CREATE TABLE IF NOT EXISTS registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id),
    student_id INTEGER NOT NULL REFERENCES users(id),
    registered_at TEXT NOT NULL,
    UNIQUE (event_id, student_id)
);
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id),
    student_id INTEGER NOT NULL REFERENCES users(id),
    time_in TEXT NOT NULL,
    time_out TEXT,
    UNIQUE (event_id, student_id),
    CHECK (time_out IS NULL OR time_out >= time_in)
);
CREATE TABLE IF NOT EXISTS badges (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS student_achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES users(id),
    badge_key TEXT NOT NULL REFERENCES badges(key),
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    reviewed_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE (student_id, badge_key)
);
CREATE TABLE IF NOT EXISTS activity_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attendance_event ON attendance(event_id);
CREATE INDEX IF NOT EXISTS idx_registrations_student ON registrations(student_id);
"""


def connect():
    try:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    except (sqlite3.Error, OSError) as exc:
        logger.exception("Could not open database")
        raise DatabaseError() from exc


def _rollback(conn):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


@contextmanager
def db_session(immediate=False):
    """One transaction: commits on success, rolls back on any error.
    immediate=True locks writers first (used for attendance and registration)."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.execute("COMMIT")
    except CampusBadgeError:
        _rollback(conn)
        raise
    except sqlite3.Error as exc:
        _rollback(conn)
        logger.exception("Database error")
        raise DatabaseError() from exc
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        for badge in BADGE_REGISTRY:
            conn.execute("INSERT OR IGNORE INTO badges (key, name, description, kind) VALUES (?,?,?,?)",
                         (badge.key, badge.name, badge.description, badge.kind))
    except sqlite3.Error as exc:
        logger.exception("Database setup failed")
        raise DatabaseError() from exc
    finally:
        conn.close()


def scalar(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()[0]


def log_activity(conn, user_id, action, details=""):
    conn.execute("INSERT INTO activity_logs (user_id, action, details, created_at) VALUES (?,?,?,?)",
                 (user_id, action, details[:300], now_str()))


# --------------------------------------------------------------------- users
def create_user(conn, rec):
    cur = conn.execute(
        "INSERT INTO users (id_number, full_name, email, password_hash, role, status, program, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (rec["id_number"], rec["full_name"], rec["email"], rec["password_hash"], rec["role"],
         rec["status"], rec["program"], now_str()))
    return cur.lastrowid


def get_user_row(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_email(conn, email):
    return conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()


def list_users(conn, role="", status=""):
    sql, args = "SELECT * FROM users WHERE 1=1", []
    if role:
        sql += " AND role = ?"
        args.append(role)
    if status:
        sql += " AND status = ?"
        args.append(status)
    return conn.execute(sql + " ORDER BY (status = 'pending') DESC, role, full_name", args).fetchall()


def update_user_profile(conn, user_id, rec):
    conn.execute("UPDATE users SET full_name=?, id_number=?, program=?, password_hash=? WHERE id=?",
                 (rec["full_name"], rec["id_number"], rec["program"], rec["password_hash"], user_id))


def set_user_status(conn, user_id, status):
    conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))


def set_user_role(conn, user_id, role):
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def count_active_admins(conn):
    return scalar(conn, "SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'")


# -------------------------------------------------------------------- events
_EVENT_SELECT = """
    SELECT e.*, u.full_name AS creator_name,
           (SELECT COUNT(*) FROM registrations r WHERE r.event_id = e.id) AS registered_count
    FROM events e LEFT JOIN users u ON u.id = e.created_by"""


def create_event(conn, rec):
    cur = conn.execute(
        "INSERT INTO events (title, description, category, location, start_time, end_time, capacity,"
        " qr_token, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rec["title"], rec["description"], rec["category"], rec["location"], rec["start_time"],
         rec["end_time"], rec["capacity"], rec["qr_token"], rec["created_by"], now_str()))
    return cur.lastrowid


def update_event(conn, event_id, rec):
    conn.execute(
        "UPDATE events SET title=?, description=?, category=?, location=?, start_time=?, end_time=?,"
        " capacity=?, qr_token=? WHERE id=?",
        (rec["title"], rec["description"], rec["category"], rec["location"], rec["start_time"],
         rec["end_time"], rec["capacity"], rec["qr_token"], event_id))


def archive_event(conn, event_id):
    conn.execute("UPDATE events SET archived = 1 WHERE id = ?", (event_id,))


def get_event_row(conn, event_id):
    return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def list_events(conn, owner_id=None, upcoming_after=None, limit=None):
    sql, args = _EVENT_SELECT + " WHERE e.archived = 0", []
    if owner_id:
        sql += " AND e.created_by = ?"
        args.append(owner_id)
    if upcoming_after:
        sql += " AND e.end_time >= ?"
        args.append(upcoming_after)
    sql += " ORDER BY e.start_time" + (" LIMIT ?" if limit else " DESC")
    if limit:
        args.append(limit)
    return conn.execute(sql, args).fetchall()


def event_summary(conn, owner_id=None):
    sql = """SELECT e.id, e.title, e.start_time, e.capacity,
        (SELECT COUNT(*) FROM registrations r WHERE r.event_id = e.id) AS registered,
        (SELECT COUNT(*) FROM attendance a WHERE a.event_id = e.id) AS timed_in,
        (SELECT COUNT(*) FROM attendance a WHERE a.event_id = e.id AND a.time_out IS NOT NULL) AS completed
        FROM events e WHERE e.archived = 0"""
    args = []
    if owner_id:
        sql += " AND e.created_by = ?"
        args.append(owner_id)
    return conn.execute(sql + " ORDER BY e.start_time DESC", args).fetchall()


# ------------------------------------------------------------- registrations
def registered_event_ids(conn, student_id):
    rows = conn.execute("SELECT event_id FROM registrations WHERE student_id = ?", (student_id,))
    return {row[0] for row in rows}


def is_registered(conn, event_id, student_id):
    return conn.execute("SELECT 1 FROM registrations WHERE event_id=? AND student_id=?",
                        (event_id, student_id)).fetchone() is not None


def register_student(conn, event_id, student_id, now_text):
    event = get_event_row(conn, event_id)
    if event is None or event["archived"]:
        raise ValidationError("That event could not be found.")
    if event["end_time"] < now_text[:16]:
        raise ValidationError("This event has already ended.")
    if is_registered(conn, event_id, student_id):
        raise ValidationError("You are already registered for this event.")
    if scalar(conn, "SELECT COUNT(*) FROM registrations WHERE event_id=?", (event_id,)) >= event["capacity"]:
        raise EventFullError()
    conn.execute("INSERT INTO registrations (event_id, student_id, registered_at) VALUES (?,?,?)",
                 (event_id, student_id, now_text))
    return event["title"]


def registered_events_for(conn, student_id):
    return conn.execute(
        "SELECT e.* FROM events e JOIN registrations r ON r.event_id = e.id"
        " WHERE r.student_id = ? AND e.archived = 0 ORDER BY e.start_time DESC", (student_id,)).fetchall()


# ---------------------------------------------------------------- attendance
def get_attendance_row(conn, event_id, student_id):
    return conn.execute("SELECT * FROM attendance WHERE event_id=? AND student_id=?",
                        (event_id, student_id)).fetchone()


def insert_attendance(conn, event_id, student_id, time_in):
    conn.execute("INSERT INTO attendance (event_id, student_id, time_in) VALUES (?,?,?)",
                 (event_id, student_id, time_in))


def set_time_out(conn, event_id, student_id, time_out):
    """Only updates rows that have no time-out yet, so a repeat cannot overwrite it."""
    cur = conn.execute("UPDATE attendance SET time_out=? WHERE event_id=? AND student_id=? AND time_out IS NULL",
                       (time_out, event_id, student_id))
    return cur.rowcount == 1


def list_attendance(conn, event_id=None, owner_id=None, student_id=None, limit=None):
    sql = """SELECT a.id, a.event_id, e.title AS event_title, e.category, e.start_time AS event_start,
                    u.full_name AS student_name, u.id_number, a.time_in, a.time_out
             FROM attendance a JOIN events e ON e.id = a.event_id JOIN users u ON u.id = a.student_id
             WHERE 1=1"""
    args = []
    if event_id:
        sql += " AND a.event_id = ?"
        args.append(event_id)
    if owner_id:
        sql += " AND e.created_by = ?"
        args.append(owner_id)
    if student_id:
        sql += " AND a.student_id = ?"
        args.append(student_id)
    sql += " ORDER BY a.time_in DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()


# ------------------------------------------------------ badges / achievements
def get_student_stats(conn, student_id):
    done = "student_id = ? AND time_out IS NOT NULL"
    marks = ",".join("?" * len(ACADEMIC_CATEGORIES))
    achievements = {row["badge_key"]: row["status"] for row in conn.execute(
        "SELECT badge_key, status FROM student_achievements WHERE student_id = ?", (student_id,))}
    return {
        "completed_events": scalar(conn, f"SELECT COUNT(*) FROM attendance WHERE {done}", (student_id,)),
        "category_count": scalar(
            conn, f"SELECT COUNT(DISTINCT e.category) FROM attendance a JOIN events e ON e.id=a.event_id"
                  f" WHERE a.{done}", (student_id,)),
        "academic_events": scalar(
            conn, f"SELECT COUNT(*) FROM attendance a JOIN events e ON e.id=a.event_id"
                  f" WHERE a.{done} AND e.category IN ({marks})", (student_id, *ACADEMIC_CATEGORIES)),
        "achievements": achievements,
    }


def award_achievement(conn, student_id, badge_key, note):
    cur = conn.execute(
        "INSERT OR IGNORE INTO student_achievements (student_id, badge_key, note, status, created_at, reviewed_at)"
        " VALUES (?,?,?,'approved',?,?)", (student_id, badge_key, note, now_str(), now_str()))
    return cur.rowcount == 1


def request_achievement(conn, student_id, badge_key, note):
    existing = conn.execute("SELECT status FROM student_achievements WHERE student_id=? AND badge_key=?",
                            (student_id, badge_key)).fetchone()
    if existing is None:
        conn.execute("INSERT INTO student_achievements (student_id, badge_key, note, created_at) VALUES (?,?,?,?)",
                     (student_id, badge_key, note, now_str()))
    elif existing["status"] == "approved":
        raise ValidationError("You have already earned this badge.")
    elif existing["status"] == "pending":
        raise ValidationError("Your request for this badge is already waiting for review.")
    else:
        conn.execute("UPDATE student_achievements SET status='pending', note=?, created_at=?, reviewed_by=NULL,"
                     " reviewed_at=NULL WHERE student_id=? AND badge_key=?",
                     (note, now_str(), student_id, badge_key))


def list_achievements(conn, status=None, limit=None):
    sql = """SELECT s.id, s.note, s.status, s.created_at, s.reviewed_at, u.full_name AS student_name,
                    u.id_number, b.name AS badge_name, r.full_name AS reviewer_name
             FROM student_achievements s JOIN users u ON u.id = s.student_id
             JOIN badges b ON b.key = s.badge_key LEFT JOIN users r ON r.id = s.reviewed_by"""
    args = []
    if status:
        sql += " WHERE s.status = ?"
        args.append(status)
    sql += " ORDER BY s.created_at DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()


def review_achievement(conn, achievement_id, reviewer_id, decision):
    cur = conn.execute("UPDATE student_achievements SET status=?, reviewed_by=?, reviewed_at=?"
                       " WHERE id=? AND status='pending'", (decision, reviewer_id, now_str(), achievement_id))
    if cur.rowcount != 1:
        raise ValidationError("That request was already reviewed or no longer exists.")


def list_activity(conn, limit=50):
    return conn.execute(
        "SELECT l.created_at, l.action, l.details, u.full_name FROM activity_logs l"
        " LEFT JOIN users u ON u.id = l.user_id ORDER BY l.id DESC LIMIT ?", (limit,)).fetchall()
