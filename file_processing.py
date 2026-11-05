"""File handling: CSV exports, backups, logs, and the secret key file.

SQLite is the single source of truth for records. Files here are *outputs*
(exports, backups, logs), so there are no conflicting copies of the same data.
"""
import csv
import logging
import os
import secrets
import sqlite3
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from exceptions import FileProcessingError

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CAMPUSBADGE_DATA", BASE_DIR / "data"))
EXPORT_DIR = DATA_DIR / "exports"
BACKUP_DIR = DATA_DIR / "backups"
LOGO_DIR = DATA_DIR / "branding"
ERROR_LOG = DATA_DIR / "errors.log"
SECRET_FILE = DATA_DIR / "secret.key"
EXPORT_INDEX = EXPORT_DIR / "export_index.csv"

logger = logging.getLogger("campusbadge")


def ensure_dirs():
    try:
        for folder in (DATA_DIR, EXPORT_DIR, BACKUP_DIR, LOGO_DIR):
            folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.exception("Could not create data folders")
        raise FileProcessingError() from exc


def setup_logging():
    """Technical errors go to data/errors.log (append mode), never to the browser."""
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    try:
        ensure_dirs()
        handler = RotatingFileHandler(ERROR_LOG, maxBytes=500_000, backupCount=2, encoding="utf-8")
    except (OSError, FileProcessingError):
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


def load_or_create_secret_key():
    """Read the Flask secret key from a file, creating it on first run."""
    try:
        ensure_dirs()
        if SECRET_FILE.exists():
            key = SECRET_FILE.read_text(encoding="utf-8").strip()
            if len(key) >= 32:
                return key
            logger.warning("Secret key file was too short; creating a new one.")
        key = secrets.token_hex(32)
        SECRET_FILE.write_text(key, encoding="utf-8")
        return key
    except (OSError, FileProcessingError) as exc:
        logger.exception("Secret key file problem")
        raise FileProcessingError() from exc


def _safe_cell(value):
    """Stop spreadsheet formula injection (cells starting with = + - @)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def write_csv(prefix, headers, rows):
    """Validate and write a CSV report; returns the file path."""
    clean_rows = []
    for row in rows:
        row = list(row)
        if len(row) != len(headers):
            raise FileProcessingError("The report data was not in the expected format.")
        clean_rows.append([_safe_cell(cell) for cell in row])
    try:
        ensure_dirs()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = EXPORT_DIR / f"{prefix}_{stamp}.csv"
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(clean_rows)
        return path
    except OSError as exc:
        logger.exception("CSV write failed")
        raise FileProcessingError() from exc


def append_export_index(kind, filename, username):
    """Append one line to the export index (an audit trail of generated files)."""
    try:
        ensure_dirs()
        is_new = not EXPORT_INDEX.exists()
        with open(EXPORT_INDEX, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(["created_at", "kind", "filename", "created_by"])
            writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind, filename, _safe_cell(username)])
    except (OSError, FileProcessingError):
        logger.exception("Could not append to export index")  # non-critical


def read_csv(path):
    """Read a CSV file back as a list of rows; handles missing/corrupted files."""
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            return list(csv.reader(handle))
    except FileNotFoundError as exc:
        raise FileProcessingError("That file could not be found.") from exc
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        logger.exception("CSV read failed")
        raise FileProcessingError("That file appears to be damaged.") from exc


def list_files(folder, limit=15):
    """Newest files first; returns [] if the folder does not exist yet."""
    try:
        files = [p for p in Path(folder).iterdir() if p.is_file()]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "size_kb": max(1, p.stat().st_size // 1024),
             "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
            for p in files[:limit]]


def tail_file(path, lines=20):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for line in handle.readlines()[-lines:]]
    except OSError:
        return []


def create_backup(db_path):
    """Copy the live SQLite database using SQLite's safe backup API."""
    try:
        ensure_dirs()
        target = BACKUP_DIR / f"campusbadge_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        source = sqlite3.connect(db_path)
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        return target
    except (OSError, sqlite3.Error) as exc:
        logger.exception("Backup failed")
        raise FileProcessingError("The backup could not be created.") from exc


# ------------------------------------------------------------------ logo / badge image
LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def _sniff_image(head):
    """Identify an image by its first bytes (never trust the filename). SVG is refused on purpose."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def find_logo():
    """Return the saved logo as (path, mimetype), or None when no logo has been uploaded."""
    for ext, mime in LOGO_TYPES.items():
        path = LOGO_DIR / f"logo.{ext}"
        if path.is_file():
            return path, mime
    return None


def remove_logo():
    try:
        for ext in LOGO_TYPES:
            (LOGO_DIR / f"logo.{ext}").unlink(missing_ok=True)
    except OSError as exc:
        logger.exception("Could not remove logo")
        raise FileProcessingError() from exc


def save_logo(file_storage):
    """Validate and store an uploaded logo image (replaces any previous one)."""
    if file_storage is None or not file_storage.filename:
        raise FileProcessingError("Please choose an image file first.")
    ext = _sniff_image(file_storage.stream.read(16))
    file_storage.stream.seek(0)
    if ext is None:
        raise FileProcessingError("Unsupported file. Please upload a PNG, JPG, GIF or WEBP image.")
    try:
        ensure_dirs()
        remove_logo()
        file_storage.save(LOGO_DIR / f"logo.{ext}")
    except OSError as exc:
        logger.exception("Could not save logo")
        raise FileProcessingError() from exc
