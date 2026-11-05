"""CampusBadge - Flask application and routes."""
import logging
import os
import secrets
import sqlite3
from datetime import datetime
from functools import wraps

import segno
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)
from markupsafe import Markup
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

import database as db
import file_processing as fp
from attendance import badge_overview, process_scan
from exceptions import (AuthenticationError, CampusBadgeError, DatabaseError, DuplicateAccountError,
                        FileProcessingError, UnauthorizedAccessError, ValidationError)
from models import (BADGES_BY_KEY, EVENT_CATEGORIES, Event, Faculty, Student, User, format_duration,
                    parse_dt, user_from_row)

fp.setup_logging()
logger = logging.getLogger("campusbadge")

app = Flask(__name__)
try:
    app.config["SECRET_KEY"] = os.environ.get("CAMPUSBADGE_SECRET") or fp.load_or_create_secret_key()
except FileProcessingError:
    app.config["SECRET_KEY"] = secrets.token_hex(32)   # sessions reset on restart, but the app still runs
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=1_000_000)
# Testing mode lets a student "simulate" a scan when no camera is available (demo only!)
TESTING_MODE = os.environ.get("CAMPUSBADGE_TESTING_MODE", "1") == "1"

try:
    db.init_db()
except CampusBadgeError:
    logger.error("Database could not be initialised at startup")

DUMMY_HASH = generate_password_hash("not-a-real-password")
STATUS_MESSAGES = {
    "pending": "Your account is waiting for administrator approval.",
    "rejected": "Your account request was not approved. Please contact the administrator.",
    "disabled": "This account has been disabled. Please contact the administrator.",
}


# ------------------------------------------------------------- helpers/filters
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


def fmt_dt(value):
    if not value:
        return "—"
    dt = parse_dt(value)
    return f"{dt.strftime('%b %d, %Y')} {int(dt.strftime('%I'))}:{dt.strftime('%M %p')}"


def event_status(start, end):
    now = db.current_time().strftime("%Y-%m-%d %H:%M")
    return "upcoming" if start > now else ("ongoing" if start <= now <= end else "ended")


def qr_svg(payload):
    return Markup(segno.make(payload, error="m").svg_inline(scale=5, dark="#0b2545", border=2))


app.jinja_env.filters.update(fmt_dt=fmt_dt, duration=format_duration,
                             dt_input=lambda v: (v or "").replace(" ", "T")[:16])
app.jinja_env.globals.update(csrf_token=csrf_token, event_status=event_status)


@app.context_processor
def inject_globals():
    logo = fp.find_logo()
    logo_url = url_for("logo_image", v=int(logo[0].stat().st_mtime)) if logo else None
    return {"user": g.get("user"), "testing_mode": TESTING_MODE, "logo_url": logo_url}


@app.before_request
def load_user_and_check_csrf():
    g.user = None
    if request.endpoint == "static":
        return
    user_id = session.get("user_id")
    if user_id:
        with db.db_session() as conn:
            row = db.get_user_row(conn, user_id)
        if row and row["status"] == "active":
            g.user = user_from_row(row)
        else:
            session.clear()
    if request.method == "POST":
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
            abort(400)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            if g.user.role not in roles:
                raise UnauthorizedAccessError()
            return view(*args, **kwargs)
        return wrapper
    return decorator


def log_action(action, details=""):
    with db.db_session() as conn:
        db.log_activity(conn, g.user.id if g.user else None, action, details)


# ------------------------------------------------------------------ error pages
def page_error(code, title, message):
    return render_template("error.html", code=code, title=title, message=message), code


@app.errorhandler(400)
def bad_request(_):
    return page_error(400, "Request could not be verified", "Your session may have expired. Go back, reload the page and try again.")


@app.errorhandler(404)
def not_found(_):
    return page_error(404, "Page not found", "We could not find what you were looking for.")


@app.errorhandler(413)
def too_large(_):
    return page_error(413, "File too large", "That file is too big. Please upload an image under 1 MB.")


@app.errorhandler(UnauthorizedAccessError)
def unauthorized(exc):
    return page_error(403, "Access denied", exc.message)


@app.errorhandler(CampusBadgeError)
def app_error(exc):
    code = 500 if isinstance(exc, (DatabaseError, FileProcessingError)) else 400
    return page_error(code, "Something went wrong", exc.message)


@app.errorhandler(Exception)
def unexpected(exc):
    if isinstance(exc, HTTPException):
        return exc
    logger.exception("Unexpected error")      # details go to the log file only
    return page_error(500, "Something went wrong", "An unexpected error occurred. Please try again.")


# ------------------------------------------------------------------ auth routes
@app.route("/")
def index():
    return redirect(url_for("dashboard" if g.user else "login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("dashboard"))
    values = {}
    if request.method == "POST":
        values = {k: request.form.get(k, "").strip() for k in ("full_name", "email", "id_number", "program", "role")}
        password, confirm = request.form.get("password", ""), request.form.get("confirm_password", "")
        try:
            if not (values["full_name"] and values["email"] and password and confirm):
                raise ValidationError("Please fill in all required fields.")
            if password != confirm:
                raise ValidationError("Passwords do not match.")
            kind = values["role"] or "student"
            if kind not in ("student", "faculty"):     # public sign-up can never create an admin
                raise ValidationError("Please choose Student or Faculty.")
            cls, status = (Student, "active") if kind == "student" else (Faculty, "pending")
            person = cls(None, values["full_name"], values["email"], User.hash_password(password),
                         status, values["id_number"], values["program"])
            with db.db_session() as conn:
                if db.get_user_by_email(conn, person.email):
                    raise DuplicateAccountError()
                try:
                    new_id = db.create_user(conn, person.to_record())
                except sqlite3.IntegrityError as exc:
                    raise DuplicateAccountError() from exc
                db.log_activity(conn, new_id, "register", f"{person.role} account registered")
            if status == "pending":
                flash("Faculty request submitted. You can log in once an administrator approves it.", "success")
            else:
                flash("Account created! You can now log in.", "success")
            return redirect(url_for("login"))
        except CampusBadgeError as exc:
            flash(exc.message, "error")
    return render_template("register.html", values=values)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email, password = request.form.get("email", "").strip().lower(), request.form.get("password", "")
        try:
            if not email or not password:
                raise ValidationError("Please enter both your email and password.")
            with db.db_session() as conn:
                row = db.get_user_by_email(conn, email)
            if row is None:
                check_password_hash(DUMMY_HASH, password)   # keeps timing similar
                raise AuthenticationError()
            person = user_from_row(row)
            if not person.check_password(password):
                raise AuthenticationError()
            if not person.is_active:
                raise AuthenticationError(STATUS_MESSAGES.get(person.status))
            session.clear()
            session["user_id"] = person.id
            with db.db_session() as conn:
                db.log_activity(conn, person.id, "login", "")
            return redirect(url_for("dashboard"))
        except CampusBadgeError as exc:
            flash(exc.message, "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    log_action("logout")
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    return redirect(url_for(g.user.DASHBOARD))


# -------------------------------------------------------------------- dashboards
@app.route("/student")
@roles_required("student")
def student_dashboard():
    uid = g.user.id
    with db.db_session() as conn:
        stats = db.get_student_stats(conn, uid)
        badges = badge_overview(stats)
        data = {
            "events_joined": db.scalar(conn, "SELECT COUNT(*) FROM registrations WHERE student_id=?", (uid,)),
            "attendance_total": db.scalar(conn, "SELECT COUNT(*) FROM attendance WHERE student_id=?", (uid,)),
            "badges_earned": sum(1 for b in badges if b["status"] == "earned"),
            "upcoming": db.list_events(conn, upcoming_after=db.current_time().strftime("%Y-%m-%d %H:%M"), limit=5),
            "registered": db.registered_event_ids(conn, uid),
            "recent": db.list_attendance(conn, student_id=uid, limit=5),
        }
    return render_template("student_dashboard.html", badges=badges, **data)


@app.route("/faculty")
@roles_required("faculty")
def faculty_dashboard():
    uid = g.user.id
    with db.db_session() as conn:
        data = {
            "events_managed": db.scalar(conn, "SELECT COUNT(*) FROM events WHERE created_by=? AND archived=0", (uid,)),
            "students_attending": db.scalar(
                conn, "SELECT COUNT(DISTINCT a.student_id) FROM attendance a JOIN events e ON e.id=a.event_id"
                      " WHERE e.created_by=?", (uid,)),
            "pending_approvals": db.scalar(conn, "SELECT COUNT(*) FROM student_achievements WHERE status='pending'"),
            "recent": db.list_attendance(conn, owner_id=uid, limit=8),
            "my_events": db.list_events(conn, owner_id=uid)[:6],
        }
    return render_template("faculty_dashboard.html", **data)


@app.route("/admin")
@roles_required("admin")
def admin_dashboard():
    with db.db_session() as conn:
        data = {
            "total_students": db.scalar(conn, "SELECT COUNT(*) FROM users WHERE role='student' AND status='active'"),
            "total_faculty": db.scalar(conn, "SELECT COUNT(*) FROM users WHERE role='faculty' AND status='active'"),
            "total_events": db.scalar(conn, "SELECT COUNT(*) FROM events WHERE archived=0"),
            "total_attendance": db.scalar(conn, "SELECT COUNT(*) FROM attendance"),
            "pending_accounts": db.scalar(conn, "SELECT COUNT(*) FROM users WHERE status='pending'"),
            "recent": db.list_attendance(conn, limit=6),
            "activity": db.list_activity(conn, limit=8),
        }
    return render_template("admin_dashboard.html", **data)


# ------------------------------------------------------------------------ events
def event_from_form(form, existing_row=None):
    """Build/validate an Event from form data (all validation lives in the Event class)."""
    start, end = form.get("start_time", "").replace("T", " "), form.get("end_time", "").replace("T", " ")
    if not start or not end:
        raise ValidationError("Start and end date/time are required.")
    if existing_row is None:
        return Event(None, form.get("title"), form.get("description"), form.get("category"),
                     form.get("location"), start, end, form.get("capacity"), None, g.user.id)
    event = Event.from_row(existing_row)
    event.title, event.description = form.get("title"), form.get("description")
    event.category, event.location = form.get("category"), form.get("location")
    event.set_schedule(start, end)
    event.capacity = form.get("capacity")
    return event


def manageable_event(conn, event_id):
    row = db.get_event_row(conn, event_id)
    if row is None or row["archived"]:
        abort(404)
    if g.user.role != "admin" and row["created_by"] != g.user.id:
        raise UnauthorizedAccessError("You can only manage events that you created.")
    return row


@app.route("/events")
@login_required
def events():
    with db.db_session() as conn:
        rows = db.list_events(conn)
        registered = db.registered_event_ids(conn, g.user.id) if g.user.role == "student" else set()
    return render_template("events.html", events=rows, registered=registered)


@app.route("/events/new", methods=["GET", "POST"])
@roles_required("faculty", "admin")
def event_new():
    values = {}
    if request.method == "POST":
        values = request.form
        try:
            event = event_from_form(request.form)
            with db.db_session() as conn:
                new_id = db.create_event(conn, event.to_record())
                db.log_activity(conn, g.user.id, "event_create", event.title)
            flash("Event created. Show its QR code at the venue for attendance.", "success")
            return redirect(url_for("event_qr", event_id=new_id))
        except CampusBadgeError as exc:
            flash(exc.message, "error")
    return render_template("event_form.html", values=values, editing=False, categories=EVENT_CATEGORIES)


@app.route("/events/<int:event_id>/edit", methods=["GET", "POST"])
@roles_required("faculty", "admin")
def event_edit(event_id):
    with db.db_session() as conn:
        row = manageable_event(conn, event_id)
    values = row
    if request.method == "POST":
        values = request.form
        try:
            event = event_from_form(request.form, row)
            with db.db_session() as conn:
                manageable_event(conn, event_id)
                if event.capacity < db.scalar(conn, "SELECT COUNT(*) FROM registrations WHERE event_id=?", (event_id,)):
                    raise ValidationError("Capacity cannot be lower than the number of registered students.")
                db.update_event(conn, event_id, event.to_record())
                db.log_activity(conn, g.user.id, "event_update", event.title)
            flash("Event updated.", "success")
            return redirect(url_for("events"))
        except CampusBadgeError as exc:
            flash(exc.message, "error")
    return render_template("event_form.html", values=values, editing=True, event_id=event_id, categories=EVENT_CATEGORIES)


@app.route("/events/<int:event_id>/delete", methods=["POST"])
@roles_required("faculty", "admin")
def event_delete(event_id):
    """Archives the event (attendance history is kept)."""
    with db.db_session() as conn:
        row = manageable_event(conn, event_id)
        db.archive_event(conn, event_id)
        db.log_activity(conn, g.user.id, "event_archive", row["title"])
    flash("Event removed. Its attendance records were kept for reports.", "success")
    return redirect(url_for("events"))


@app.route("/events/<int:event_id>/qr", methods=["GET", "POST"])
@roles_required("faculty", "admin")
def event_qr(event_id):
    with db.db_session() as conn:
        row = manageable_event(conn, event_id)
    if request.method == "POST":           # regenerate: the old QR code stops working
        event = Event.from_row(row)
        event.regenerate_qr()
        with db.db_session() as conn:
            manageable_event(conn, event_id)
            db.update_event(conn, event_id, event.to_record())
            db.log_activity(conn, g.user.id, "qr_regenerate", row["title"])
        flash("A new QR code was generated. Printed copies of the old one no longer work.", "success")
        return redirect(url_for("event_qr", event_id=event_id))
    event = Event.from_row(row)
    return render_template("event_qr.html", event=row, payload=event.qr_payload, svg=qr_svg(event.qr_payload))


@app.route("/events/<int:event_id>/register", methods=["POST"])
@roles_required("student")
def event_register(event_id):
    try:
        with db.db_session(immediate=True) as conn:
            title = db.register_student(conn, event_id, g.user.id, db.now_str())
            db.log_activity(conn, g.user.id, "event_register", title)
        flash(f"You are registered for “{title}”.", "success")
    except CampusBadgeError as exc:
        flash(exc.message, "error")
    return redirect(request.referrer or url_for("events"))


# -------------------------------------------------------------------- attendance
@app.route("/attendance")
@roles_required("student", "faculty", "admin")
def attendance():
    if g.user.role == "student":
        with db.db_session() as conn:
            history = db.list_attendance(conn, student_id=g.user.id)
            registered = db.registered_events_for(conn, g.user.id)
        return render_template("attendance.html", history=history, registered=registered)
    event_id = request.args.get("event_id", type=int)
    owner = g.user.id if g.user.role == "faculty" else None
    with db.db_session() as conn:
        records = db.list_attendance(conn, event_id=event_id, owner_id=owner)
        choices = db.list_events(conn, owner_id=owner)
    return render_template("attendance.html", records=records, choices=choices, selected=event_id)


@app.route("/attendance/scan", methods=["POST"])
@roles_required("student")
def attendance_scan():
    action = request.form.get("action", "")
    code = request.form.get("qr_code", "").strip()
    ok = False
    try:
        if TESTING_MODE and request.form.get("simulate_event_id"):    # clearly-labelled testing mode
            with db.db_session() as conn:
                row = db.get_event_row(conn, request.form.get("simulate_event_id", type=int))
            code = Event.from_row(row).qr_payload if row else ""
        message, ok = process_scan(g.user, code, action), True
    except CampusBadgeError as exc:
        message = exc.message
    except Exception:
        logger.exception("Unexpected scan error")
        message = "Something went wrong while recording attendance. Please try again."
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=ok, message=message)
    flash(message, "success" if ok else "error")
    return redirect(url_for("attendance"))


# ----------------------------------------------------------------------- badges
@app.route("/badges")
@roles_required("student")
def badges():
    with db.db_session() as conn:
        stats = db.get_student_stats(conn, g.user.id)
    return render_template("badges.html", badges=badge_overview(stats))


@app.route("/badges/request", methods=["POST"])
@roles_required("student")
def badge_request():
    key, note = request.form.get("badge_key", ""), request.form.get("note", "").strip()
    try:
        badge = BADGES_BY_KEY.get(key)
        if badge is None or badge.kind != "approval":
            raise ValidationError("That badge cannot be requested.")
        if not 5 <= len(note) <= 300:
            raise ValidationError("Please describe your achievement (5 to 300 characters).")
        with db.db_session() as conn:
            db.request_achievement(conn, g.user.id, key, note)
            db.log_activity(conn, g.user.id, "badge_request", badge.name)
        flash("Request sent! A faculty member will review it.", "success")
    except CampusBadgeError as exc:
        flash(exc.message, "error")
    return redirect(url_for("badges"))


@app.route("/achievements")
@roles_required("faculty", "admin")
def achievements():
    with db.db_session() as conn:
        pending = db.list_achievements(conn, status="pending")
        reviewed = [r for r in db.list_achievements(conn, limit=30) if r["status"] != "pending"][:10]
    return render_template("achievements.html", pending=pending, reviewed=reviewed)


@app.route("/achievements/<int:item_id>/review", methods=["POST"])
@roles_required("faculty", "admin")
def achievement_review(item_id):
    decision = {"approve": "approved", "reject": "rejected"}.get(request.form.get("decision", ""))
    try:
        if decision is None:
            raise ValidationError("Invalid choice.")
        with db.db_session() as conn:
            db.review_achievement(conn, item_id, g.user.id, decision)
            db.log_activity(conn, g.user.id, f"achievement_{decision}", f"request #{item_id}")
        flash(f"Achievement {decision}.", "success")
    except CampusBadgeError as exc:
        flash(exc.message, "error")
    return redirect(url_for("achievements"))


# ---------------------------------------------------------------- admin accounts
@app.route("/admin/accounts")
@roles_required("admin")
def accounts():
    role, status = request.args.get("role", ""), request.args.get("status", "")
    with db.db_session() as conn:
        rows = db.list_users(conn, role, status)
    return render_template("accounts.html", rows=rows, role=role, status=status)


@app.route("/admin/accounts/<int:target_id>", methods=["POST"])
@roles_required("admin")
def account_action(target_id):
    action, new_role = request.form.get("action", ""), request.form.get("role", "")
    try:
        with db.db_session(immediate=True) as conn:
            target = db.get_user_row(conn, target_id)
            if target is None:
                raise ValidationError("Account not found.")
            if target_id == g.user.id and action in ("reject", "disable", "set_role"):
                raise ValidationError("You cannot change your own account here.")
            if target["role"] == "admin" and target["status"] == "active" and db.count_active_admins(conn) <= 1 \
                    and action in ("reject", "disable", "set_role"):
                raise ValidationError("At least one active administrator is required.")
            if action in ("approve", "enable"):
                db.set_user_status(conn, target_id, "active")
            elif action == "reject":
                db.set_user_status(conn, target_id, "rejected")
            elif action == "disable":
                db.set_user_status(conn, target_id, "disabled")
            elif action == "set_role" and new_role in ("student", "faculty", "admin"):
                db.set_user_role(conn, target_id, new_role)
            else:
                raise ValidationError("Invalid action.")
            db.log_activity(conn, g.user.id, f"account_{action}", f"{target['email']} {new_role}".strip())
        flash("Account updated.", "success")
    except CampusBadgeError as exc:
        flash(exc.message, "error")
    return redirect(request.referrer or url_for("accounts"))


# ---------------------------------------------------------------------- reports
def build_export(kind, conn):
    """Return (headers, rows) for a report, limited to what this user may see."""
    owner = g.user.id if g.user.role == "faculty" else None
    if kind == "attendance":
        rows = db.list_attendance(conn, owner_id=owner)
        return (["Event", "Student", "ID Number", "Time In", "Time Out", "Duration"],
                [[r["event_title"], r["student_name"], r["id_number"], r["time_in"], r["time_out"] or "",
                  format_duration(r["time_in"], r["time_out"])] for r in rows])
    if kind == "events":
        rows = db.list_events(conn, owner_id=owner)
        return (["Title", "Category", "Location", "Start", "End", "Capacity", "Registered", "Created By"],
                [[r["title"], r["category"], r["location"], r["start_time"], r["end_time"], r["capacity"],
                  r["registered_count"], r["creator_name"] or ""] for r in rows])
    if kind == "achievements":
        rows = db.list_achievements(conn)
        return (["Student", "ID Number", "Badge", "Status", "Note", "Requested", "Reviewed By"],
                [[r["student_name"], r["id_number"], r["badge_name"], r["status"], r["note"], r["created_at"],
                  r["reviewer_name"] or ""] for r in rows])
    if kind == "activity":
        rows = db.list_activity(conn, limit=1000)
        return (["Time", "User", "Action", "Details"],
                [[r["created_at"], r["full_name"] or "", r["action"], r["details"]] for r in rows])
    if kind == "my_history":
        rows = db.list_attendance(conn, student_id=g.user.id)
        return (["Event", "Category", "Event Start", "Time In", "Time Out", "Duration"],
                [[r["event_title"], r["category"], r["event_start"], r["time_in"], r["time_out"] or "",
                  format_duration(r["time_in"], r["time_out"])] for r in rows])
    abort(404)


EXPORT_ROLES = {"attendance": ("faculty", "admin"), "events": ("faculty", "admin"),
                "achievements": ("faculty", "admin"), "activity": ("admin",), "my_history": ("student",)}


@app.route("/reports")
@roles_required("faculty", "admin")
def reports():
    owner = g.user.id if g.user.role == "faculty" else None
    with db.db_session() as conn:
        summary = db.event_summary(conn, owner)
        activity = db.list_activity(conn, 30) if g.user.role == "admin" else []
    extras = {}
    if g.user.role == "admin":
        extras = {"exports": fp.list_files(fp.EXPORT_DIR), "backups": fp.list_files(fp.BACKUP_DIR),
                  "error_lines": fp.tail_file(fp.ERROR_LOG, 12)}
    return render_template("reports.html", summary=summary, activity=activity, **extras)


@app.route("/reports/export/<kind>")
@login_required
def export_report(kind):
    if kind not in EXPORT_ROLES or g.user.role not in EXPORT_ROLES[kind]:
        raise UnauthorizedAccessError()
    with db.db_session() as conn:
        headers, rows = build_export(kind, conn)
    path = fp.write_csv(kind if kind != "my_history" else "my_participation", headers, rows)
    fp.append_export_index(kind, path.name, g.user.email)
    log_action("export", kind)
    return send_file(path, as_attachment=True, download_name=path.name, mimetype="text/csv")


@app.route("/admin/backup", methods=["POST"])
@roles_required("admin")
def backup():
    try:
        path = fp.create_backup(db.DB_PATH)
        log_action("backup", path.name)
        flash(f"Backup created: {path.name}", "success")
    except CampusBadgeError as exc:
        flash(exc.message, "error")
    return redirect(url_for("reports"))


@app.route("/admin/files/<folder>/<name>")
@roles_required("admin")
def download_file(folder, name):
    directory = {"exports": fp.EXPORT_DIR, "backups": fp.BACKUP_DIR}.get(folder)
    if directory is None:
        abort(404)
    return send_from_directory(directory, name, as_attachment=True)


# ------------------------------------------------------------- logo / badge image
@app.route("/logo")
def logo_image():
    """Public (the login page needs it). Serves the uploaded logo, or 404 if none."""
    found = fp.find_logo()
    if found is None:
        abort(404)
    response = send_file(found[0], mimetype=found[1], max_age=0)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/admin/logo", methods=["POST"])
@roles_required("admin")
def upload_logo():
    try:
        if request.form.get("action") == "remove":
            fp.remove_logo()
            log_action("logo_removed")
            flash("Logo removed. The default CB mark is back.", "success")
        else:
            fp.save_logo(request.files.get("logo"))
            log_action("logo_updated")
            flash("Logo updated.", "success")
    except CampusBadgeError as exc:
        flash(exc.message, "error")
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------------------- profile
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        try:
            person = g.user
            person.full_name = request.form.get("full_name")
            person.id_number = request.form.get("id_number")
            person.program = request.form.get("program")
            new_password = request.form.get("new_password", "")
            if new_password:
                if new_password != request.form.get("confirm_password", ""):
                    raise ValidationError("New passwords do not match.")
                person.change_password(request.form.get("current_password", ""), new_password)
            with db.db_session() as conn:
                db.update_user_profile(conn, person.id, person.to_record())
                db.log_activity(conn, person.id, "profile_update", "")
            flash("Profile updated.", "success")
        except CampusBadgeError as exc:
            flash(exc.message, "error")
        return redirect(url_for("profile"))
    return render_template("profile.html")


if __name__ == "__main__":
    app.run(debug=os.environ.get("CAMPUSBADGE_DEBUG") == "1")
