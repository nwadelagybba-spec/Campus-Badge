"""QR validation, time-in/time-out, and automatic badge awards."""
import logging
import sqlite3

import database as db
from exceptions import (DuplicateTimeInError, DuplicateTimeOutError, InvalidQRCodeError,
                        UnauthorizedAccessError, ValidationError)
from models import BADGE_REGISTRY, DT_FMT, QR_PREFIX, Attendance, Event

logger = logging.getLogger("campusbadge")


def parse_qr(code):
    """Return (event_id, token) from text like CAMPUSBADGE:12:abcdef..., or raise."""
    if not isinstance(code, str) or len(code) > 200:
        raise InvalidQRCodeError()
    parts = code.strip().split(":")
    if len(parts) != 3 or parts[0] != QR_PREFIX or not parts[1].isdigit() or not 8 <= len(parts[2]) <= 64:
        raise InvalidQRCodeError()
    return int(parts[1]), parts[2]


def badge_overview(stats):
    """Every badge type through the same interface (describe)."""
    return [badge.describe(stats) for badge in BADGE_REGISTRY]


def award_automatic_badges(conn, student_id):
    stats = db.get_student_stats(conn, student_id)
    awarded = []
    for badge in BADGE_REGISTRY:
        if badge.kind == "auto" and badge.is_earned(stats) and badge.key not in stats["achievements"]:
            if db.award_achievement(conn, student_id, badge.key, "Awarded automatically"):
                awarded.append(badge.name)
    return awarded


def process_scan(user, code, action, now=None):
    """Handle one Time In / Time Out scan. Returns a success message or raises
    a CampusBadgeError subclass. Everything is checked on the server."""
    if user is None or user.role != "student" or not user.is_active or not user.can("scan_attendance"):
        raise UnauthorizedAccessError("Only active student accounts can record attendance.")
    if action not in ("in", "out"):
        raise ValidationError("Please choose Time In or Time Out.")
    event_id, token = parse_qr(code)
    when = now or db.current_time()

    with db.db_session(immediate=True) as conn:       # one locked transaction per scan
        row = db.get_event_row(conn, event_id)
        if row is None:
            raise InvalidQRCodeError()
        event = Event.from_row(row)
        event.validate_scan(token, when)
        if not db.is_registered(conn, event_id, user.id):
            raise UnauthorizedAccessError("You are not registered for this event. Register on the Events page first.")

        saved = db.get_attendance_row(conn, event_id, user.id)
        attendance = Attendance.from_row(saved) if saved else Attendance(None, event_id, user.id)
        stamp = when.strftime(DT_FMT)
        readable = when.strftime("%b %d, %Y %I:%M %p")

        if action == "in":
            attendance.record_time_in(when)           # raises DuplicateTimeInError if already in
            try:
                db.insert_attendance(conn, event_id, user.id, stamp)
            except sqlite3.IntegrityError as exc:      # database constraint is the final safety net
                raise DuplicateTimeInError() from exc
            db.log_activity(conn, user.id, "time_in", f"{event.title}")
            return f"Time in recorded for “{event.title}” at {readable}."

        attendance.record_time_out(when)              # raises MissingTimeIn / DuplicateTimeOut
        if not db.set_time_out(conn, event_id, user.id, stamp):
            raise DuplicateTimeOutError()
        db.log_activity(conn, user.id, "time_out", f"{event.title}")
        new_badges = award_automatic_badges(conn, user.id)
        message = f"Time out recorded for “{event.title}” at {readable}. Attendance completed!"
        if new_badges:
            message += " New badge earned: " + ", ".join(new_badges) + "."
        return message
