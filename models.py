"""Domain classes: encapsulation (User/Event/Attendance) and abstraction (Badge).

Sensitive data is stored in "private" attributes (leading underscore) and is
only changed through validated properties or methods.
"""
import hmac
import re
import secrets
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

from exceptions import (DuplicateTimeInError, DuplicateTimeOutError, InvalidQRCodeError,
                        MissingTimeInError, ValidationError)

# ----------------------------------------------------------------- settings
DT_FMT = "%Y-%m-%d %H:%M:%S"          # attendance timestamps
EVENT_DT_FMT = "%Y-%m-%d %H:%M"       # event start/end
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
QR_PREFIX = "CAMPUSBADGE"
QR_GRACE = timedelta(minutes=60)       # QR works from 1h before start to 1h after end
EVENT_CATEGORIES = ["Academic", "Seminar", "Workshop", "Leadership", "Community Service",
                    "Sports", "Cultural", "Social"]
ACADEMIC_CATEGORIES = ("Academic", "Seminar", "Workshop")

# Badge requirements are configurable here (no thresholds are hard-wired in the classes).
BADGE_THRESHOLDS = {"attendance": 5, "campus_explorer": 3, "knowledge_seeker": 3}


def parse_dt(text):
    """Parse a stored/typed date-time string into a datetime."""
    if isinstance(text, datetime):
        return text
    for fmt in (DT_FMT, EVENT_DT_FMT):
        try:
            return datetime.strptime(str(text).strip(), fmt)
        except ValueError:
            continue
    raise ValidationError("Please enter a valid date and time.")


def format_duration(time_in, time_out):
    if not time_in:
        return "—"
    if not time_out:
        return "In progress"
    minutes = int((parse_dt(time_out) - parse_dt(time_in)).total_seconds() // 60)
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m" if hours else f"{mins}m"


# -------------------------------------------------------------------- users
class User:
    ROLE = "user"
    PERMISSIONS = frozenset()
    DASHBOARD = "login"
    STATUSES = ("active", "pending", "rejected", "disabled")

    def __init__(self, user_id, full_name, email, password_hash, status="active",
                 id_number="", program=""):
        self._id = user_id
        self._password_hash = password_hash
        self._status = "active"
        self._full_name = ""
        self._email = ""
        self._id_number = ""
        self._program = ""
        self.full_name = full_name
        self.email = email
        self.id_number = id_number
        self.program = program
        self.status = status

    # --- validated properties
    @property
    def id(self):
        return self._id

    @property
    def role(self):
        return self.ROLE

    @property
    def full_name(self):
        return self._full_name

    @full_name.setter
    def full_name(self, value):
        value = (value or "").strip()
        if not 2 <= len(value) <= 100:
            raise ValidationError("Full name must be 2 to 100 characters long.")
        self._full_name = value

    @property
    def email(self):
        return self._email

    @email.setter
    def email(self, value):
        value = (value or "").strip().lower()
        if not EMAIL_RE.match(value) or len(value) > 120:
            raise ValidationError("Please enter a valid email address.")
        self._email = value

    @property
    def id_number(self):
        return self._id_number

    @id_number.setter
    def id_number(self, value):
        value = (value or "").strip()
        if len(value) > 30:
            raise ValidationError("ID number is too long.")
        self._id_number = value

    @property
    def program(self):
        return self._program

    @program.setter
    def program(self, value):
        value = (value or "").strip()
        if len(value) > 100:
            raise ValidationError("Program / department is too long.")
        self._program = value

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        if value not in self.STATUSES:
            raise ValidationError("Invalid account status.")
        self._status = value

    @property
    def is_active(self):
        return self._status == "active"

    # --- passwords (the hash is never exposed as a plain attribute)
    @staticmethod
    def validate_password(password):
        if len(password or "") < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
            raise ValidationError("Password must be at least 8 characters and include a letter and a number.")

    @classmethod
    def hash_password(cls, password):
        cls.validate_password(password)
        return generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self._password_hash, password or "")

    def change_password(self, current, new):
        if not self.check_password(current):
            raise ValidationError("Your current password is incorrect.")
        self._password_hash = self.hash_password(new)

    def can(self, permission):
        return permission in self.PERMISSIONS

    def to_record(self):
        """Data needed to save this user (used only by the database layer)."""
        return {"role": self.ROLE, "status": self._status, "full_name": self._full_name,
                "email": self._email, "password_hash": self._password_hash,
                "id_number": self._id_number, "program": self._program}


class Student(User):
    ROLE = "student"
    DASHBOARD = "student_dashboard"
    PERMISSIONS = frozenset({"view_events", "register_event", "scan_attendance",
                             "view_own_history", "request_badge", "edit_profile"})


class Faculty(User):
    ROLE = "faculty"
    DASHBOARD = "faculty_dashboard"
    PERMISSIONS = frozenset({"view_events", "manage_own_events", "view_attendance",
                             "review_achievements", "export_reports", "edit_profile"})


class Administrator(User):
    ROLE = "admin"
    DASHBOARD = "admin_dashboard"
    PERMISSIONS = frozenset({"view_events", "manage_events", "manage_users", "view_attendance",
                             "review_achievements", "export_reports", "backup", "edit_profile"})


ROLE_CLASSES = {"student": Student, "faculty": Faculty, "admin": Administrator}


def user_from_row(row):
    cls = ROLE_CLASSES[row["role"]]
    return cls(row["id"], row["full_name"], row["email"], row["password_hash"],
               row["status"], row["id_number"], row["program"])


# ------------------------------------------------------------------- events
class Event:
    def __init__(self, event_id, title, description, category, location, start_time, end_time,
                 capacity, qr_token, created_by, archived=False):
        self._id = event_id
        self._created_by = created_by
        self._archived = bool(archived)
        self._qr_token = qr_token or secrets.token_hex(12)
        self.title = title
        self.description = description
        self.category = category
        self.location = location
        self.set_schedule(start_time, end_time)
        self.capacity = capacity

    @property
    def id(self):
        return self._id

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        value = (value or "").strip()
        if not 3 <= len(value) <= 120:
            raise ValidationError("Event title must be 3 to 120 characters long.")
        self._title = value

    @property
    def description(self):
        return self._description

    @description.setter
    def description(self, value):
        value = (value or "").strip()
        if len(value) > 1000:
            raise ValidationError("Description is too long (maximum 1000 characters).")
        self._description = value

    @property
    def category(self):
        return self._category

    @category.setter
    def category(self, value):
        if value not in EVENT_CATEGORIES:
            raise ValidationError("Please choose a valid event category.")
        self._category = value

    @property
    def location(self):
        return self._location

    @location.setter
    def location(self, value):
        value = (value or "").strip()
        if not 2 <= len(value) <= 120:
            raise ValidationError("Location must be 2 to 120 characters long.")
        self._location = value

    @property
    def capacity(self):
        return self._capacity

    @capacity.setter
    def capacity(self, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValidationError("Capacity must be a whole number.")
        if not 1 <= value <= 5000:
            raise ValidationError("Capacity must be between 1 and 5000.")
        self._capacity = value

    @property
    def start_time(self):
        return self._start

    @property
    def end_time(self):
        return self._end

    def set_schedule(self, start, end):
        start, end = parse_dt(start), parse_dt(end)
        if end <= start:
            raise ValidationError("The event must end after it starts.")
        self._start, self._end = start, end

    @property
    def archived(self):
        return self._archived

    @property
    def qr_payload(self):
        return f"{QR_PREFIX}:{self._id}:{self._qr_token}"

    def regenerate_qr(self):
        self._qr_token = secrets.token_hex(12)

    def validate_scan(self, token, now):
        """Reject archived events, wrong/replaced codes, and codes outside the time window."""
        if self._archived:
            raise InvalidQRCodeError("This event is no longer available.")
        if not hmac.compare_digest(self._qr_token, token):
            raise InvalidQRCodeError("This QR code is invalid or has been replaced.")
        if now < self._start - QR_GRACE:
            raise InvalidQRCodeError("This QR code is not active yet. Attendance opens closer to the event start.")
        if now > self._end + QR_GRACE:
            raise InvalidQRCodeError("This QR code has expired because the event is over.")

    def to_record(self):
        return {"title": self._title, "description": self._description, "category": self._category,
                "location": self._location, "start_time": self._start.strftime(EVENT_DT_FMT),
                "end_time": self._end.strftime(EVENT_DT_FMT), "capacity": self._capacity,
                "qr_token": self._qr_token, "created_by": self._created_by}

    @classmethod
    def from_row(cls, row):
        return cls(row["id"], row["title"], row["description"], row["category"], row["location"],
                   row["start_time"], row["end_time"], row["capacity"], row["qr_token"],
                   row["created_by"], row["archived"])


# --------------------------------------------------------------- attendance
class Attendance:
    """Time-in/out can only change through validated methods (never by assignment)."""

    def __init__(self, attendance_id, event_id, student_id, time_in=None, time_out=None):
        self._id = attendance_id
        self._event_id = event_id
        self._student_id = student_id
        self._time_in = parse_dt(time_in) if time_in else None
        self._time_out = parse_dt(time_out) if time_out else None

    @property
    def time_in(self):
        return self._time_in

    @property
    def time_out(self):
        return self._time_out

    @property
    def is_complete(self):
        return self._time_in is not None and self._time_out is not None

    def record_time_in(self, when):
        if self._time_in is not None:
            raise DuplicateTimeInError()
        self._time_in = when

    def record_time_out(self, when):
        if self._time_in is None:
            raise MissingTimeInError()
        if self._time_out is not None:
            raise DuplicateTimeOutError()
        if when < self._time_in:
            raise ValidationError("Time out cannot be earlier than time in.")
        self._time_out = when

    @classmethod
    def from_row(cls, row):
        return cls(row["id"], row["event_id"], row["student_id"], row["time_in"], row["time_out"])


# ------------------------------------------------------------------- badges
class Badge(ABC):
    """Common interface. The dashboard only calls describe(); it never needs to
    know how each specific badge decides whether it has been earned."""
    key = ""
    name = ""
    description = ""
    icon = ""
    kind = "auto"      # "auto" = awarded by the system, "approval" = faculty must approve

    @abstractmethod
    def requirement(self):
        """Human-readable requirement text."""

    @abstractmethod
    def progress(self, stats):
        """Return (current, target) using a student's stats dictionary."""

    def is_earned(self, stats):
        current, target = self.progress(stats)
        return current >= target

    def status(self, stats):
        record = stats["achievements"].get(self.key)
        if record == "approved" or self.is_earned(stats):
            return "earned"
        return record if record in ("pending", "rejected") else "locked"

    def describe(self, stats):
        current, target = self.progress(stats)
        return {"key": self.key, "name": self.name, "description": self.description,
                "icon": self.icon, "kind": self.kind, "requirement": self.requirement(),
                "current": current, "target": target, "percent": int(100 * current / target),
                "status": self.status(stats)}


class ApprovalBadge(Badge):
    """Badges a faculty member approves after a student requests them."""
    kind = "approval"

    def progress(self, stats):
        return (1 if stats["achievements"].get(self.key) == "approved" else 0, 1)


class AttendanceBadge(Badge):
    key, name, icon = "attendance", "Attendance Streak", "AS"
    description = "Rewards students who consistently show up."

    def requirement(self):
        return f"Complete attendance (time in and out) at {BADGE_THRESHOLDS['attendance']} events."

    def progress(self, stats):
        target = BADGE_THRESHOLDS["attendance"]
        return min(stats["completed_events"], target), target


class CampusExplorerBadge(Badge):
    key, name, icon = "campus_explorer", "Campus Explorer", "CE"
    description = "Try different kinds of campus activities."

    def requirement(self):
        return f"Complete events in {BADGE_THRESHOLDS['campus_explorer']} different categories."

    def progress(self, stats):
        target = BADGE_THRESHOLDS["campus_explorer"]
        return min(stats["category_count"], target), target


class KnowledgeSeekerBadge(Badge):
    key, name, icon = "knowledge_seeker", "Knowledge Seeker", "KS"
    description = "Learn through academic events."

    def requirement(self):
        return (f"Complete {BADGE_THRESHOLDS['knowledge_seeker']} Academic, Seminar or Workshop events.")

    def progress(self, stats):
        target = BADGE_THRESHOLDS["knowledge_seeker"]
        return min(stats["academic_events"], target), target


class LeadershipBadge(ApprovalBadge):
    key, name, icon = "leadership", "Leadership", "LD"
    description = "Recognizes students who lead an organization or activity."

    def requirement(self):
        return "Request this badge and have a faculty member approve your leadership role."


class VolunteerBadge(ApprovalBadge):
    key, name, icon = "volunteer", "Volunteer", "VL"
    description = "Recognizes service to the campus community."

    def requirement(self):
        return "Request this badge with your volunteer activity; faculty approval required."


class EventBuilderBadge(ApprovalBadge):
    key, name, icon = "event_builder", "Event Builder", "EB"
    description = "Recognizes students who help organize events."

    def requirement(self):
        return "Request this badge after helping organize an event; faculty approval required."


BADGE_REGISTRY = [AttendanceBadge(), CampusExplorerBadge(), KnowledgeSeekerBadge(),
                  LeadershipBadge(), VolunteerBadge(), EventBuilderBadge()]
BADGES_BY_KEY = {badge.key: badge for badge in BADGE_REGISTRY}
