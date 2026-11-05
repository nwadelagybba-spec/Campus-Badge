# CampusBadge: Discover. Participate. Achieve. Grow.

Student organization engagement and recognition platform for Caraga State University – Cabadbaran Campus.
Students discover events, register, record **Time In / Time Out with QR codes**, and earn badges.
Faculty monitor attendance and approve achievements. Administrators manage everything.

Built with **Python (Flask) + SQLite + HTML + CSS + JavaScript**.

## Setup (Windows / macOS / Linux)

1. Install Python 3.10 or newer.
2. Open a terminal in this folder and install the two dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Create the database.
   - **For a demo / class presentation** (sample accounts + events):
     ```
     python setup_db.py --demo
     ```
   - **For real use** (you choose the administrator email and password):
     ```
     python setup_db.py
     ```
4. Run the website:
   ```
   python app.py
   ```
   Open <http://127.0.0.1:5000>.

### Demo accounts (created by `--demo` only; change them before real use)

| Role | Email | Password |
|---|---|---|
| Administrator | admin@campusbadge.test | Admin@12345 |
| Faculty | faculty@campusbadge.test | Faculty@123 |
| Faculty (pending approval) | newfaculty@campusbadge.test | Faculty@123 |
| Student | student@campusbadge.test | Student@123 |
| Student | student2@campusbadge.test | Student@123 |

## Demo script (5 minutes)

1. Log in as **faculty** → *Events* → *QR* on "Campus Orientation Day (happening now)". Show the QR.
2. Log out, log in as **student** → *QR Attendance*.
3. Choose **Time In** → use *Testing mode* (simulated scan) or paste the code text → success message.
4. Try Time In again → friendly "already timed in" error. Choose **Time Out** → completed. Try it again → error.
5. Try "Intro to Python Seminar (already ended)" in testing mode → expired QR error.
6. Student → *My Badges* → request **Leadership**. Log in as faculty → *Achievements* → approve.
7. Log in as **admin** → *Accounts* (approve the pending faculty), *Reports* (export CSV, create backup).

## Changing the logo / badge image
Log in as the administrator, open the **Dashboard**, and use the **Logo / badge image** card to upload a PNG, JPG, GIF or WEBP (max 1 MB). It replaces the "CB" mark in the sidebar and shows on the login/register pages. It is stored in `data/branding/`. Use **Remove logo** to go back to the default.

## Testing mode (no camera)

`CAMPUSBADGE_TESTING_MODE=1` (the default) shows a clearly labelled **TESTING MODE** box where a student can simulate a scan
of an event they registered for. The server still checks registration, the time window and duplicates.
**Turn it off for real use:** set `CAMPUSBADGE_TESTING_MODE=0` before running `python app.py`.
The real camera scanner uses the html5-qrcode library (loaded from the internet; needs camera permission and `localhost`/https).

## Run the automated tests

```
python -m unittest test_campusbadge -v
```
They use a temporary folder, so your real data is never touched.

## Project structure

| File | Purpose |
|---|---|
| `app.py` | Flask app, routes, role checks, CSRF protection |
| `models.py` | `User`/`Student`/`Faculty`/`Administrator`, `Event`, `Attendance` (encapsulation); abstract `Badge` + 6 badge types (abstraction) |
| `database.py` | SQLite schema, transactions, all parameterized queries |
| `attendance.py` | QR validation, time-in / time-out rules, automatic badge awards |
| `exceptions.py` | Custom exceptions with friendly messages |
| `file_processing.py` | CSV exports, backups, error log, secret key file |
| `setup_db.py` | Creates the database, first admin, and optional demo data |
| `templates/`, `static/` | HTML pages, CSS, JavaScript |
| `data/` | Created at runtime: database, `exports/`, `backups/`, `errors.log` |

## How the OOP requirements are met (for your defense)

- **Encapsulation**: private `_attributes` with validated properties (`User.email`, `Event.capacity`, …). The password hash is never a public attribute; `Attendance.time_in/time_out` can only change through `record_time_in()` / `record_time_out()`, which raise errors for invalid sequences.
- **Abstraction**: `Badge` (an `abc.ABC`) declares abstract `requirement()` and `progress()`. Each badge class implements them; the dashboard only calls `describe()`.
- **Exception handling**: custom errors (`InvalidQRCodeError`, `DuplicateTimeInError`, `MissingTimeInError`, `DuplicateTimeOutError`, `UnauthorizedAccessError`, `EventFullError`, …) are caught in routes and shown as friendly messages; technical details go to `data/errors.log` only.
- **File processing**: CSV reports (write), export index (append), secret key and logs (read/write), SQLite backup, with missing/corrupted file handling.

## Badge rules (configurable in `models.py` → `BADGE_THRESHOLDS`)

- **Attendance Streak**: 5 completed events (time in *and* out). *Automatic.*
- **Campus Explorer**: completed events in 3 different categories. *Automatic.*
- **Knowledge Seeker**: 3 completed Academic/Seminar/Workshop events. *Automatic.*
- **Leadership, Volunteer, Event Builder**: student requests, faculty/admin approves.

## Security notes

Passwords are hashed; all SQL is parameterized; sessions are HttpOnly; every POST needs a CSRF token; public registration can only
create Student accounts or *pending* Faculty accounts (an administrator must approve them); administrators are created only with
`setup_db.py`. Attendance is validated on the server inside a locked transaction, with a UNIQUE database constraint as a safety net.
Deleting an event only archives it, and there is no route that edits or deletes attendance records.
Limitation: there is no login rate-limiting; add one (e.g. Flask-Limiter) before putting this on the public internet.
"# Campus-Badge" 
"# Campus-Badge" 
